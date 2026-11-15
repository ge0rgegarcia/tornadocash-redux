from concurrent.futures import ThreadPoolExecutor
from hexbytes import HexBytes
from pathlib import Path
from solcx.exceptions import SolcError
from types import SimpleNamespace
from unittest import mock
from unittest.mock import Mock, PropertyMock
from web3 import Web3
from web3.exceptions import ContractLogicError
import json
import pytest
import time

from blockchain import rpc
from contracts import compiler
from core import batch_tx
from core.pool import Tornado
from util.mytype import ChainID, MerkleProof, Note, Symbol, TornadoUnit
import config


ACCOUNT = Web3.to_checksum_address('0x' + '11' * 20)
RECIPIENT = Web3.to_checksum_address('0x' + '22' * 20)
PROXY = Web3.to_checksum_address('0x' + '33' * 20)
MULTICALL = Web3.to_checksum_address('0x' + '44' * 20)
TOKEN = Web3.to_checksum_address('0x' + '66' * 20)
COMMITMENT = HexBytes('0x' + '77' * 32)
ROOT = HexBytes('0x' + '88' * 32)

BATCH_ABI = [
    {'type': 'constructor', 'inputs': [{'name': 'routerAddress', 'type': 'address'}], 'stateMutability': 'nonpayable'},
    {'type': 'function', 'name': 'deposit', 'inputs': [
        {'name': 'groups', 'type': 'tuple[]', 'components': [
            {'name': 'token', 'type': 'address'},
            {'name': 'deposits', 'type': 'tuple[]', 'components': [
                {'name': 'instance', 'type': 'address'}, {'name': 'commitment', 'type': 'bytes32'},
            ]},
        ]},
    ], 'outputs': [], 'stateMutability': 'payable'},
    {'type': 'function', 'name': 'executed', 'inputs': [], 'outputs': [{'type': 'bool'}], 'stateMutability': 'view'},
]
ARTIFACTS = {
    'BatchDepositorUniversal': {'abi': BATCH_ABI, 'bytecode': '0x6001'},
    'BatchDepositorERC20': {'abi': BATCH_ABI, 'bytecode': '0x6002'},
}
COMPILED = {
    name + '.sol': {name: {'abi': artifact['abi'], 'evm': {'bytecode': {'object': artifact['bytecode'][2:]}}}}
    for name, artifact in ARTIFACTS.items()
}


@pytest.fixture
def compiled_contracts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source_dir: Path = tmp_path / 'src'
    source_dir.mkdir()
    for name in ('BatchDepositorBase.sol', 'BatchDepositorUniversal.sol', 'BatchDepositorERC20.sol'):
        (source_dir / name).write_text(name)
    dependency = tmp_path / 'vendor/openzeppelin/token/ERC20/IERC20.sol'
    dependency.parent.mkdir(parents=True)
    dependency.write_text('IERC20')
    monkeypatch.setattr(config, 'BATCH_CONTRACTS_PATH', str(tmp_path))
    run = Mock(return_value=COMPILED)
    monkeypatch.setattr(compiler, 'compile_contracts', run)
    return run, tmp_path


@pytest.fixture
def instances(monkeypatch: pytest.MonkeyPatch, compiled_contracts):
    connection = SimpleNamespace(nonce=Mock(return_value=7), sign_transaction=Mock(), send_transaction=Mock())
    connection.get_contract = Mock(side_effect=lambda address, abi, **kwargs: Web3().eth.contract(address=address, abi=abi, **kwargs))
    proxy_abi = json.loads(Path(config.TORNADO_PROXY_ABI_PATH).read_text())
    token_abi = json.loads(Path(config.ERC20_ABI_PATH).read_text())
    count = 0

    def create(symbol: Symbol = Symbol.ETH, unit: TornadoUnit = TornadoUnit.UNIT_1) -> Tornado:
        nonlocal count
        count += 1
        tornado = object.__new__(Tornado)
        tornado.external, tornado.connection = True, connection
        tornado.chain, tornado.symbol, tornado.unit = ChainID.ETHEREUM, symbol, unit
        tornado.proxy_address = PROXY
        tornado.deployment_address = Web3.to_checksum_address('0x' + f'{count:040x}')
        tornado.proxy_contract = Web3().eth.contract(address=PROXY, abi=proxy_abi)
        tornado.token_contract = Web3().eth.contract(address=TOKEN, abi=token_abi)
        tornado.meta = SimpleNamespace(multicall_address=MULTICALL, decimals={Symbol.ETH:18, Symbol.DAI:18, Symbol.WBTC:8},
                                       token_address={Symbol.DAI:TOKEN, Symbol.WBTC:RECIPIENT})
        tornado._progress = Mock()
        tornado.prepare_withdraw = Mock(return_value=(MerkleProof(ROOT), {'solidity': {'proof': '0x0102'}}))
        return tornado

    def build(contract, method, transaction, *args):
        if method is None:
            return {**transaction, 'to': b'', 'data': contract.constructor(*args).data_in_transaction}
        return {**transaction, 'to':contract.address, 'data':contract.encode_abi(method, args=list(args))}

    monkeypatch.setattr(batch_tx.rpc, 'contract_build_tx', Mock(side_effect=build))
    monkeypatch.setattr(batch_tx.rpc, 'contract_call', Mock(return_value=0))
    return create


