from threading import Event, Lock
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from hexbytes import HexBytes
import pytest
from components import merkle_tree, pool
from components.mytype import ChainID, CircuitInput, Key, MerkleProof, Note, Second, Symbol, TornadoUnit
ACCOUNT = '0x' + '11' * 20
RECIPIENT = '0x' + '22' * 20
HASH = HexBytes('0x' + '44' * 32)


@pytest.fixture
def note() -> Note:
    return Note(bytearray.fromhex('11' * 31), bytearray.fromhex('22' * 31))


@pytest.fixture
def tornado(monkeypatch: pytest.MonkeyPatch) -> pool.Tornado:
    instance: pool.Tornado = object.__new__(pool.Tornado)
    instance.external: bool = True
    instance.tag     : str  = 'test'
    instance.chain, instance.symbol, instance.unit = ChainID.ETHEREUM, Symbol.ETH, TornadoUnit.UNIT_1
    instance.note_deposited, instance.note_withdrawn = Mock(return_value=False), Mock(return_value=False)
    instance.token_contract, instance.deployment_contract, instance.proxy_contract = object(), object(), object()
    instance.proxy_address, instance.deployment_address = RECIPIENT, ACCOUNT
    instance.meta = SimpleNamespace(decimals={Symbol.ETH: 18, Symbol.DAI: 18})
    instance.mutex, instance.sync_only, instance.handlers, instance.initialized = Lock(), False, [], True
    instance.last_update_sec = None
    instance.db = SimpleNamespace(get_commitments=Mock(return_value=[]), add_synchronized=Mock(return_value=True), close=Mock())
    instance.tree = SimpleNamespace(get_index=Mock(return_value=0), add=Mock(),
                                    get_merkle_proof=Mock(return_value=MerkleProof(HexBytes('0x' + '33' * 32))))
    proof: dict[str, Any] = {'solidity': {'proof': '0x01', 'publicSignals': ['0x00'] * 6}}
    instance.zksnark = SimpleNamespace(prove=Mock(return_value=proof), verify=Mock(return_value=True))
    instance.connection = SimpleNamespace(nonce=Mock(return_value=0), gas_price=Mock(return_value=1),
                                          sign_transaction=Mock(), send_transaction=Mock())
    monkeypatch.setattr(pool.rpc, 'contract_build_tx', Mock(side_effect=lambda contract, method, tx, *args:
                        {**tx, 'to': RECIPIENT, 'data': b'\x01'}))
    monkeypatch.setattr(pool.rpc, 'contract_call', Mock(return_value=True))
    return instance


def test_deposit_builds_an_unsigned_native_transaction(tornado: pool.Tornado, note: Note) -> None:
    tx = tornado.deposit_tx(note.commitment, ACCOUNT)
    assert tx == {'from': ACCOUNT, 'to': RECIPIENT, 'chainId': 1, 'nonce': 0, 'value': 10 ** 18, 'data': b'\x01'}
    tornado.note_deposited.assert_not_called()
    pool.rpc.contract_call.assert_not_called()
    tornado.connection.sign_transaction.assert_not_called()
    tornado.connection.send_transaction.assert_not_called()


def test_deposit_reports_only_the_steps_it_runs(tornado: pool.Tornado, note: Note) -> None:
    messages: list[str] = []
    tornado.handlers = [SimpleNamespace(on_progress=messages.append)]

    assert tornado.deposit_tx(note.commitment, ACCOUNT) is not None
    assert messages == ['Fetching deposit transaction nonce', 'Building deposit transaction']
    messages.clear()
    assert tornado.deposit_tx(note.commitment, ACCOUNT, nonce=7) is not None
    assert messages == ['Building deposit transaction']
    tornado.connection.nonce.assert_called_once_with(ACCOUNT)


def test_progress_uses_a_handler_snapshot(tornado: pool.Tornado) -> None:
    observe: Mock = Mock()
    tornado.handlers = [SimpleNamespace(on_progress=lambda _: tornado.handlers.clear()), SimpleNamespace(on_progress=observe)]
    tornado._progress('Building deposit transaction')
    observe.assert_called_once_with('Building deposit transaction')
    assert not tornado.handlers


def test_erc20_builds_without_preflight_queries_or_automatic_approval(tornado: pool.Tornado, note: Note) -> None:
    tornado.symbol = Symbol.DAI
    tx = tornado.deposit_tx(note.commitment, ACCOUNT)
    assert 'value' not in tx
    assert pool.rpc.contract_build_tx.call_args.args[1] == 'deposit'
    pool.rpc.contract_build_tx.assert_called_once()
    pool.rpc.contract_call.assert_not_called()
    tornado.note_deposited.assert_not_called()
    tornado.connection.sign_transaction.assert_not_called()
    tornado.connection.send_transaction.assert_not_called()
    assert tornado.approve_tx(ACCOUNT)['chainId'] == 1
    assert pool.rpc.contract_build_tx.call_args.args[1] == 'approve'


