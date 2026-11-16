from __future__ import annotations

from hexbytes import HexBytes
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from typing import Any, Callable
from unittest import mock
from unittest.mock import Mock
import pytest

from blockchain.sync import ChainReader
from util.mytype import ChainID, Key, Note, Symbol, TornadoUnit
import cli


ACCOUNT = '0x' + '11' * 20
RECIPIENT = '0x' + '22' * 20
DEPLOYED = '0x' + '33' * 20
HASH = HexBytes('0x' + '44' * 32)


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> SimpleNamespace:
    instances, connections, notes = [], {}, []
    sync_steps: list[tuple[str, ChainID, Symbol]] = []
    key = object.__new__(Key)
    monkeypatch.setattr(Key, 'eth_address', lambda self: ACCOUNT)
    monkeypatch.setattr(cli.config, 'BACKUP_DIR', str(tmp_path))
    monkeypatch.setattr(cli.config, 'RPC_URLS', {chain: 'mock://' + chain.name for chain in ChainID})
    monkeypatch.setattr(cli.helper_func, 'wait', lambda _: None)
    monkeypatch.setattr(cli.log, 'info', Mock())
    monkeypatch.setattr(cli.log, 'warn', Mock())
    monkeypatch.setattr(cli.log, 'error', Mock())

    def connection(chain, url):
        result = SimpleNamespace(start=Mock(return_value=True), stop=Mock(),
                                 transaction_receipt=Mock(return_value={'blockNumber': 10, 'status': 1, 'contractAddress': DEPLOYED}),
                                 transaction_revert_reason=Mock(return_value=None))
        connections[chain] = result
        return result

    def tornado(chain: ChainID, symbol: Symbol, unit: TornadoUnit, connection: SimpleNamespace,
                *, reader: ChainReader | None = None) -> SimpleNamespace:
        def start_sync(*, start_gate: Event) -> bool:
            assert not start_gate.is_set()
            result.start_gate = start_gate
            sync_steps.append(('start', chain, symbol))
            return True

        def wait_sync() -> bool:
            assert result.start_gate.is_set()
            sync_steps.append(('wait', chain, symbol))
            return True

        result: SimpleNamespace = SimpleNamespace(
            chain=chain, symbol=symbol, unit=unit, connection=connection,
            reader=reader, poller=SimpleNamespace(wait_sync=Mock(side_effect=wait_sync)),
            meta=SimpleNamespace(decimals={symbol: 18}), token_contract=object(),
            init=Mock(return_value=True), un_init=Mock(),
            start_sync=Mock(side_effect=start_sync), stop_sync=Mock(),
            add_handler=Mock(), remove_all_handlers=Mock(),
            withdraw=Mock(return_value=HASH), deposit=Mock(return_value=HASH),
        )
        instances.append(result)
        return result

    def note():
        value = len(notes) + 1
        result = SimpleNamespace(commitment=HexBytes(value.to_bytes(32, 'big')),
                                 nullifier_hash=HexBytes((value + 100).to_bytes(32, 'big')))
        notes.append(result)
        return result

    monkeypatch.setattr(cli.rpc, 'Connection', connection)
    monkeypatch.setattr(cli, 'Tornado', tornado)
    monkeypatch.setattr(Note, 'create', note)
    monkeypatch.setattr(Note, 'to_text', lambda chain, symbol, unit, note: 'note-' + note.commitment.hex())
    monkeypatch.setattr(Note, 'to_invoice', lambda chain, symbol, unit, note: 'invoice-' + note.commitment.hex())
    deposit  = Mock(return_value={'kind': 'deposit'})
    withdraw = Mock(return_value={'kind': 'withdraw'})
    approve  = Mock(return_value={'kind': 'approve'})
    send     = Mock(return_value=HASH)
    deploy   = Mock(return_value={'kind': 'deploy'})
    for name, callback in [('deposit_tx', deposit), ('withdraw_tx', withdraw), ('approve_tx', approve), ('send', send),
                           ('deploy_tx', deploy)]:
        monkeypatch.setattr(cli.batch_tx, name, callback)
    return SimpleNamespace(key=key, path=tmp_path, instances=instances, connections=connections, notes=notes,
                           sync_steps=sync_steps,
                           deploy=deploy, deposit=deposit, withdraw=withdraw, approve=approve, send=send)


