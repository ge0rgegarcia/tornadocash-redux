from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from hexbytes import HexBytes
from threading import Event
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock
from web3.types import LogReceipt
import pytest

from blockchain import rpc, sync
from util.mytype import ChainID


POOL_A       : str                  = '0x' + '00' * 19 + 'a1'
POOL_B       : str                  = '0x' + '00' * 19 + 'b2'
OTHER_POOL   : str                  = '0x' + '00' * 19 + 'c3'
DEPOSIT      : HexBytes             = sync.EventDeposit.event_hash()
WITHDRAWAL   : HexBytes             = sync.EventWithdraw.event_hash()
TOPICS       : tuple[HexBytes, ...] = (DEPOSIT, WITHDRAWAL)
FILTER_TOPICS: list[list[HexBytes]] = [list(TOPICS)]


@pytest.fixture
def connection() -> SimpleNamespace:
    return SimpleNamespace(latest_block_number=Mock(return_value=8), log_receipt=Mock(return_value=[]))


def receipt(address: str, block: int, index: int, topic: HexBytes = DEPOSIT) -> LogReceipt:
    return {
        'address': address,
        'blockHash': HexBytes(block.to_bytes(32, 'big')),
        'blockNumber': block,
        'data': HexBytes(b''),
        'logIndex': index,
        'removed': False,
        'topics': [topic],
        'transactionHash': HexBytes(bytes(32)),
        'transactionIndex': 0,
    }


def test_pools_share_head_and_log_fetch_but_receive_only_their_ordered_logs(connection: SimpleNamespace) -> None:
    first     : LogReceipt = receipt(POOL_A, 5, 0)
    second    : LogReceipt = receipt(POOL_A.replace('a1', 'A1'), 5, 2)
    withdrawal: LogReceipt = receipt(POOL_A, 6, 0, WITHDRAWAL)
    other     : LogReceipt = receipt(POOL_B, 5, 1)
    connection.log_receipt.return_value = [withdrawal, second, other, first]
    reader: sync.ChainReader = sync.ChainReader(connection)
    reader.register(POOL_A, 5)
    reader.register(POOL_B, 5)
    reader.register(OTHER_POOL, 6)

    assert reader.latest_block_number(POOL_A) == 8
    assert reader.get_logs(POOL_A, 5, 8, TOPICS) == [first, second, withdrawal]
    reader.persisted(POOL_A, 9)

    assert reader.latest_block_number(POOL_B) == 8
    assert reader.get_logs(POOL_B, 5, 8, TOPICS) == [other]
    connection.latest_block_number.assert_called_once_with()
    fetched, start, end, topics = connection.log_receipt.call_args.args
    assert set(fetched) == {POOL_A, POOL_B}
    assert (start, end, topics) == (5, 8, FILTER_TOPICS)
    assert connection.log_receipt.call_count == 1


def test_start_gate_registers_every_pool_before_the_first_rpc(connection: SimpleNamespace) -> None:
    connection.started = lambda: True
    queried: Event = Event()
    connection.log_receipt.side_effect = lambda *_: queried.set() or []
    reader : sync.ChainReader      = sync.ChainReader(connection)
    gate   : Event                 = Event()
    pollers: list[sync.PoolPoller] = [
        sync.PoolPoller(ChainID.SEPOLIA, connection, reader=reader) for _ in range(2)
    ]
    try:
        for poller, pool in zip(pollers, (POOL_A, POOL_B)):
            assert poller.start(pool, 8, start_gate=gate)
        connection.latest_block_number.assert_not_called()
        gate.set()
        assert queried.wait(2)
        connection.latest_block_number.assert_called_once_with()
        addresses, start, end, topics = connection.log_receipt.call_args.args
        assert set(addresses) == {POOL_A, POOL_B}
        assert (start, end, topics) == (8, 8, FILTER_TOPICS)
    finally:
        gate.set()
        for poller in pollers:
            if poller.worker is not None:
                poller.stop()