def decode(transaction, path=None):
    abi = json.loads(Path(path).read_text()) if path is not None else BATCH_ABI
    contract = Web3().eth.contract(address=transaction['to'], abi=abi)
    return contract.decode_function_input(transaction['data'])[1]


def test_native_batch_encodes_values_and_only_builds_the_outer_transaction(instances, compiled_contracts) -> None:
    first, second = instances(), instances(unit=TornadoUnit.UNIT_10)
    transaction = batch_tx.deposit_tx([(first, COMMITMENT), (second, ROOT)], ACCOUNT)
    calls = decode(transaction, config.MULTICALL_ABI_PATH)['calls']
    assert transaction['to'] == MULTICALL and transaction['value'] == 11 * 10**18
    assert transaction['nonce'] == 7 and transaction['chainId'] == 1
    assert [call['value'] for call in calls] == [10**18, 10 * 10**18]
    assert all(call['target'] == PROXY and call['allowFailure'] is False for call in calls)
    for call, tornado, commitment in zip(calls, (first, second), (COMMITMENT, ROOT)):
        args = tornado.proxy_contract.decode_function_input(call['callData'])[1]
        assert args == {'_tornado':tornado.deployment_address, '_commitment':bytes(commitment), '_encryptedNote':b''}
    first.connection.nonce.assert_called_once_with(ACCOUNT, block='pending')
    batch_tx.rpc.contract_build_tx.assert_called_once()
    first.connection.sign_transaction.assert_not_called()
    first.connection.send_transaction.assert_not_called()
    compiled_contracts[0].assert_not_called()


def test_mixed_deposit_calls_the_deployed_contract_with_native_value(instances) -> None:
    eth, dai = instances(), instances(Symbol.DAI, TornadoUnit.UNIT_100)
    transaction = batch_tx.deposit_tx([(eth, COMMITMENT), (dai, ROOT), (dai, COMMITMENT)], ACCOUNT,
                                   depositor=RECIPIENT)
    groups = decode(transaction)['groups']
    assert groups == [
        {'token': '0x' + '00' * 20, 'deposits': [{'instance': eth.deployment_address, 'commitment': bytes(COMMITMENT)}]},
        {'token': TOKEN, 'deposits': [
            {'instance': dai.deployment_address, 'commitment': bytes(ROOT)},
            {'instance': dai.deployment_address, 'commitment': bytes(COMMITMENT)},
        ]},
    ]
    assert transaction['to'] == RECIPIENT and transaction['value'] == 10**18 and transaction['nonce'] == 7
    batch_tx.rpc.contract_build_tx.assert_called_once()


def test_batch_deployment_selects_the_variant_and_sends_no_native_value(instances) -> None:
    eth, dai = instances(), instances(Symbol.DAI, TornadoUnit.UNIT_100)
    for mixed in (False, True, False):
        deposits = [(eth, COMMITMENT), (dai, ROOT)] if mixed else [(dai, ROOT)]
        transaction = batch_tx.deploy_tx(deposits, ACCOUNT)
        bytecode = HexBytes('0x6001' if mixed else '0x6002')
        data = HexBytes(transaction['data'])
        assert data.startswith(bytecode)
        assert Web3().codec.decode(['address'], data[len(bytecode):])[0].lower() == PROXY.lower()
        assert 'to' not in transaction and transaction['value'] == 0
    assert batch_tx.rpc.contract_build_tx.call_count == 3