@pytest.mark.parametrize('method', ['approve', 'deposit'])
@pytest.mark.parametrize('reason', [None, 'This note has already been spent'])
def test_build_failure_preserves_rpc_error_and_never_signs(tornado: pool.Tornado, note: Note,
        monkeypatch: pytest.MonkeyPatch, method: str, reason: str | None) -> None:
    error = pool.rpc.Error(pool.rpc.ErrorCode.EXECUTION_REVERTED, (), 'Raw RPC details', reason)
    monkeypatch.setattr(pool.rpc, 'contract_build_tx', Mock(return_value=error))
    logged = Mock()
    monkeypatch.setattr(pool.log, 'error', logged)
    unsigned = {'approve': (ACCOUNT,), 'deposit': (note.commitment, ACCOUNT),
                'withdraw': (note, ACCOUNT, RECIPIENT, MerkleProof(HASH), tornado.zksnark.prove.return_value)}
    assert getattr(tornado, method + '_tx')(*unsigned[method]) is error
    key = Key('11' * 32)
    signed = {'approve': (key,), 'deposit': (note.commitment, key), 'withdraw': (note, RECIPIENT, key)}
    assert getattr(tornado, method)(*signed[method]) is None
    tornado.connection.sign_transaction.assert_not_called()
    tornado.connection.send_transaction.assert_not_called()
    assert all(call.args[1].endswith('failed to build transaction, RPC error: EXECUTION_REVERTED')
               for call in logged.call_args_list)
    assert logged.call_count == 2 and error.msg == 'Raw RPC details'


@pytest.mark.parametrize('method', ['approve', 'deposit'])
def test_missing_unsigned_transaction_never_signs(tornado: pool.Tornado, note: Note,
        monkeypatch: pytest.MonkeyPatch, method: str) -> None:
    monkeypatch.setattr(tornado, method + '_tx', Mock(return_value=None))
    key = Key('11' * 32)
    args = {'approve': (key,), 'deposit': (note.commitment, key), 'withdraw': (note, RECIPIENT, key)}
    assert getattr(tornado, method)(*args[method]) is None
    tornado.connection.sign_transaction.assert_not_called()
    tornado.connection.send_transaction.assert_not_called()


@pytest.mark.parametrize('method', ['approve', 'deposit'])
@pytest.mark.parametrize('reason', [None, 'Insufficient token allowance'])
def test_send_failure_hides_revert_reason_in_cli_logs(tornado: pool.Tornado, note: Note,
        monkeypatch: pytest.MonkeyPatch, method: str, reason: str | None) -> None:
    error = pool.rpc.Error(pool.rpc.ErrorCode.EXECUTION_REVERTED, (), 'Raw RPC details', reason)
    tornado.connection.send_transaction.return_value = error
    logged = Mock()
    monkeypatch.setattr(pool.log, 'error', logged)
    key = Key('11' * 32)
    args = {'approve': (key,), 'deposit': (note.commitment, key), 'withdraw': (note, RECIPIENT, key)}
    assert getattr(tornado, method)(*args[method]) is None
    tornado.connection.sign_transaction.assert_called_once()
    tornado.connection.send_transaction.assert_called_once()
    message = logged.call_args.args[1]
    assert message.endswith('failed to send transaction, RPC error: EXECUTION_REVERTED')
    assert 'Raw RPC details' not in message
    assert error.msg == 'Raw RPC details'