def test_forced_sync_checks_new_head_even_when_another_pools_head_is_cached(connection: SimpleNamespace) -> None:
    connection.latest_block_number.side_effect = [8, 12]
    reader: sync.ChainReader = sync.ChainReader(connection)
    reader.register(POOL_A, 5)

    assert reader.latest_block_number(POOL_A) == 8
    assert reader.get_logs(POOL_A, 5, 8, TOPICS) == []
    reader.persisted(POOL_A, 9)
    reader.register(POOL_B, 9)
    assert reader.latest_block_number(POOL_B, refresh=True) == 12
    assert connection.latest_block_number.call_count == 2
    assert reader.get_logs(POOL_B, 9, 12, TOPICS) == []
    assert [call.args[1:3] for call in connection.log_receipt.call_args_list] == [(5, 8), (9, 12)]


def test_next_regular_cycle_also_shares_one_fresh_head_and_log_request(connection: SimpleNamespace) -> None:
    connection.latest_block_number.side_effect = [8, 12]
    reader: sync.ChainReader = sync.ChainReader(connection)
    for pool in (POOL_A, POOL_B):
        reader.register(pool, 5)
    for pool in (POOL_A, POOL_B):
        assert reader.latest_block_number(pool) == 8
        assert reader.get_logs(pool, 5, 8, TOPICS) == []
        reader.persisted(pool, 9)

    for pool in (POOL_B, POOL_A):
        assert reader.latest_block_number(pool) == 12
        assert reader.get_logs(pool, 9, 12, TOPICS) == []

    assert connection.latest_block_number.call_count == 2
    calls: list[Any] = connection.log_receipt.call_args_list
    assert len(calls) == 2
    assert all(set(call.args[0]) == {POOL_A, POOL_B} for call in calls)
    assert [call.args[1:] for call in calls] == [(5, 8, FILTER_TOPICS), (9, 12, FILTER_TOPICS)]


@pytest.mark.parametrize('code', [rpc.ErrorCode.UNKNOWN, rpc.ErrorCode.RPC_CALL_RATE_LIMIT])
def test_failed_group_retries_the_same_filter_after_half_a_second(
    connection: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, code: rpc.ErrorCode,
) -> None:
    clock: Mock = Mock(return_value=100.0)
    monkeypatch.setattr(sync, 'monotonic', clock)
    failure: rpc.Error  = rpc.Error(code, (), 'requested range rejected')
    first  : LogReceipt = receipt(POOL_A, 5, 0)
    second : LogReceipt = receipt(POOL_B, 8, 0)
    connection.log_receipt.side_effect = [failure, [first, second]]
    reader: sync.ChainReader = sync.ChainReader(connection)
    for pool in (POOL_A, POOL_B):
        reader.register(pool, 5)
        assert reader.latest_block_number(pool) == 8

    assert reader.get_logs(POOL_A, 5, 8, TOPICS) is failure
    clock.return_value = 100.25
    assert reader.get_logs(POOL_B, 5, 8, TOPICS) is failure
    connection.log_receipt.assert_called_once_with([POOL_A, POOL_B], 5, 8, FILTER_TOPICS)

    clock.return_value = 100.5
    assert reader.get_logs(POOL_A, 5, 8, TOPICS) == [first]
    assert reader.get_logs(POOL_B, 5, 8, TOPICS) == [second]
    assert connection.log_receipt.call_count == 2
    assert connection.log_receipt.call_args_list[0] == connection.log_receipt.call_args_list[1]


def test_pool_restart_discards_an_old_registration_prefetch(connection: SimpleNamespace) -> None:
    old        : LogReceipt = receipt(POOL_B, 8, 0)
    replacement: LogReceipt = receipt(POOL_B, 8, 1)
    connection.log_receipt.side_effect = [[old], [replacement]]
    reader: sync.ChainReader = sync.ChainReader(connection)
    reader.register(POOL_A, 8)
    reader.register(POOL_B, 8)
    assert reader.latest_block_number(POOL_A) == 8
    assert reader.get_logs(POOL_A, 8, 8, TOPICS) == []

    reader.unregister(POOL_B)
    reader.register(POOL_B, 8)
    assert reader.latest_block_number(POOL_B) == 8
    assert reader.get_logs(POOL_B, 8, 8, TOPICS) == [replacement]
    assert connection.log_receipt.call_count == 2


