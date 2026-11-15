from unittest.mock import Mock
from threading import Event

from eth_abi import encode
from hexbytes import HexBytes
import pytest
from web3.exceptions import ContractCustomError, ContractLogicError, ContractPanicError

from blockchain import rpc
from util.mytype import ChainID


HASH = HexBytes('0x' + '11' * 32)
REASON = '0x08c379a0' + encode(['string'], ['Token mismatch']).hex()
PANIC = '0x4e487b71' + encode(['uint256'], [0x11]).hex()
CUSTOM = '0x12345678' + encode(['address'], ['0x' + '22' * 20]).hex()


@pytest.mark.parametrize(('data', 'expected'), [
    (REASON, 'Token mismatch'),
    (HexBytes(REASON), 'Token mismatch'),
    ('0x08c379a0' + encode(['string'], ['']).hex(), 'Execution reverted with an empty reason.'),
    (PANIC, 'Panic(0x11): arithmetic overflow or underflow'),
    ('0x4e487b71' + encode(['uint256'], [0xff]).hex(), 'Panic(0xff): unknown panic code'),
    (CUSTOM, f'Custom error 0x12345678 (data: {CUSTOM})'),
    ('0x12345678', 'Custom error 0x12345678 (data: 0x12345678)'),
    (None, None), ('0x', None), ('0x12', 'Revert data: 0x12'), ('invalid', None),
    ('0x08c379a0', 'Revert data: 0x08c379a0'), ('0x4e487b71ff', 'Revert data: 0x4e487b71ff'),
])
def test_decode_revert(data, expected):
    assert rpc.decode_revert(data) == expected


@pytest.mark.parametrize('exception', [
    ContractLogicError('execution reverted: Token mismatch', REASON),
    ContractPanicError('Panic error 0x11', PANIC),
    ContractCustomError(CUSTOM, CUSTOM),
])
@pytest.mark.parametrize('operation', ['call', 'build', 'constructor'])
def test_contract_errors_preserve_raw_message_and_decode_reason(exception, operation):
    contract = Mock(address='0x' + '33' * 20)
    contract.get_function_by_name.return_value.return_value.call.side_effect = exception
    contract.get_function_by_name.return_value.return_value.build_transaction.side_effect = exception
    contract.constructor.return_value.build_transaction.side_effect = exception
    result = rpc.contract_call(contract, 'deposit') if operation == 'call' else rpc.contract_build_tx(
        contract, None if operation == 'constructor' else 'deposit', {},
    )
    assert isinstance(result, rpc.Error)
    assert result.code == rpc.ErrorCode.EXECUTION_REVERTED
    assert result.msg == str(exception)
    assert result.reason == rpc.decode_revert(exception.data)
    assert str(result) == 'EXECUTION_REVERTED'


def test_contract_error_without_decodable_data_uses_its_message():
    contract = Mock(address='0x' + '33' * 20)
    error = ContractLogicError('execution reverted: Pool rejected deposit', '0x')
    contract.get_function_by_name.return_value.return_value.call.side_effect = error
    result = rpc.contract_call(contract, 'deposit')
    assert result.reason == error.message and result.msg == str(error)
    assert str(rpc.Error(rpc.ErrorCode.UNKNOWN, (), 'raw')) == 'UNKNOWN'


@pytest.fixture
def connection(monkeypatch):
    instance = rpc.Connection(ChainID.SEPOLIA, 'http://127.0.0.1:1')
    assert instance.start()
    monkeypatch.setattr(instance.w3.manager, 'request_blocking', Mock())
    try:
        yield instance
    finally:
        instance.stop()


@pytest.mark.parametrize(('trace', 'expected'), [
    ({'error': 'execution reverted', 'output': REASON}, 'Token mismatch'),
    ({'error': 'execution reverted', 'output': PANIC}, 'Panic(0x11): arithmetic overflow or underflow'),
    ({'error': 'execution reverted', 'output': CUSTOM}, f'Custom error 0x12345678 (data: {CUSTOM})'),
    ({'error': 'execution reverted', 'output': '0x'}, 'Execution reverted without return data.'),
    ({'error': 'execution reverted', 'output': '0x08c379a0'}, 'Revert data: 0x08c379a0'),
    ({'error': 'execution reverted', 'output': '0x12'}, 'Revert data: 0x12'),
    ({'error': 'execution reverted', 'output': '0x08c379a0' + encode(['string'], ['']).hex()},
     'Execution reverted with an empty reason.'),
    ({'error': 'execution reverted', 'revertReason': 'Token mismatch'}, 'Token mismatch'),
    ({'error': 'out of gas', 'output': '0x'}, 'out of gas'),
    ({'error': 'execution reverted', 'calls': [{'output': REASON}]}, 'Execution reverted without return data.'),
    ({}, None), (None, None), ({'revertReason': 42, 'output': {}}, None),
])
def test_transaction_revert_reason_uses_only_original_top_call(connection, trace, expected):
    request = connection.w3.manager.request_blocking
    request.return_value = trace
    assert connection.transaction_revert_reason(HASH) == expected
    request.assert_called_once_with('debug_traceTransaction', [
        HASH.to_0x_hex(), {'tracer': 'callTracer', 'tracerConfig': {'onlyTopCall': True}, 'timeout': '5s'},
    ])


def test_unsupported_trace_is_optional_and_logged(connection, monkeypatch):
    debug = Mock()
    monkeypatch.setattr(rpc.log, 'debug', debug)
    connection.w3.manager.request_blocking.side_effect = RuntimeError('Method not supported')
    assert connection.transaction_revert_reason(HASH) is None
    assert debug.called


def test_stopped_connection_does_not_query_trace(connection):
    request = connection.w3.manager.request_blocking
    connection.stop()
    assert connection.transaction_revert_reason(HASH) is None
    request.assert_not_called()


@pytest.mark.parametrize('asynchronous', [False, True])
def test_rpc_worker_preserves_contract_revert_reason(connection, asynchronous):
    error = ContractLogicError('execution reverted: Token mismatch', REASON)

    def action():
        raise error

    if asynchronous:
        done, results = Event(), []

        def callback(result):
            results.append(result)
            done.set()

        connection.run_async(action, 'test', (), callback)
        assert done.wait(3)
        result = results[0]
    else:
        result = connection.run_sync(action, 'test', ())
    assert result.reason == 'Token mismatch'
    assert result.msg == str(error)
    assert str(result) == 'EXECUTION_REVERTED'