def test_approval_sums_each_token_and_uses_the_deployed_address(instances) -> None:
    dai = instances(Symbol.DAI, TornadoUnit.UNIT_100)
    other_dai = instances(Symbol.DAI, TornadoUnit.UNIT_1000)
    wbtc = instances(Symbol.WBTC, TornadoUnit.UNIT_1)
    deposits = [(dai, COMMITMENT), (other_dai, ROOT), (wbtc, COMMITMENT)]
    assert batch_tx.token_amounts(deposits) == {Symbol.DAI:1100 * 10**18, Symbol.WBTC:10**8}
    transaction = batch_tx.approve_tx(deposits, ACCOUNT, Symbol.DAI, depositor=RECIPIENT)
    args = decode(transaction, config.ERC20_ABI_PATH)
    assert list(args.values()) == [RECIPIENT, 1100 * 10**18]
    assert transaction['to'] == TOKEN and transaction['value'] == 0 and transaction['nonce'] == 7
    dai.connection.nonce.assert_called_once_with(ACCOUNT, block='pending')


def test_missing_batch_contract_does_not_affect_native_batches(instances) -> None:
    native = instances()
    assert batch_tx.deposit_tx([(native, COMMITMENT)], ACCOUNT) is not None
    erc20 = instances(Symbol.DAI)
    with pytest.raises(ValueError, match='Deploy a batch contract'):
        batch_tx.deposit_tx([(erc20, COMMITMENT)], ACCOUNT)


def test_deployed_batch_uses_the_next_pending_nonce_for_each_step(instances) -> None:
    tornado = instances(Symbol.DAI)
    deposits = [(tornado, COMMITMENT)]
    pending = 8
    tornado.connection.nonce.side_effect = lambda _, *, block='latest': pending if block == 'pending' else 7
    assert batch_tx.deploy_tx(deposits, ACCOUNT)['nonce'] == 8
    pending = 11
    approval = batch_tx.approve_tx(deposits, ACCOUNT, Symbol.DAI, depositor=RECIPIENT)
    assert approval['nonce'] == 11
    assert decode(approval, config.ERC20_ABI_PATH)['spender'] == RECIPIENT
    pending = 12
    assert batch_tx.deposit_tx(deposits, ACCOUNT, depositor=RECIPIENT)['nonce'] == 12


@pytest.mark.parametrize('status', [True, rpc.Error(rpc.ErrorCode.UNKNOWN, ())])
def test_executed_or_unreadable_batch_contract_stops_before_building(instances, status) -> None:
    tornado = instances(Symbol.DAI)
    batch_tx.rpc.contract_call.return_value = status
    with pytest.raises((ValueError, RuntimeError), match='batch contract'):
        batch_tx.deposit_tx([(tornado, COMMITMENT)], ACCOUNT, depositor=RECIPIENT)
    batch_tx.rpc.contract_build_tx.assert_not_called()


def test_withdrawals_have_separate_proofs_and_one_outer_nonce(instances, compiled_contracts) -> None:
    first, second = instances(), instances(Symbol.DAI)
    notes = [Note(bytearray([i] * 31), bytearray([i + 1] * 31)) for i in (1, 3)]
    second.prepare_withdraw.return_value = MerkleProof(COMMITMENT), {'solidity': {'proof':'0x0304'}}
    transaction = batch_tx.withdraw_tx(list(zip((first, second), notes)), ACCOUNT, RECIPIENT)
    calls = decode(transaction, config.MULTICALL_ABI_PATH)['calls']
    for index, (call, tornado, note) in enumerate(zip(calls, (first, second), notes), 1):
        args = tornado.proxy_contract.decode_function_input(call['callData'])[1]
        assert args['_nullifierHash'] == note.nullifier_hash
        assert args['_recipient'] == RECIPIENT and args['_fee'] == args['_refund'] == 0
        assert args['_proof'] == bytes.fromhex('0102' if index == 1 else '0304')
        assert args['_root'] == (ROOT if index == 1 else COMMITMENT)
        assert call['value'] == 0 and call['allowFailure'] is False
        tornado.prepare_withdraw.assert_called_once_with(note, RECIPIENT)
        tornado._progress.assert_any_call(f'Preparing withdrawal {index}/2')
    assert transaction['value'] == 0
    first.connection.nonce.assert_called_once_with(ACCOUNT, block='pending')
    batch_tx.rpc.contract_build_tx.assert_called_once()
    compiled_contracts[0].assert_not_called()