def test_pool_restart_cannot_consume_prefetch_from_an_in_flight_request(connection: SimpleNamespace) -> None:
    request_started: Event      = Event()
    return_old_logs: Event      = Event()
    old            : LogReceipt = receipt(POOL_B, 8, 0)
    replacement    : LogReceipt = receipt(POOL_B, 8, 1)

    def get_logs(addresses: rpc.LogAddresses, start: int, end: int,
                 topics: rpc.LogTopics) -> list[LogReceipt]:
        if isinstance(addresses, str):
            return [replacement]
        request_started.set()
        assert return_old_logs.wait(timeout=5)
        return [old]

    connection.log_receipt.side_effect = get_logs
    reader: sync.ChainReader = sync.ChainReader(connection)
    reader.register(POOL_A, 8)
    reader.register(POOL_B, 8)
    assert reader.latest_block_number(POOL_A) == 8
    with ThreadPoolExecutor(max_workers=1) as worker:
        pending: Future[list[LogReceipt] | rpc.Error] = worker.submit(reader.get_logs, POOL_A, 8, 8, TOPICS)
        try:
            assert request_started.wait(timeout=5)
            reader.unregister(POOL_B)
            reader.register(POOL_B, 8)
        finally:
            return_old_logs.set()
        assert pending.result(timeout=5) == []
    reader.persisted(POOL_A, 9)

    assert reader.latest_block_number(POOL_B) == 8
    assert reader.get_logs(POOL_B, 8, 8, TOPICS) == [replacement]
    assert connection.log_receipt.call_count == 2


def assert_invalid_response(result: list[LogReceipt] | int | rpc.Error) -> None:
    assert isinstance(result, rpc.Error)
    assert result.code is rpc.ErrorCode.RPC_INVALID_RESPONSE


def test_identical_log_rows_are_delivered_once(connection: SimpleNamespace) -> None:
    first : LogReceipt = receipt(POOL_A, 8, 0)
    second: LogReceipt = receipt(POOL_A, 8, 1)
    connection.log_receipt.return_value = [second, first, dict(first)]
    reader: sync.ChainReader = sync.ChainReader(connection)
    reader.register(POOL_A, 8)
    assert reader.latest_block_number(POOL_A) == 8

    assert reader.get_logs(POOL_A, 8, 8, TOPICS) == [first, second]


@pytest.mark.parametrize('response', [
    pytest.param([receipt(POOL_A, 8, 0), receipt(POOL_A, 8, 0, WITHDRAWAL)], id='conflicting-duplicate'),
    pytest.param([{**receipt(POOL_A, 8, 0), 'removed': True}], id='removed-log'),
    pytest.param([receipt(POOL_A, 9, 0)], id='wrong-block'),
    pytest.param([receipt(OTHER_POOL, 8, 0)], id='wrong-address'),
    pytest.param({'result': []}, id='non-list'),
    pytest.param(None, id='null'),
])
def test_invalid_log_response_cannot_look_like_a_successful_range(
    connection: SimpleNamespace, response: object,
) -> None:
    connection.log_receipt.return_value = response
    reader: sync.ChainReader = sync.ChainReader(connection)
    reader.register(POOL_A, 8)
    assert reader.latest_block_number(POOL_A) == 8

    assert_invalid_response(reader.get_logs(POOL_A, 8, 8, TOPICS))


def test_unpersisted_high_head_does_not_block_a_corrected_head(connection: SimpleNamespace) -> None:
    connection.latest_block_number.side_effect = [100, 8]
    reader: sync.ChainReader = sync.ChainReader(connection)
    for pool in (POOL_A, POOL_B):
        reader.register(pool, 8)
    assert reader.latest_block_number(POOL_A) == 100

    assert reader.latest_block_number(POOL_B, refresh=True) == 8
    assert reader.latest_block_number(POOL_A) == 8
    assert reader.get_logs(POOL_A, 8, 8, TOPICS) == []
    connection.log_receipt.assert_called_once_with([POOL_A, POOL_B], 8, 8, FILTER_TOPICS)