def test_tree_initialization_forwards_real_layer_progress(tornado: pool.Tornado, note: Note, monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    tornado.handlers = [SimpleNamespace(on_progress=messages.append)]
    tornado.sync_only, tornado.tree = True, None
    tornado.db.get_commitments.return_value = [note.commitment]
    monkeypatch.setattr(pool.config, 'MERKLE_TREE_HEIGHT', 3)
    assert tornado.init(sync_only=False)
    assert messages == ['Reading commitments from database', 'Preparing Merkle tree',
                        'Rebuilding Merkle tree: level 1/3 · 0/1 nodes', 'Rebuilding Merkle tree: level 1/3 · 1/1 nodes',
                        'Rebuilding Merkle tree: level 2/3 · 0/1 nodes', 'Rebuilding Merkle tree: level 2/3 · 1/1 nodes',
                        'Rebuilding Merkle tree: level 3/3 · 0/1 nodes', 'Rebuilding Merkle tree: level 3/3 · 1/1 nodes',
                        'Merkle tree ready']
    assert tornado.tree.size() == 1 and not tornado.sync_only


@pytest.mark.parametrize('with_deposit', [False, True])
def test_sync_records_update_time_before_notifying(tornado: pool.Tornado, monkeypatch: pytest.MonkeyPatch, with_deposit: bool) -> None:
    updated: Second = Second(1234)
    clock: Mock = Mock(return_value=updated)
    handler: Mock = Mock(side_effect=lambda *args: observed.append(tornado.last_update_sec))
    observed: list[Second | None] = []
    tornado.handlers = [SimpleNamespace(on_sync=handler)]
    monkeypatch.setattr(pool.util, 'unix_timestamp', clock)
    deposits: list = [SimpleNamespace(commitment=HexBytes('0x55'))] if with_deposit else []
    tornado.on_sync(100, 101, deposits, [])
    assert tornado.last_update_sec == updated
    assert observed == [updated]
    clock.assert_called_once_with()
    handler.assert_called_once_with(100, 101, deposits, [])


@pytest.mark.parametrize('failure', ['database', 'tree', 'rejected_append'])
def test_failed_sync_keeps_previous_update_time(tornado: pool.Tornado, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    tornado.last_update_sec = Second(1234)
    clock: Mock = Mock(return_value=Second(5678))
    handler: Mock = Mock()
    tornado.handlers = [SimpleNamespace(on_sync=handler)]
    monkeypatch.setattr(pool.util, 'unix_timestamp', clock)
    if failure == 'database':
        tornado.db.add_synchronized.return_value = False
    elif failure == 'tree':
        tornado.tree.add.side_effect = RuntimeError('tree update failed')
    else:
        tornado.tree.add.return_value = False
    with pytest.raises(RuntimeError):
        tornado.on_sync(100, 101, [SimpleNamespace(commitment=HexBytes('0x55'))], [])
    assert tornado.last_update_sec == Second(1234)
    clock.assert_not_called()
    handler.assert_not_called()


@pytest.mark.parametrize('failure', [None, ValueError('invalid leaves')])
def test_failed_full_init_keeps_sync_only_mode(tornado: pool.Tornado, monkeypatch: pytest.MonkeyPatch, failure: ValueError | None) -> None:
    tornado.sync_only, tornado.tree = True, None
    if failure is None:
        tornado.db.get_commitments.return_value = None
    else:
        monkeypatch.setattr(pool.merkle_tree, 'create', Mock(side_effect=failure))
    assert not tornado.init(sync_only=False)
    assert tornado.initialized and tornado.sync_only and tornado.tree is None
    tornado.db.close.assert_not_called()


def test_concurrent_full_init_builds_only_one_tree(tornado: pool.Tornado, monkeypatch: pytest.MonkeyPatch) -> None:
    tree, tornado.tree = tornado.tree, None
    tornado.sync_only = True
    create = Mock(return_value=tree)
    monkeypatch.setattr(pool.merkle_tree, 'create', create)
    with ThreadPoolExecutor(max_workers=2) as workers:
        assert all(workers.map(lambda _: tornado.init(sync_only=False), range(2)))
    create.assert_called_once()
    assert tornado.tree is tree and not tornado.sync_only


@pytest.mark.parametrize('sync_only', [True, False])
def test_initialization_builds_tree_only_when_explicitly_requested(tornado: pool.Tornado, monkeypatch: pytest.MonkeyPatch, sync_only: bool) -> None:
    tree, tornado.tree = tornado.tree, None
    tornado.initialized = False
    tornado.connection.started = Mock(return_value=True)
    tornado.db.open, tornado.db.check_integrity = Mock(return_value=True), Mock(return_value=True)
    tornado.poller = SimpleNamespace(add_handler=Mock())
    create = Mock(return_value=tree)
    monkeypatch.setattr(pool.merkle_tree, 'create', create)
    assert tornado.init(sync_only=sync_only)
    assert tornado.sync_only == sync_only
    assert create.call_count == (0 if sync_only else 1)
    assert tornado.init(sync_only=False)
    assert tornado.tree is tree and not tornado.sync_only
    create.assert_called_once()
    tornado.poller.add_handler.assert_called_once_with(tornado)


def relayer_requests(tornado: pool.Tornado, monkeypatch: pytest.MonkeyPatch, reward_account: str = RECIPIENT) -> tuple[Mock, Mock]:
    status: dict[str, Any] = {'netId': 1, 'rewardAccount': reward_account,
              'instances': {'eth': {'instanceAddress': {'1': ACCOUNT}}}}
    get : Mock = Mock(side_effect=[
        SimpleNamespace(status_code=200, json=lambda: status),
        SimpleNamespace(status_code=200, json=lambda: {'status': 'CONFIRMED', 'txHash': HASH.to_0x_hex()}),
    ])
    post: Mock = Mock(return_value=SimpleNamespace(status_code=200, json=lambda: {'id': 'job'}))
    monkeypatch.setattr(relayer.requests, 'get', get)
    monkeypatch.setattr(relayer.requests, 'post', post)
    monkeypatch.setattr(relayer, 'wait', lambda seconds: None)
    return get, post