def test_duplicate_withdrawal_is_rejected_before_proving(instances) -> None:
    tornado = instances()
    note = Note(bytearray([1] * 31), bytearray([2] * 31))
    with pytest.raises(ValueError, match='same withdrawal'):
        batch_tx.withdraw_tx([(tornado, note), (tornado, note)], ACCOUNT, RECIPIENT)
    tornado.prepare_withdraw.assert_not_called()
    tornado.connection.nonce.assert_not_called()


def test_failed_proof_does_not_build_or_send_a_transaction(instances) -> None:
    tornado = instances()
    tornado.prepare_withdraw.return_value = None
    note = Note(bytearray([1] * 31), bytearray([2] * 31))
    assert batch_tx.withdraw_tx([(tornado, note)], ACCOUNT, RECIPIENT) is None
    batch_tx.rpc.contract_build_tx.assert_not_called()
    tornado.connection.nonce.assert_not_called()
    tornado.connection.sign_transaction.assert_not_called()
    tornado.connection.send_transaction.assert_not_called()


@pytest.mark.parametrize('withdraw', [False, True])
def test_batches_cannot_cross_chains(instances, withdraw) -> None:
    first, second = instances(), instances()
    second.chain = ChainID.BSC
    with pytest.raises(ValueError, match='one blockchain'):
        if withdraw:
            note = Note(bytearray([1] * 31), bytearray([2] * 31))
            batch_tx.withdraw_tx([(first, note), (second, note)], ACCOUNT, RECIPIENT)
        else:
            batch_tx.deposit_tx([(first, COMMITMENT), (second, ROOT)], ACCOUNT)
    first.connection.nonce.assert_not_called()
    batch_tx.rpc.contract_build_tx.assert_not_called()


@pytest.mark.parametrize('stage', ['nonce', 'build', 'sign', 'send'])
def test_rpc_failures_do_not_continue_to_later_stages(instances, stage) -> None:
    tornado = instances()
    error = rpc.Error(rpc.ErrorCode.UNKNOWN, ())
    if stage == 'nonce':
        tornado.connection.nonce.return_value = error
    elif stage == 'build':
        batch_tx.rpc.contract_build_tx.return_value = error
        batch_tx.rpc.contract_build_tx.side_effect = None
    else:
        tornado.connection.sign_transaction.return_value = error if stage == 'sign' else SimpleNamespace(hash=ROOT)
        tornado.connection.send_transaction.return_value = error
    transaction = batch_tx.deposit_tx([(tornado, COMMITMENT)], ACCOUNT)
    assert batch_tx.send(tornado, transaction, object()) is None
    if stage in ('nonce', 'build'):
        tornado.connection.sign_transaction.assert_not_called()
    if stage != 'send':
        tornado.connection.send_transaction.assert_not_called()


def test_signed_batch_is_sent_once(instances) -> None:
    tornado = instances()
    signed, key = object(), object()
    tx_hash = HexBytes('0x' + '99' * 32)
    tornado.connection.sign_transaction.return_value = signed
    tornado.connection.send_transaction.return_value = tx_hash
    transaction = batch_tx.deposit_tx([(tornado, COMMITMENT)], ACCOUNT)
    assert batch_tx.send(tornado, transaction, key) == tx_hash
    tornado.connection.sign_transaction.assert_called_once_with(transaction, key)
    tornado.connection.send_transaction.assert_called_once_with(signed)


def test_contracts_compile_on_first_use_and_share_the_disk_cache(compiled_contracts) -> None:
    run, directory = compiled_contracts
    for mixed in (False, True, False):
        abi, code = compiler.batch_depositor_artifact(directory, mixed)
        assert json.loads(abi) == BATCH_ABI
        assert code == ('0x6001' if mixed else '0x6002')
    run.assert_called_once_with(compiler.source_files(directory), None)
    cache = json.loads((directory / 'build/batch_depositors.json').read_text())
    assert cache['contracts'] == ARTIFACTS


