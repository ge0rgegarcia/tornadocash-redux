from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from eth_account import Account
from hexbytes import HexBytes
import pytest
import solcx

from components import batch_tx, rpc, util
from components.pool import Tornado
from components.mytype import ChainID, Key, Symbol, TornadoUnit
from contracts import compiler
import cli
import config
import webui


@pytest.fixture(scope='module')
def compiled():
    if compiler.SOLC_VERSION not in map(str, solcx.get_installed_solc_versions()):
        pytest.skip('Install solc ' + compiler.SOLC_VERSION + ' to run Anvil tests.')
    directory = Path(__file__).parents[1] / 'contracts'
    sources = compiler.source_files(directory)
    sources['test/Mocks.sol'] = {'content': (directory / 'test/Mocks.sol').read_text()}
    return compiler.compile_contracts(sources)['test/Mocks.sol']


@pytest.fixture
def environment(anvil, compiled):
    snapshot = anvil.provider.make_request('evm_snapshot', [])['result']
    account = anvil.eth.accounts[0]

    def deploy(name, *args):
        artifact = compiled[name]
        factory = anvil.eth.contract(abi=artifact['abi'], bytecode=artifact['evm']['bytecode']['object'])
        receipt = anvil.eth.wait_for_transaction_receipt(factory.constructor(*args).transact({'from': account}))
        assert receipt['status'] == 1
        return anvil.eth.contract(address=receipt['contractAddress'], abi=artifact['abi'])

    proxy, multicall = deploy('TestProxy'), deploy('TestMulticall3')
    key = Key(Account.create().key.to_0x_hex())
    owner = key.eth_address()
    assert 'error' not in anvil.provider.make_request('anvil_setBalance', [owner, hex(100 * 10**18)])
    connection = rpc.Connection(ChainID.SEPOLIA, anvil.provider.endpoint_uri)
    assert connection.start()
    tokens, pools, stages = {}, {}, []
    metadata = SimpleNamespace(multicall_address=multicall.address, token_address={}, decimals={Symbol.ETH: 18})

    def tornado(symbol=Symbol.ETH, unit=TornadoUnit.UNIT_0_1, decimals=18):
        if symbol != Symbol.ETH and symbol not in tokens:
            tokens[symbol] = deploy('TestDecimalsToken', decimals)
            tokens[symbol].functions.mint(owner, 1000000 * 10**decimals).transact({'from': account})
            metadata.token_address[symbol] = tokens[symbol].address
            metadata.decimals[symbol] = decimals
        token = tokens.get(symbol)
        address = token.address if token else '0x' + '00' * 20
        pool = deploy('TestPool', address, util.unit_to_wei(unit, decimals))
        proxy.functions.addPool(pool.address).transact({'from': account})
        pools[pool.address] = pool
        result = object.__new__(Tornado)
        result.external = True
        result.chain, result.symbol, result.unit = ChainID.SEPOLIA, symbol, unit
        result.connection, result.meta = connection, metadata
        result.proxy_address, result.deployment_address = proxy.address, pool.address
        result.proxy_contract, result.token_contract = proxy, token
        result._progress = stages.append
        return result

    def send(tornado, transaction):
        assert transaction is not None
        tx_hash = batch_tx.send(tornado, transaction, key)
        assert isinstance(tx_hash, HexBytes)
        receipt = anvil.eth.wait_for_transaction_receipt(tx_hash)
        assert connection.transaction_receipt(tx_hash) == receipt
        submitted = connection.transaction(tx_hash)
        assert not rpc.is_error(submitted)
        assert submitted['from'] == owner and submitted['input'] == HexBytes(transaction['data'])
        assert submitted['nonce'] == transaction['nonce'] and submitted['value'] == transaction.get('value', 0)
        assert submitted['blockHash'] == receipt['blockHash']
        expected = webui.encode_wallet_format_tx(transaction)
        item = SimpleNamespace(tornado=tornado)
        backend = object.__new__(webui.Backend)
        with patch.object(connection, 'transaction_revert_reason', wraps=connection.transaction_revert_reason) as trace:
            verified = backend._receipt(item, tx_hash.to_0x_hex(), lambda: None, stages.append, expected)
            assert verified['status'] == ('confirmed' if receipt['status'] else 'failed')
            trace.assert_not_called()
            assert 'reason' not in verified
        if receipt['status'] and 'to' not in transaction:
            assert verified['contract_address'] == receipt['contractAddress']
        altered = {**expected, 'value': hex(transaction.get('value', 0) + 1)}
        assert backend._receipt(item, tx_hash.to_0x_hex(), lambda: None, stages.append, altered)['status'] == 'mismatch'
        return receipt

    try:
        yield SimpleNamespace(web3=anvil, connection=connection, owner=owner, key=key, account=account,
                              proxy=proxy, multicall=multicall, tokens=tokens, pools=pools,
                              tornado=tornado, send=send, deploy=deploy, stages=stages)
    finally:
        anvil.provider.make_request('evm_setAutomine', [True])
        connection.stop()
        anvil.provider.make_request('evm_revert', [snapshot])