def test_head_behind_one_pools_cursor_is_still_usable_by_another_pool(connection: SimpleNamespace) -> None:
    connection.latest_block_number.side_effect = [8, 7]
    reader: sync.ChainReader = sync.ChainReader(connection)
    reader.register(POOL_A, 8)
    assert reader.latest_block_number(POOL_A) == 8
    assert reader.get_logs(POOL_A, 8, 8, TOPICS) == []
    reader.persisted(POOL_A, 9)

    assert_invalid_response(reader.latest_block_number(POOL_A, refresh=True))
    reader.register(POOL_B, 7)
    assert reader.latest_block_number(POOL_B) == 7
    assert connection.latest_block_number.call_count == 2


def test_wrong_chain_never_fetches_a_head_or_logs(
    connection: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock: Mock = Mock(return_value=0.0)
    monkeypatch.setattr(sync, 'monotonic', clock)
    connection.remote_chain_id = Mock(return_value=137)
    reader: sync.ChainReader = sync.ChainReader(connection, expected_chain_id=1)
    for pool in (POOL_A, POOL_B):
        reader.register(pool, 5)
        result: int | rpc.Error = reader.latest_block_number(pool)
        assert isinstance(result, rpc.Error) and result.code is rpc.ErrorCode.RPC_CHAIN_MISMATCH
    connection.remote_chain_id.assert_called_once_with()
    connection.latest_block_number.assert_not_called()
    connection.log_receipt.assert_not_called()

    clock.return_value = 0.5
    connection.remote_chain_id.return_value = 1
    assert reader.latest_block_number(POOL_A) == reader.latest_block_number(POOL_B) == 8
    assert connection.remote_chain_id.call_count == 2
    connection.latest_block_number.assert_called_once_with()


def test_temporary_chain_id_failure_recovers_before_first_head(
    connection: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock: Mock = Mock(return_value=0.0)
    monkeypatch.setattr(sync, 'monotonic', clock)
    failure: rpc.Error = rpc.Error(rpc.ErrorCode.RPC_CALL_RATE_LIMIT, ())
    connection.remote_chain_id = Mock(side_effect=[failure, 1])
    reader: sync.ChainReader = sync.ChainReader(connection, expected_chain_id=1)
    for pool in (POOL_A, POOL_B):
        reader.register(pool, 5)
        result: int | rpc.Error = reader.latest_block_number(pool)
        assert isinstance(result, rpc.Error) and result.code is failure.code

    connection.remote_chain_id.assert_called_once_with()
    connection.latest_block_number.assert_not_called()
    clock.return_value = 0.5
    assert reader.latest_block_number(POOL_A) == reader.latest_block_number(POOL_B) == 8
    assert connection.remote_chain_id.call_count == 2
    connection.latest_block_number.assert_called_once_with()


def test_failed_range_does_not_block_another_pools_range(
    connection: SimpleNamespace, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sync, 'monotonic', lambda: 100.0)
    failure: rpc.Error = rpc.Error(rpc.ErrorCode.UNKNOWN, (), 'range rejected')
    connection.log_receipt.side_effect = [failure, []]
    reader: sync.ChainReader = sync.ChainReader(connection)
    reader.register(POOL_A, 5)
    reader.register(POOL_B, 8)
    assert reader.latest_block_number(POOL_A) == 8

    assert reader.get_logs(POOL_A, 5, 8, TOPICS) is failure
    assert reader.latest_block_number(POOL_B) == 8
    assert reader.get_logs(POOL_B, 8, 8, TOPICS) == []
    assert reader.get_logs(POOL_A, 5, 8, TOPICS) is failure
    assert [call.args for call in connection.log_receipt.call_args_list] == [
        (POOL_A, 5, 8, FILTER_TOPICS), (POOL_B, 8, 8, FILTER_TOPICS),
    ]