@pytest.mark.parametrize('name', ['src/BatchDepositorBase.sol', 'src/BatchDepositorUniversal.sol',
                                  'src/BatchDepositorERC20.sol', 'vendor/openzeppelin/token/ERC20/IERC20.sol'])
def test_contract_source_changes_invalidate_the_cache(compiled_contracts, name: str) -> None:
    run, directory = compiled_contracts
    compiler.batch_depositor_artifact(directory, False)
    (directory / name).write_text('changed')
    compiler.batch_depositor_artifact(directory, False)
    assert run.call_count == 2


@pytest.mark.parametrize('setting', ['SOLC_VERSION', 'SETTINGS'])
def test_compiler_changes_invalidate_the_cache(compiled_contracts, monkeypatch: pytest.MonkeyPatch, setting: str) -> None:
    run, directory = compiled_contracts
    compiler.batch_depositor_artifact(directory, False)
    changed = '0.8.31' if setting == 'SOLC_VERSION' else {**compiler.SETTINGS, 'evmVersion': 'shanghai'}
    monkeypatch.setattr(compiler, setting, changed)
    compiler.batch_depositor_artifact(directory, False)
    assert run.call_count == 2


@pytest.mark.parametrize('error_type', [RuntimeError, SolcError])
def test_failed_compilation_does_not_cache_an_artifact(compiled_contracts, monkeypatch: pytest.MonkeyPatch, error_type) -> None:
    run, directory = compiled_contracts
    error = error_type('Solidity compilation failed')
    run.side_effect = error
    log_error = Mock()
    monkeypatch.setattr(batch_tx.log, 'error', log_error)
    with pytest.raises(RuntimeError, match='Batch contract compilation failed. Check backend logs.') as caught:
        compiler.batch_depositor_artifact(directory, False)
    assert caught.value.__cause__ is error
    log_error.assert_called_once_with('Batch', f'Failed to compile batch contracts: {error}')
    assert not (directory / 'build/batch_depositors.json').exists()
    run.side_effect = None
    assert compiler.batch_depositor_artifact(directory, False)[1] == '0x6002'
    assert run.call_count == 2


def test_cache_write_failure_can_be_retried(compiled_contracts, monkeypatch: pytest.MonkeyPatch) -> None:
    run, directory = compiled_contracts
    error = PermissionError('Cache directory is read-only')
    log_error = Mock()
    monkeypatch.setattr(batch_tx.log, 'error', log_error)
    with monkeypatch.context() as patch:
        patch.setattr(compiler, 'NamedTemporaryFile', Mock(side_effect=error))
        with pytest.raises(RuntimeError, match='Could not cache compiled batch contracts. Check backend logs.') as caught:
            compiler.batch_depositor_artifact(directory, False)
    assert caught.value.__cause__ is error
    log_error.assert_called_once_with('Batch', f'Failed to cache compiled batch contracts: {error}')
    assert not (directory / 'build/batch_depositors.json').exists()
    assert compiler.batch_depositor_artifact(directory, False)[1] == '0x6002'
    assert (directory / 'build/batch_depositors.json').exists()
    assert run.call_count == 2


def test_concurrent_first_use_compiles_only_once(compiled_contracts) -> None:
    run, directory = compiled_contracts

    def compile_contracts(*args, **kwargs):
        time.sleep(0.02)
        return COMPILED

    run.side_effect = compile_contracts
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(compiler.batch_depositor_artifact, [directory] * 8, [False, True] * 4))
    assert [code for _, code in results] == ['0x6002', '0x6001'] * 4
    run.assert_called_once()