def commitment(number):
    return HexBytes(number.to_bytes(32, 'big'))


def prepare(environment, deposits):
    env = environment
    if not batch_tx.token_amounts(deposits):
        return None
    creation = batch_tx.deploy_tx(deposits, env.owner)
    assert creation['value'] == 0 and 'to' not in creation
    receipt = env.send(deposits[0][0], creation)
    assert receipt['status'] == 1
    address = receipt['contractAddress']
    for symbol, amount in batch_tx.token_amounts(deposits).items():
        approval = batch_tx.approve_tx(deposits, env.owner, symbol, depositor=address)
        assert env.send(deposits[0][0], approval)['status'] == 1
        assert env.tokens[symbol].functions.allowance(env.owner, address).call() == amount
    return address


@pytest.mark.parametrize('kind', ['native', 'erc20', 'mixed'])
def test_batch_build_sign_send_and_receipts_over_anvil_rpc(environment, kind):
    env = environment
    native, dai = env.tornado(), env.tornado(Symbol.DAI, TornadoUnit.UNIT_100)
    deposits = []
    if kind != 'erc20':
        deposits += [(native, commitment(1)), (native, commitment(2))]
    if kind != 'native':
        deposits += [(dai, commitment(3)), (dai, commitment(4))]
    address = prepare(env, deposits)
    transaction = batch_tx.deposit_tx(deposits, env.owner, depositor=address)
    assert transaction['to'] == (address or env.multicall.address)
    assert transaction['value'] == (0 if kind == 'erc20' else 2 * 10**17)
    assert env.send(deposits[0][0], transaction)['status'] == 1
    assert env.pools[native.deployment_address].functions.count().call() == (0 if kind == 'erc20' else 2)
    assert env.pools[dai.deployment_address].functions.count().call() == (0 if kind == 'native' else 2)
    assert env.tokens[Symbol.DAI].functions.balanceOf(dai.deployment_address).call() == (0 if kind == 'native' else 200 * 10**18)
    if address:
        contract = env.web3.eth.contract(address=address, abi=compiler.batch_depositor_artifact(Path(config.BATCH_CONTRACTS_PATH), kind == 'mixed')[0])
        assert contract.functions.owner().call() == env.owner
        assert contract.functions.router().call() == env.proxy.address
        assert contract.functions.executed().call()
        assert env.tokens[Symbol.DAI].functions.allowance(env.owner, address).call() == 0
        assert env.tokens[Symbol.DAI].functions.allowance(address, env.proxy.address).call() == 0
        with pytest.raises(ValueError, match='already executed'):
            batch_tx.deposit_tx(deposits, env.owner, depositor=address)


def test_erc20_batch_uses_six_and_eight_decimal_token_totals(environment):
    env = environment
    usdc = env.tornado(Symbol.USDC, TornadoUnit.UNIT_100, 6)
    wbtc = env.tornado(Symbol.WBTC, TornadoUnit.UNIT_1, 8)
    deposits = [(usdc, commitment(1)), (usdc, commitment(2)), (wbtc, commitment(3))]
    assert batch_tx.token_amounts(deposits) == {Symbol.USDC: 200 * 10**6, Symbol.WBTC: 10**8}
    address = prepare(env, deposits)
    assert env.send(usdc, batch_tx.deposit_tx(deposits, env.owner, depositor=address))['status'] == 1
    assert env.tokens[Symbol.USDC].functions.balanceOf(usdc.deployment_address).call() == 200 * 10**6
    assert env.tokens[Symbol.WBTC].functions.balanceOf(wbtc.deployment_address).call() == 10**8