def test_deposit_saves_every_note_before_one_transaction_per_chain(environment: SimpleNamespace) -> None:
    env = environment

    def build(deposits, account, depositor):
        assert depositor is None
        saved = '\n'.join(path.read_text() for path in env.path.glob('*.txt'))
        assert all('Note: note-' + commitment.hex() in saved for _, commitment in deposits)
        return {'kind': 'deposit'}

    env.deposit.side_effect = build
    cli.deposit_batch(env.key, "{'ethereum':{'eth':{'0.1':2,'1':1}},'bsc':{'bnb':{'0.1':2}}}")
    assert len(list(env.path.glob('*.txt'))) == 5
    assert all(len(path.read_text().splitlines()) == 3 for path in env.path.glob('*.txt'))
    assert [len(call.args[0]) for call in env.deposit.call_args_list] == [3, 2]
    assert env.send.call_count == 2
    env.approve.assert_not_called()
    env.deploy.assert_not_called()
    for instance in env.instances:
        instance.init.assert_called_once_with(sync_only=True)
        instance.un_init.assert_called_once()
        instance.deposit.assert_not_called()
    assert all(connection.stop.call_count == 1 for connection in env.connections.values())
    assert all(connection.transaction_revert_reason.call_count == 0 for connection in env.connections.values())


def test_mixed_deposit_deploys_and_confirms_approvals_before_deposit(environment: SimpleNamespace) -> None:
    env = environment
    events = []

    def deploy(deposits, account):
        assert len(list(env.path.glob('*.txt'))) == 5
        assert account == ACCOUNT
        return {'kind': 'deploy'}

    def approve(deposits, account, symbol, **options):
        assert options == {'depositor': DEPLOYED}
        return {'kind': symbol.value}

    def send(tornado, tx, key):
        events.append(tx['kind'])
        tornado.connection.transaction_receipt.side_effect = receipt
        return HASH

    def receipt(tx_hash):
        contract = DEPLOYED if events[-1] == 'deploy' else None
        events.append('receipt')
        return {'blockNumber': 10, 'status': 1, 'contractAddress': contract}

    def deposit(deposits, account, depositor):
        assert depositor == DEPLOYED
        events.append('build deposit')
        return {'kind': 'deposit'}

    env.deploy.side_effect = deploy
    env.approve.side_effect = approve
    env.send.side_effect = send
    env.deposit.side_effect = deposit
    cli.deposit_batch(env.key, "{'ethereum':{'eth':{'1':1},'dai':{'100':2,'1000':1},'usdt':{'100':1}}}")
    assert events == ['deploy', 'receipt', 'dai', 'receipt', 'usdt', 'receipt', 'build deposit', 'deposit', 'receipt']
    deposits = env.deposit.call_args.args[0]
    env.deploy.assert_called_once_with(deposits, ACCOUNT)
    assert env.approve.call_args_list == [
        mock.call(deposits, ACCOUNT, Symbol.DAI, depositor=DEPLOYED),
        mock.call(deposits, ACCOUNT, Symbol.USDT, depositor=DEPLOYED),
    ]
    assert cli.batch_tx.token_amounts(deposits) == {Symbol.DAI: 1200 * 10 ** 18, Symbol.USDT: 100 * 10 ** 18}
    assert any(DEPLOYED in str(message.args) for message in cli.log.info.call_args_list)


def test_approval_build_failure_keeps_the_deployed_contract_and_notes(environment: SimpleNamespace) -> None:
    env = environment
    env.approve.side_effect = [{'kind': 'approve'}, RuntimeError('Could not query the token allowance.')]
    cli.deposit_batch(env.key, "{'ethereum':{'dai':{'100':1},'usdt':{'100':1}}}")
    assert env.deploy.call_count == 1 and env.send.call_count == 2
    assert env.approve.call_count == 2
    env.deposit.assert_not_called()
    assert len(list(env.path.glob('*.txt'))) == 2
    assert 'Could not query the token allowance' in cli.log.error.call_args.args[1]


@pytest.mark.parametrize('operation', ['deploy', 'approve'])
@pytest.mark.parametrize('failure', ['signing', 'receipt'])
def test_preparation_failure_stops_deposits(environment: SimpleNamespace, operation: str, failure: str) -> None:
    env = environment

    def send(tornado, tx, key):
        if tx['kind'] == operation and failure == 'signing':
            return None
        tornado.connection.transaction_receipt.return_value = {
            'blockNumber': 10, 'status': 0 if tx['kind'] == operation else 1, 'contractAddress': DEPLOYED,
        }
        return HASH

    env.send.side_effect = send
    cli.deposit_batch(env.key, "{'ethereum':{'dai':{'100':2}}}")
    env.deposit.assert_not_called()
    if operation == 'deploy':
        env.approve.assert_not_called()
    assert len(list(env.path.glob('*.txt'))) == 2
    assert env.instances[0].un_init.call_count == 1
    assert env.connections[ChainID.ETHEREUM].stop.call_count == 1


