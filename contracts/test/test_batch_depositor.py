from pathlib import Path
from types import SimpleNamespace
from web3 import Web3
from web3.logs import DISCARD
import pytest

solcx = pytest.importorskip('solcx')

from contracts import compiler


ZERO = '0x' + '00' * 20


@pytest.fixture(scope='module')
def artifacts():
    if compiler.SOLC_VERSION not in map(str, solcx.get_installed_solc_versions()):
        pytest.skip('Install solc ' + compiler.SOLC_VERSION + ' to run contract tests.')
    directory = Path(__file__).parents[1]
    sources = compiler.source_files(directory)
    sources['test/Mocks.sol'] = {'content': (directory / 'test/Mocks.sol').read_text()}
    result = compiler.compile_contracts(sources)
    universal = result['BatchDepositorUniversal.sol']['BatchDepositorUniversal']
    erc20 = result['BatchDepositorERC20.sol']['BatchDepositorERC20']
    assert len(erc20['evm']['bytecode']['object']) < len(universal['evm']['bytecode']['object'])
    return result


@pytest.fixture(params=['eth-tester', 'anvil'])
def chain(request):
    if request.param == 'eth-tester':
        pytest.importorskip('eth_tester')
        pytest.importorskip('eth.vm')
        web3 = Web3(Web3.EthereumTesterProvider())
    else:
        web3 = request.getfixturevalue('anvil')
    snapshot = web3.provider.make_request('evm_snapshot', [])['result']
    web3.eth.default_account = web3.eth.accounts[0]
    yield web3
    web3.provider.make_request('evm_revert', [snapshot])


def deploy(chain, artifacts, name, *args):
    file = 'test/Mocks.sol' if name.startswith('Test') else name + '.sol'
    artifact = artifacts[file][name]
    factory = chain.eth.contract(abi=artifact['abi'], bytecode=artifact['evm']['bytecode']['object'])
    receipt = chain.eth.wait_for_transaction_receipt(factory.constructor(*args).transact())
    assert receipt['status'] == 1
    assert chain.eth.get_transaction(receipt['transactionHash'])['value'] == 0
    return chain.eth.contract(address=receipt['contractAddress'], abi=artifact['abi'])


@pytest.fixture
def setup(chain, artifacts):
    def create(variant='Universal', token_options=(False, False)):
        proxy = deploy(chain, artifacts, 'TestProxy')
        token = deploy(chain, artifacts, 'TestToken', *token_options)
        native = deploy(chain, artifacts, 'TestPool', ZERO, 10)
        erc20 = deploy(chain, artifacts, 'TestPool', token.address, 100)
        proxy.functions.addPool(native.address).transact()
        proxy.functions.addPool(erc20.address).transact()
        token.functions.mint(chain.eth.default_account, 1000).transact()
        depositor = deploy(chain, artifacts, 'BatchDepositor' + variant, proxy.address)
        return SimpleNamespace(proxy=proxy, token=token, native=native, erc20=erc20, depositor=depositor)
    return create


def item(pool, number):
    return pool.address, number.to_bytes(32, 'big')


def approve(chain, depositor, *tokens):
    assert chain.eth.get_code(depositor.address)
    for token, amount in tokens:
        token.functions.approve(depositor.address, amount).transact()


def submit(chain, depositor, groups, *, value=0, sender=None, success=True):
    transaction = chain.eth.send_transaction({
        'from': sender or chain.eth.default_account, 'to': depositor.address,
        'data': depositor.encode_abi('deposit', args=[groups]), 'value': value, 'gas': 1000000,
    })
    receipt = chain.eth.wait_for_transaction_receipt(transaction)
    assert receipt['status'] == int(success)
    return receipt