def test_batch_nonce_follows_an_unmined_previous_transaction(environment):
    env = environment
    native = env.tornado()
    assert 'error' not in env.web3.provider.make_request('evm_setAutomine', [False])
    transfer = {
        'from': env.owner, 'to': env.account, 'value': 1, 'nonce': 0, 'gas': 21000,
        'gasPrice': env.web3.eth.gas_price, 'chainId': ChainID.SEPOLIA.value,
    }
    signed = env.connection.sign_transaction(transfer, env.key)
    previous = env.connection.send_transaction(signed)
    assert not rpc.is_error(previous)
    assert env.connection.nonce(env.owner) == 0 and env.connection.nonce(env.owner, block='pending') == 1
    missing = env.connection.transaction_receipt(previous)
    assert rpc.is_error(missing) and missing.code == rpc.ErrorCode.TX_HASH_NOT_FOUND
    transaction = batch_tx.deposit_tx([(native, commitment(1))], env.owner)
    assert transaction['nonce'] == 1
    current = batch_tx.send(native, transaction, env.key)
    assert isinstance(current, HexBytes)
    assert 'error' not in env.web3.provider.make_request('evm_mine', [])
    assert env.connection.transaction_receipt(previous)['status'] == 1
    assert env.connection.transaction_receipt(current)['status'] == 1
    assert env.connection.nonce(env.owner) == 2


def test_native_batch_rolls_back_when_a_commitment_was_used_after_building(environment):
    env = environment
    native = env.tornado()
    deposits = [(native, commitment(1)), (native, commitment(2))]
    transaction = batch_tx.deposit_tx(deposits, env.owner)
    env.proxy.functions.deposit(native.deployment_address, commitment(2), b'').transact({
        'from': env.account, 'value': 10**17,
    })
    receipt = env.send(native, transaction)
    pool = env.pools[native.deployment_address]
    assert receipt['status'] == 0
    assert env.connection.transaction_revert_reason(receipt['transactionHash']) == 'Call failed'
    assert pool.functions.count().call() == 1
    assert not pool.functions.commitments(commitment(1)).call()
    assert env.web3.eth.get_balance(pool.address) == 10**17


@pytest.mark.parametrize('method,args,expected', [
    ('failString', [], 'Fixture revert'),
    ('failPanic', [2**256 - 1], 'Panic(0x11)'),
    ('failCustom', [], 'Custom error'),
])
def test_reverted_anvil_transactions_hide_reasons_from_webui_and_cli(environment, monkeypatch, method, args, expected):
    env = environment
    contract = env.deploy('TestReverter')
    tornado = env.tornado()
    transaction = getattr(contract.functions, method)(*args).build_transaction({
        'from': env.owner, 'chainId': ChainID.SEPOLIA.value, 'nonce': env.connection.nonce(env.owner),
        'gas': 100000, 'gasPrice': env.web3.eth.gas_price,
    })
    receipt = env.send(tornado, transaction)
    assert receipt['status'] == 0
    reason = env.connection.transaction_revert_reason(receipt['transactionHash'])
    assert expected in reason
    if method == 'failCustom':
        selector = env.web3.keccak(text='FixtureError(uint256)')[:4].to_0x_hex()
        assert selector in reason
    backend = object.__new__(webui.Backend)
    assert backend._receipt(SimpleNamespace(tornado=tornado), receipt['transactionHash'].to_0x_hex(),
                            lambda: None, env.stages.append, webui.encode_wallet_format_tx(transaction)) == {
        'status': 'failed',
    }
    info, error = Mock(), Mock()
    monkeypatch.setattr(cli.log, 'info', info)
    monkeypatch.setattr(cli.log, 'error', error)
    monkeypatch.setattr(cli.util, 'wait', lambda _: None)
    with patch.object(env.connection, 'transaction_revert_reason', wraps=env.connection.transaction_revert_reason) as trace:
        assert cli._wait_batch_receipt(env.connection, receipt['transactionHash'], 'Withdrawal') is None
        trace.assert_not_called()
    assert receipt['transactionHash'].to_0x_hex() in error.call_args.args[1]
    assert reason not in error.call_args.args[1]
    assert not any('transaction mined' in str(call.args) for call in info.call_args_list)