@pytest.mark.parametrize('operation', ['deploy', 'approve', 'deposit'])
def test_erc20_batch_receipt_timeout_keeps_notes(environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
                                               operation: str) -> None:
    env = environment
    elapsed = 0

    def wait(seconds):
        nonlocal elapsed
        elapsed += seconds

    def send(tornado, tx, key):
        receipt = None if tx['kind'] == operation else {'blockNumber': 10, 'status': 1, 'contractAddress': DEPLOYED}
        tornado.connection.transaction_receipt.return_value = receipt
        return HASH

    monkeypatch.setattr(cli, 'monotonic', lambda: elapsed)
    monkeypatch.setattr(cli.helper_func, 'wait', wait)
    env.send.side_effect = send
    cli.deposit_batch(env.key, "{'ethereum':{'dai':{'100':2}}}")
    assert elapsed == 120 + 15 * ['deploy', 'approve', 'deposit'].index(operation)
    assert env.deposit.call_count == (1 if operation == 'deposit' else 0)
    assert len(list(env.path.glob('*.txt'))) == 2
    message = cli.log.error.call_args.args[1]
    assert 'Timed out' in message and HASH.to_0x_hex() in message and 'before retrying' in message


@pytest.mark.parametrize('symbol,unit', [('eth', '0.1'), ('dai', '100')])
def test_failed_batch_receipt_logs_failure_and_keeps_notes(environment: SimpleNamespace, symbol: str,
                                                          unit: str) -> None:
    env = environment

    def send(tornado, transaction, key):
        tornado.connection.transaction_receipt.return_value = {
            'blockNumber': 10, 'status': int(transaction['kind'] != 'deposit'), 'contractAddress': DEPLOYED,
        }
        return HASH

    env.send.side_effect = send
    cli.deposit_batch(env.key, str({'ethereum': {symbol: {unit: 2}}}))
    connection = env.connections[ChainID.ETHEREUM]
    connection.transaction_revert_reason.assert_not_called()
    message = cli.log.error.call_args.args[1]
    assert message == f'Deposit transaction failed, tx_hash: {HASH.to_0x_hex()}'
    assert not any('Deposit transaction mined' in str(call.args) for call in cli.log.info.call_args_list)
    assert len(list(env.path.glob('*.txt'))) == 2
    assert all(instance.un_init.call_count == 1 for instance in env.instances)
    connection.stop.assert_called_once()


@pytest.mark.parametrize('contract_address', [None, 'invalid'])
def test_missing_deployment_address_stops_before_approval(environment: SimpleNamespace, contract_address: str | None) -> None:
    env = environment

    def send(tornado, tx, key):
        tornado.connection.transaction_receipt.return_value = {
            'blockNumber': 10, 'status': 1, 'contractAddress': contract_address,
        }
        return HASH

    env.send.side_effect = send
    cli.deposit_batch(env.key, "{'ethereum':{'dai':{'100':2}},'bsc':{'bnb':{'0.1':1}}}")
    assert env.send.call_count == 1
    env.approve.assert_not_called()
    env.deposit.assert_not_called()
    assert len(list(env.path.glob('*.txt'))) == 2
    assert 'Missing batch contract address' in cli.log.error.call_args.args[1]


def test_backup_failure_does_not_approve_or_send(environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, '_save_deposit_note', Mock(return_value=None))
    cli.deposit_batch(environment.key, "{'ethereum':{'eth':{'1':2}}}")
    environment.deposit.assert_not_called()
    environment.approve.assert_not_called()
    environment.send.assert_not_called()
    environment.instances[0].un_init.assert_called_once()