def test_batch_build_hides_revert_details_without_signing(instances, monkeypatch: pytest.MonkeyPatch) -> None:
    tornado = instances()
    error = rpc.Error(rpc.ErrorCode.EXECUTION_REVERTED, (), reason='Duplicate commitment')
    build = Mock(return_value=error)
    build.side_effect = None
    monkeypatch.setattr(batch_tx.rpc, 'contract_build_tx', build)
    monkeypatch.setattr(batch_tx, '_needs_more_gas', Mock(return_value=False))
    logged = Mock()
    monkeypatch.setattr(batch_tx.log, 'error', logged)
    transaction = batch_tx.deposit_tx([(tornado, COMMITMENT)], ACCOUNT)
    assert transaction is error
    assert batch_tx.send(tornado, transaction, object()) is None
    tornado.connection.sign_transaction.assert_not_called()
    tornado.connection.send_transaction.assert_not_called()
    message = logged.call_args.args[1]
    assert 'EXECUTION_REVERTED' in message
    assert 'Duplicate commitment' not in message


@pytest.mark.parametrize('chain, gas, allowed', [
    (ChainID.ETHEREUM, 16_777_216, True),
    (ChainID.ETHEREUM, 16_777_217, False),
    (ChainID.BSC, 16_777_217, True),
])
def test_batch_gas_estimate_is_checked_against_ethereum_transaction_limit(instances, chain, gas, allowed) -> None:
    tornado = instances()
    tornado.chain = chain
    build = batch_tx.rpc.contract_build_tx
    original = build.side_effect
    build.side_effect = lambda *args: {**original(*args), 'gas': gas}

    transaction = batch_tx.deposit_tx([(tornado, COMMITMENT)], ACCOUNT)
    if allowed:
        assert transaction['gas'] == gas
        return
    assert rpc.is_error(transaction)
    assert transaction.code == rpc.ErrorCode.GAS_LIMIT_EXCEEDED
    assert batch_tx.send(tornado, transaction, object()) is None
    tornado.connection.sign_transaction.assert_not_called()
    tornado.connection.send_transaction.assert_not_called()


@pytest.mark.parametrize('needs_more_gas', [False, True])
def test_generic_multicall_revert_requires_gas_diagnosis(instances, monkeypatch: pytest.MonkeyPatch, needs_more_gas) -> None:
    tornado = instances()
    error = rpc.Error(rpc.ErrorCode.EXECUTION_REVERTED, (), reason='Multicall3: call failed')
    batch_tx.rpc.contract_build_tx.side_effect = None
    batch_tx.rpc.contract_build_tx.return_value = error
    diagnosis = Mock(return_value=needs_more_gas)
    monkeypatch.setattr(batch_tx, '_needs_more_gas', diagnosis)

    transaction = batch_tx.deposit_tx([(tornado, COMMITMENT)], ACCOUNT)
    diagnosis.assert_called_once()
    if needs_more_gas:
        assert transaction.code == rpc.ErrorCode.GAS_LIMIT_EXCEEDED
    else:
        assert transaction is error


@pytest.mark.parametrize('allowance', [16_777_216, 1_000_000])
def test_provider_gas_allowance_error_is_classified_at_ethereum_cap(instances, allowance) -> None:
    tornado = instances()
    error = rpc.Error(rpc.ErrorCode.UNKNOWN, (), msg=f'gas required exceeds allowance ({allowance})')
    batch_tx.rpc.contract_build_tx.side_effect = None
    batch_tx.rpc.contract_build_tx.return_value = error

    transaction = batch_tx.deposit_tx([(tornado, COMMITMENT)], ACCOUNT)
    if allowance == 16_777_216:
        assert transaction.code == rpc.ErrorCode.GAS_LIMIT_EXCEEDED
    else:
        assert transaction is error


@pytest.mark.parametrize('succeeds', [True, False])
def test_gas_diagnosis_compares_calls_at_the_same_block(succeeds) -> None:
    error = ContractLogicError('Multicall3: call failed')
    execute = Mock(side_effect=[error, [] if succeeds else error])
    eth = Mock()
    type(eth).block_number = PropertyMock(side_effect=[123, 124])
    contract = Mock(w3=SimpleNamespace(eth=eth))
    contract.get_function_by_name.return_value.return_value.call = execute
    params = {'from': ACCOUNT, 'nonce': 7, 'chainId': 1, 'value': 10**18}

    assert batch_tx._needs_more_gas(contract, 'aggregate3Value', ([],), params) is succeeds
    assert execute.call_args_list == [
        mock.call({**params, 'gas': 16_777_216}, block_identifier=123),
        mock.call({**params, 'gas': 33_554_432}, block_identifier=123),
    ]