def test_mixed_deposit_transfers_each_token_total_once_and_executes_once(chain, artifacts, setup):
    state = setup()
    proxy, token, native, erc20, depositor = state.proxy, state.token, state.native, state.erc20, state.depositor
    pool = deploy(chain, artifacts, 'TestPool', token.address, 150)
    proxy.functions.addPool(pool.address).transact()
    approve(chain, depositor, (token, 250))
    groups = [(ZERO, [item(native, 1), item(native, 2)]), (token.address, [item(erc20, 3), item(pool, 4)])]
    receipt = submit(chain, depositor, groups, value=20)
    assert receipt['to'] == depositor.address
    assert depositor.functions.owner().call() == chain.eth.default_account
    assert depositor.functions.router().call() == proxy.address
    assert depositor.functions.executed().call()
    topic = Web3.keccak(text='Deposit(bytes32,uint32,uint256)')
    assert sum(log['topics'][0] == topic for log in receipt['logs']) == 4
    transfers = token.events.Transfer().process_receipt(receipt, errors=DISCARD)
    pulls = [log['args'] for log in transfers if log['args']['to'] == depositor.address]
    assert len(pulls) == 1
    assert pulls[0]['from'] == chain.eth.default_account and pulls[0]['value'] == 250
    assert chain.eth.get_balance(native.address) == 20
    assert token.functions.balanceOf(erc20.address).call() == 100
    assert token.functions.balanceOf(pool.address).call() == 150
    assert token.functions.balanceOf(chain.eth.default_account).call() == 750
    assert token.functions.balanceOf(depositor.address).call() == 0
    assert token.functions.balanceOf(proxy.address).call() == 0
    assert token.functions.allowance(depositor.address, proxy.address).call() == 0
    assert token.functions.allowance(chain.eth.default_account, depositor.address).call() == 0
    assert chain.eth.get_balance(depositor.address) == 0
    submit(chain, depositor, groups, value=20, success=False)
    assert native.functions.count().call() == 2
    assert token.functions.balanceOf(chain.eth.default_account).call() == 750


def test_erc20_deposit_supports_different_tokens_and_executes_once(chain, artifacts, setup):
    state = setup('ERC20')
    second = deploy(chain, artifacts, 'TestToken', False, False)
    pool = deploy(chain, artifacts, 'TestPool', second.address, 50)
    state.proxy.functions.addPool(pool.address).transact()
    second.functions.mint(chain.eth.default_account, 100).transact()
    approve(chain, state.depositor, (state.token, 100), (second, 50))
    receipt = submit(chain, state.depositor, [
        (state.token.address, [item(state.erc20, 5)]), (second.address, [item(pool, 6)]),
    ])
    assert receipt['to'] == state.depositor.address
    assert state.token.functions.balanceOf(state.erc20.address).call() == 100
    assert second.functions.balanceOf(pool.address).call() == 50
    assert state.depositor.functions.executed().call()
    submit(chain, state.depositor, [(state.token.address, [item(state.erc20, 15)])], success=False)


@pytest.mark.parametrize('variant', ['Universal', 'ERC20'])
@pytest.mark.parametrize('zero_first', [False, True], ids=['no-return', 'zero-reset'])
def test_repeated_groups_support_nonstandard_token_approval(chain, setup, variant, zero_first):
    state = setup(variant, (True, zero_first))
    approve(chain, state.depositor, (state.token, 200))
    submit(chain, state.depositor, [
        (state.token.address, [item(state.erc20, 7)]), (state.token.address, [item(state.erc20, 8)]),
    ])
    assert state.token.functions.balanceOf(state.erc20.address).call() == 200
    assert state.token.functions.allowance(state.depositor.address, state.proxy.address).call() == 0