def test_failed_pool_initialization_aborts_chain_and_cleans_up(environment: SimpleNamespace,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    factory = cli.Tornado

    def fail(*args):
        result = factory(*args)
        result.init.return_value = False
        return result

    monkeypatch.setattr(cli, 'Tornado', fail)
    cli.deposit_batch(env.key, "{'ethereum':{'eth':{'1':2}}}")
    env.send.assert_not_called()
    env.instances[0].un_init.assert_called_once()
    env.connections[ChainID.ETHEREUM].stop.assert_called_once()


def _withdrawal_notes(monkeypatch: pytest.MonkeyPatch) -> None:
    records = {
        'one': (ChainID.ETHEREUM, Symbol.ETH, TornadoUnit.UNIT_1, Note.create()),
        'two': (ChainID.ETHEREUM, Symbol.ETH, TornadoUnit.UNIT_1, Note.create()),
        'three': (ChainID.ETHEREUM, Symbol.DAI, TornadoUnit.UNIT_100, Note.create()),
        'four': (ChainID.BSC, Symbol.BNB, TornadoUnit.UNIT_0_1, Note.create()),
    }
    monkeypatch.setattr(Note, 'from_text', records.get)


def test_wallet_withdraw_combines_pools_and_splits_chains(environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    _withdrawal_notes(monkeypatch)
    cli.withdraw_batch('one,two,three,four', RECIPIENT, env.key)
    assert [len(call.args[0]) for call in env.withdraw.call_args_list] == [3, 1]
    assert all(call.args[1:] == (ACCOUNT, RECIPIENT) for call in env.withdraw.call_args_list)
    assert env.send.call_count == 2
    eth, dai, bnb = env.instances
    assert eth.reader is dai.reader and eth.reader is not bnb.reader
    assert eth.start_gate is dai.start_gate and eth.start_gate is not bnb.start_gate
    assert env.sync_steps == [
        ('start', ChainID.ETHEREUM, Symbol.ETH), ('start', ChainID.ETHEREUM, Symbol.DAI),
        ('wait', ChainID.ETHEREUM, Symbol.ETH), ('wait', ChainID.ETHEREUM, Symbol.DAI),
        ('start', ChainID.BSC, Symbol.BNB), ('wait', ChainID.BSC, Symbol.BNB),
    ]
    assert all(instance.withdraw.call_count == 0 and instance.un_init.call_count == 1 for instance in env.instances)
    assert all(connection.stop.call_count == 1 for connection in env.connections.values())


def test_relayer_withdraw_still_submits_each_note(environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    _withdrawal_notes(monkeypatch)
    cli.withdraw_batch('one,two,three', RECIPIENT, 'https://relayer.example')
    assert [instance.withdraw.call_count for instance in env.instances] == [2, 1]
    assert all(call.args[1:] == (RECIPIENT, 'https://relayer.example')
               for instance in env.instances for call in instance.withdraw.call_args_list)
    env.withdraw.assert_not_called()
    env.send.assert_not_called()
    assert env.connections[ChainID.ETHEREUM].transaction_receipt.call_count == 3
    env.connections[ChainID.ETHEREUM].transaction_revert_reason.assert_not_called()
    assert env.instances[0].reader is env.instances[1].reader
    assert env.sync_steps == [
        ('start', ChainID.ETHEREUM, Symbol.ETH), ('start', ChainID.ETHEREUM, Symbol.DAI),
        ('wait', ChainID.ETHEREUM, Symbol.ETH), ('wait', ChainID.ETHEREUM, Symbol.DAI),
    ]


def test_sync_all_pools_are_started_with_shared_sources(environment: SimpleNamespace,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.helper_func, 'load_metadata', lambda: {
        ChainID.ETHEREUM: SimpleNamespace(deployment={Symbol.ETH: {TornadoUnit.UNIT_1: None},
                                                     Symbol.DAI: {TornadoUnit.UNIT_100: None}}),
        ChainID.BSC: SimpleNamespace(deployment={Symbol.BNB: {TornadoUnit.UNIT_1: None}}),
    })
    cli.sync_all_events(keep=False)
    eth, dai, bnb = environment.instances
    assert eth.reader is dai.reader and eth.reader is not bnb.reader
    assert eth.start_gate is dai.start_gate is bnb.start_gate
    assert [action for action, _, _ in environment.sync_steps] == ['start'] * 3 + ['wait'] * 3
    for instance in environment.instances:
        instance.init.assert_called_once_with(sync_only=True)
        instance.stop_sync.assert_called_once()
        instance.un_init.assert_called_once()
    assert all(connection.stop.call_count == 1 for connection in environment.connections.values())


def test_interrupt_during_batch_sync_start_releases_workers(environment: SimpleNamespace,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    _withdrawal_notes(monkeypatch)
    create: Callable[..., SimpleNamespace] = cli.Tornado

    def create_interrupted_pool(*args: Any, **kwargs: Any) -> SimpleNamespace:
        pool: SimpleNamespace = create(*args, **kwargs)
        if pool.symbol == Symbol.DAI:
            pool.start_sync.side_effect = KeyboardInterrupt
        else:
            def stop() -> None:
                assert pool.start_gate.is_set()
            pool.stop_sync.side_effect = stop
        return pool

    monkeypatch.setattr(cli, 'Tornado', create_interrupted_pool)
    cli.withdraw_batch('one,three', RECIPIENT, environment.key)
    eth, dai = environment.instances
    eth.stop_sync.assert_called_once()
    dai.stop_sync.assert_not_called()
    assert all(pool.un_init.call_count == 1 for pool in environment.instances)
    environment.connections[ChainID.ETHEREUM].stop.assert_called_once()
    environment.withdraw.assert_not_called()
    environment.send.assert_not_called()


@pytest.mark.parametrize('relayed', [False, True])
def test_failed_sync_prevents_any_batch_withdrawal(
    environment: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, relayed: bool,
) -> None:
    _withdrawal_notes(monkeypatch)
    create: Callable[..., SimpleNamespace] = cli.Tornado

    def create_pool(*args: Any, **kwargs: Any) -> SimpleNamespace:
        tornado: SimpleNamespace = create(*args, **kwargs)
        if tornado.symbol == Symbol.ETH:
            tornado.poller.wait_sync.side_effect = None
            tornado.poller.wait_sync.return_value = False
        return tornado

    monkeypatch.setattr(cli, 'Tornado', create_pool)
    cli.withdraw_batch('one,three', RECIPIENT, 'https://relayer.example' if relayed else environment.key)
    environment.withdraw.assert_not_called()
    environment.send.assert_not_called()
    for tornado in environment.instances:
        tornado.withdraw.assert_not_called()
        tornado.stop_sync.assert_called_once()
        tornado.un_init.assert_called_once()
    environment.connections[ChainID.ETHEREUM].stop.assert_called_once()


def test_sync_keep_waits_for_healthy_pool_after_another_stops(environment: SimpleNamespace,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    connection: SimpleNamespace = cli.rpc.Connection(ChainID.ETHEREUM, 'mock://ETHEREUM')
    eth       : SimpleNamespace = cli.Tornado(ChainID.ETHEREUM, Symbol.ETH, TornadoUnit.UNIT_1, connection)
    dai       : SimpleNamespace = cli.Tornado(ChainID.ETHEREUM, Symbol.DAI, TornadoUnit.UNIT_100, connection)
    eth.poller.is_started = Mock(return_value=False)
    dai.poller.is_started = Mock(side_effect=[True, False])
    sleep: Mock = Mock()
    monkeypatch.setattr(cli, 'sleep', sleep)

    assert not cli._sync_pools([eth, dai], keep=True)
    cli.log.error.assert_called_once_with(cli.tag, 'Pool synchronization stopped.')
    sleep.assert_called_once_with(0.5)
    for pool in (eth, dai):
        pool.stop_sync.assert_called_once()


def test_relayer_withdraw_failed_receipt_reports_failure_and_cleans_up(environment: SimpleNamespace,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    env = environment
    _withdrawal_notes(monkeypatch)
    factory = cli.rpc.Connection

    def connection(*args):
        result = factory(*args)
        result.transaction_receipt.return_value = {'blockNumber': 10, 'status': 0}
        return result

    monkeypatch.setattr(cli.rpc, 'Connection', connection)
    cli.withdraw_batch('one', RECIPIENT, 'https://relayer.example')
    current = env.connections[ChainID.ETHEREUM]
    current.transaction_receipt.assert_called_once_with(HASH)
    current.transaction_revert_reason.assert_not_called()
    message = cli.log.error.call_args.args[1]
    assert message == f'Withdraw transaction failed, tx_hash: {HASH.to_0x_hex()}'
    assert not any('Withdraw transaction mined' in str(call.args) for call in cli.log.info.call_args_list)
    env.instances[0].un_init.assert_called_once()
    current.stop.assert_called_once()


def test_single_deposit_keeps_original_backup_failure_behavior(environment: SimpleNamespace) -> None:
    env = environment
    instance = cli.Tornado(ChainID.ETHEREUM, Symbol.ETH, TornadoUnit.UNIT_1, SimpleNamespace())
    instance.deposit.return_value = None
    assert cli._deposit(env.key, instance) is None
    assert not list(env.path.glob('*.txt'))
    assert any('IMPORTANT:' in str(call.args) for call in cli.log.info.call_args_list)