def test_failed_mixed_deposit_rolls_back_and_can_retry(chain, setup):
    state = setup()
    approve(chain, state.depositor, (state.token, 200))
    duplicate = item(state.erc20, 9)
    submit(chain, state.depositor, [
        (ZERO, [item(state.native, 10)]), (state.token.address, [duplicate, duplicate]),
    ], value=10, success=False)
    assert not state.depositor.functions.executed().call()
    assert state.native.functions.count().call() == state.erc20.functions.count().call() == 0
    assert chain.eth.get_balance(state.native.address) == 0
    assert state.token.functions.balanceOf(chain.eth.default_account).call() == 1000
    assert state.token.functions.balanceOf(state.depositor.address).call() == 0
    assert state.token.functions.allowance(state.depositor.address, state.proxy.address).call() == 0
    assert state.token.functions.allowance(chain.eth.default_account, state.depositor.address).call() == 200
    submit(chain, state.depositor, [
        (ZERO, [item(state.native, 10)]), (state.token.address, [duplicate, item(state.erc20, 14)]),
    ], value=10)
    assert state.depositor.functions.executed().call()
    assert state.native.functions.count().call() == 1
    assert state.erc20.functions.count().call() == 2


def test_mixed_deposit_rejects_incorrect_value_and_token(chain, artifacts, setup):
    state = setup()
    for value in (9, 11):
        submit(chain, state.depositor, [(ZERO, [item(state.native, 11)])], value=value, success=False)
    wrong = deploy(chain, artifacts, 'TestToken', False, False)
    submit(chain, state.depositor, [(wrong.address, [item(state.erc20, 12)])], success=False)
    assert not state.depositor.functions.executed().call()
    assert state.native.functions.count().call() == 0
    assert state.token.functions.balanceOf(chain.eth.default_account).call() == 1000


@pytest.mark.parametrize('variant', ['Universal', 'ERC20'])
def test_another_caller_cannot_execute_the_batch(chain, setup, variant):
    state = setup(variant)
    approve(chain, state.depositor, (state.token, 100))
    groups = [(state.token.address, [item(state.erc20, 13)])]
    submit(chain, state.depositor, groups, sender=chain.eth.accounts[1], success=False)
    assert not state.depositor.functions.executed().call()
    assert state.erc20.functions.count().call() == 0
    assert state.token.functions.balanceOf(chain.eth.default_account).call() == 1000
    assert state.token.functions.allowance(chain.eth.default_account, state.depositor.address).call() == 100
    submit(chain, state.depositor, groups)
    assert state.erc20.functions.count().call() == 1


@pytest.mark.parametrize('variant', ['Universal', 'ERC20'])
def test_creation_rejects_proxy_without_code(chain, artifacts, variant):
    name = 'BatchDepositor' + variant
    artifact = artifacts[name + '.sol'][name]
    factory = chain.eth.contract(abi=artifact['abi'], bytecode=artifact['evm']['bytecode']['object'])
    transaction = factory.constructor(chain.eth.accounts[1]).transact({'gas': 2000000})
    receipt = chain.eth.wait_for_transaction_receipt(transaction)
    assert receipt['status'] == 0
    assert not receipt['contractAddress'] or not chain.eth.get_code(receipt['contractAddress'])


def test_erc20_only_rejects_native_and_can_retry_failed_deposit(chain, setup):
    state = setup('ERC20')
    approve(chain, state.depositor, (state.token, 200))
    duplicate = item(state.erc20, 16)
    group = (state.token.address, [duplicate, duplicate])
    submit(chain, state.depositor, [group], success=False)
    assert not state.depositor.functions.executed().call()
    assert state.erc20.functions.count().call() == 0
    assert state.token.functions.balanceOf(chain.eth.default_account).call() == 1000
    assert state.token.functions.allowance(state.depositor.address, state.proxy.address).call() == 0
    assert state.token.functions.allowance(chain.eth.default_account, state.depositor.address).call() == 200
    submit(chain, state.depositor, [(ZERO, [item(state.native, 17)])], success=False)
    submit(chain, state.depositor, [group], value=1, success=False)
    assert not state.depositor.functions.executed().call()
    assert state.native.functions.count().call() == 0
    submit(chain, state.depositor, [(state.token.address, [duplicate, item(state.erc20, 18)])])
    assert state.depositor.functions.executed().call()
    assert state.erc20.functions.count().call() == 2
    assert state.token.functions.balanceOf(chain.eth.default_account).call() == 800
