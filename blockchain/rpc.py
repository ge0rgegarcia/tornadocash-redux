from __future__ import annotations

from collections.abc import Mapping, Sequence
from enum import Enum
from eth_abi import decode
from eth_abi.exceptions import DecodingError
from eth_account.datastructures import SignedTransaction
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from requests import Response
from requests.exceptions import SSLError, Timeout
from typing import Any, Callable, TYPE_CHECKING, Union
from web3 import Web3
from web3.contract import Contract
from web3.datastructures import AttributeDict
from web3.eth import Eth
from web3.exceptions import BadResponseFormat, ContractLogicError, TooManyRequests
from web3.middleware import ExtraDataToPOAMiddleware
from web3.types import LogReceipt, Nonce, TxData, TxParams, TxReceipt, Wei
import re
import threading

from util import log
from util.mytype import ChainID, Key

if TYPE_CHECKING:
    from web3 import HTTPProvider
    from web3.providers.rpc.utils import ExceptionRetryConfiguration


LogAddresses = Union[ChecksumAddress, list[ChecksumAddress]]
LogTopics    = Sequence[Union[HexBytes, Sequence[HexBytes], None]]


class ErrorCode(Enum):
    UNKNOWN              = 'UNKNOWN'
    RPC_NOT_STARTED      = 'RPC_NOT_STARTED'
    RPC_CALL_RATE_LIMIT  = 'RPC_CALL_RATE_LIMIT'
    RPC_AUTH_FAILED      = 'RPC_AUTH_FAILED'
    RPC_INVALID_RESPONSE = 'RPC_INVALID_RESPONSE'
    RPC_CHAIN_MISMATCH   = 'RPC_CHAIN_MISMATCH'
    SSL_ERROR            = 'SSL_ERROR'
    CONNECTION_ABORTED   = 'CONNECTION_ABORTED'
    SERVER_ERROR_504     = 'SERVER_ERROR_504'
    GATEWAY_TIMEOUT      = 'GATEWAY_TIMEOUT'
    NONCE_TOO_LOW        = 'NONCE_TOO_LOW'
    TX_HASH_NOT_FOUND    = 'TX_HASH_NOT_FOUND'
    MAX_FEE_TOO_LOW      = 'MAX_FEE_LESS_THAN_BASE'
    GAS_LIMIT_EXCEEDED   = 'GAS_LIMIT_EXCEEDED'
    INSUFFICIENT_FUNDS   = 'INSUFFICIENT_FUNDS'
    REPLACE_UNDERPRICE   = 'REPLACE_UNDERPRICE'
    EXECUTION_REVERTED   = 'EXECUTION_REVERTED'


class Error(object):

    def __init__(self, code: ErrorCode, call_param: tuple[Any, ...], msg: str = '', reason: str | None = None) -> None:
        self.code      : ErrorCode       = code
        self.call_param: tuple[Any, ...] = call_param
        self.msg       : str             = msg
        self.reason    : str | None      = reason

    def __str__(self) -> str:
        return self.code.value


def is_error(value: Any) -> bool:
    if isinstance(value, Error):
        return True
    return False


def _classify_exception(error: Exception, text: str, http_status: int | None, rpc_code: int | None) -> ErrorCode:
    if isinstance(error, ContractLogicError):
        return ErrorCode.EXECUTION_REVERTED
    if isinstance(error, BadResponseFormat):
        return ErrorCode.RPC_INVALID_RESPONSE
    if http_status == 429 or rpc_code == 429 or isinstance(error, TooManyRequests):
        return ErrorCode.RPC_CALL_RATE_LIMIT
    if http_status in (401, 403) or rpc_code in (401, 403):
        return ErrorCode.RPC_AUTH_FAILED
    if 'execution reverted' in text:
        return ErrorCode.EXECUTION_REVERTED
    if any(term in text for term in ('too many requests', 'rate limit', 'rate-limit', 'quota exceeded',
                                     'quota has been exceeded', 'request rate exceeded')):
        return ErrorCode.RPC_CALL_RATE_LIMIT
    if isinstance(error, SSLError) or any(key in text for key in ('sslerror', 'ssleoferror')):
        return ErrorCode.SSL_ERROR
    elif all(key in text for key in ('connection', 'abort')):
        return ErrorCode.CONNECTION_ABORTED
    elif http_status == 504 or '504' in text:
        return ErrorCode.SERVER_ERROR_504
    elif isinstance(error, (Timeout, TimeoutError)) or any(
        key in text for key in ('gateway timeout', 'timeout', 'timed out')
    ):
        return ErrorCode.GATEWAY_TIMEOUT
    elif all(key in text for key in ['nonce', 'low']):
        return ErrorCode.NONCE_TOO_LOW
    elif all(key in text for key in ['transaction', 'hash', 'not found']):
        return ErrorCode.TX_HASH_NOT_FOUND
    elif all(key in text for key in ['max fee', 'less than', 'base fee']):
        return ErrorCode.MAX_FEE_TOO_LOW
    elif all(key in text for key in ['insufficient']) and any(key in text for key in ['balance', 'funds']):
        return ErrorCode.INSUFFICIENT_FUNDS
    elif all(key in text for key in ['replace', 'underprice']):
        return ErrorCode.REPLACE_UNDERPRICE
    elif all(key in text for key in ['execution', 'reverted']):
        return ErrorCode.EXECUTION_REVERTED
    elif any(key in text for key in ('server', 'error')):
        return ErrorCode.SERVER_ERROR_504
    return ErrorCode.UNKNOWN


def _provider_error(error: Exception, call_param: tuple[Any, ...]) -> Error:
    response    : Response | None = getattr(error, 'response', None)
    http_status : int | None      = getattr(response, 'status_code', None)
    rpc_response: object          = getattr(error, 'rpc_response', None)
    if rpc_response is None and isinstance(response, Response):
        try:
            rpc_response = response.json()
        except (TypeError, ValueError):
            pass
    payload: object = rpc_response.get('error', rpc_response) if isinstance(rpc_response, Mapping) else None
    if not isinstance(payload, Mapping):
        payload = error.args[0] if error.args and isinstance(error.args[0], Mapping) else {}
    provider_code: object     = payload.get('code')
    rpc_code     : int | None = provider_code if type(provider_code) is int else None
    raw_message  : str        = str(error)
    text         : str        = f"{raw_message} {payload.get('message', '')}".lower()
    code         : ErrorCode  = _classify_exception(error, text, http_status, rpc_code)
    return Error(code, call_param, raw_message, _exception_reason(error))


def parse_exception_to_error_code(e: Exception) -> ErrorCode:
    return _provider_error(e, ()).code


def decode_revert(data: str | bytes | None) -> str | None:
    try:
        raw = HexBytes(data)
    except (TypeError, ValueError, OverflowError):
        return None
    if not raw:
        return None
    try:
        if raw[:4].hex() == '08c379a0':
            return decode(['string'], raw[4:])[0] or 'Execution reverted with an empty reason.'
        if raw[:4].hex() == '4e487b71':
            code = decode(['uint256'], raw[4:])[0]
            meaning = {
                0x00: 'compiler panic', 0x01: 'assertion failed',
                0x11: 'arithmetic overflow or underflow', 0x12: 'division or modulo by zero',
                0x21: 'invalid enum conversion', 0x22: 'invalid storage byte array encoding',
                0x31: 'pop from an empty array', 0x32: 'array index out of bounds',
                0x41: 'too much memory allocated', 0x51: 'uninitialized internal function',
            }.get(code, 'unknown panic code')
            return f'Panic(0x{code:x}): {meaning}'
        if len(raw) >= 4:
            return f'Custom error 0x{raw[:4].hex()} (data: {raw.to_0x_hex()})'
    except (TypeError, ValueError, OverflowError, DecodingError):
        pass
    return f'Revert data: {raw.to_0x_hex()}'


def _exception_reason(error: Exception) -> str | None:
    if not isinstance(error, ContractLogicError):
        return None
    data = error.data
    if isinstance(data, Mapping):
        data = data.get('data', data.get('result'))
    decoded = decode_revert(data) if isinstance(data, (str, bytes)) else None
    return decoded or error.message or 'Execution reverted without return data.'


def contract_call(contract: Contract, method: str, *args) -> Any | Error:
    try:
        func = contract.get_function_by_name(method)
        result: Any = func(*args).call()
        return result
    except Exception as e:
        return _provider_error(e, (contract.address, method, args))


def contract_build_tx(contract: Contract, method: str | None, tx_params: TxParams, *args) -> TxParams | Error:
    try:
        func = contract.constructor if method is None else contract.get_function_by_name(method)
        tx: TxParams = func(*args).build_transaction(tx_params)
        return tx
    except Exception as e:
        return _provider_error(e, (contract.address, method, tx_params, args))


class Web3EthWrapper(Eth):

    def __init__(self, web3: Web3, chain: ChainID) -> None:
        super().__init__(web3)
        self.__chain: ChainID = chain

    @property
    def chain_id(self):
        return self.__chain.value


class Interface(object):

    def __init__(self, chain: ChainID) -> None:
        self.chain = chain

    def start(self) -> bool:
        raise NotImplementedError

    def started(self) -> bool:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError

    def gas_price(self, callback: Callable[[Wei | Error], None] = None) -> Wei | Error | None:
        raise NotImplementedError

    def get_contract(self, address: ChecksumAddress | None, abi: str, callback: Callable[[Contract | Error], None] = None,
                     *, bytecode: str | None = None) -> Contract | Error | None:
        raise NotImplementedError

    def latest_block_number(self, callback: Callable[[int | Error], None] = None) -> int | Error | None:
        raise NotImplementedError

    def remote_chain_id(self) -> int | Error:
        raise NotImplementedError

    def log_receipt(self, contract_address: LogAddresses, blk_num_from: int, blk_num_to: int, event_hash: LogTopics,
                    callback: Callable[[list[LogReceipt] | Error], None] | None = None) -> list[LogReceipt] | Error | None:
        raise NotImplementedError

    def transaction_receipt(self, tx_hash: HexBytes, callback: Callable[[tuple[HexBytes, TxReceipt] | Error], None] = None) -> TxReceipt | Error | None:
        raise NotImplementedError

    def transaction(self, tx_hash: HexBytes) -> TxData | Error:
        raise NotImplementedError

    def transaction_revert_reason(self, tx_hash: HexBytes) -> str | None:
        raise NotImplementedError

    def nonce(self, address: ChecksumAddress, callback: Callable[[int | Error], None] = None,
              *, block: str = 'latest') -> int | Error | None:
        raise NotImplementedError

    def sign_transaction(self, transaction: TxParams, private_key: Key, callback: Callable[[SignedTransaction | Error], None] = None) -> SignedTransaction | Error | None:
        raise NotImplementedError

    def send_transaction(self, transaction: SignedTransaction, callback: Callable[[HexBytes | Error], None] = None) -> HexBytes | Error | None:
        raise NotImplementedError


class Connection(Interface):

    def __init__(self, chain: ChainID, url: str) -> None:
        super().__init__(chain)
        self.TAG        : str                   = f'{__name__}.Connection'
        self.url        : str                   = url
        self.w3         : Web3|None             = None
        self.turn_off   : bool                  = True
        self.mutex      : threading.RLock       = threading.RLock()
        self.condition  : threading.Condition   = threading.Condition(self.mutex)
        self.tasks      : list[Callable]        = []
        self.worker     : threading.Thread|None = None
        log.debug(self.TAG, 'Initialized RPC connection')

    def start(self) -> bool:
        if not self.turn_off:
            log.debug(self.TAG, f'start({self}) already started')
            return True
        if self.worker is not None:
            log.debug(self.TAG, f'start({self}) stopping previous thread')
            self.stop()
        else:
            log.debug(self.TAG, f'start({self})')
        if self.url.startswith('http'):
            provider: HTTPProvider                       = Web3.HTTPProvider(self.url)
            retry   : ExceptionRetryConfiguration | None = provider.exception_retry_configuration
            if retry is not None:
                # The event poller handles retries for synchronization reads.
                retry.method_allowlist = [
                    method for method in retry.method_allowlist
                    if method not in ('eth_chainId', 'eth_blockNumber', 'eth_getLogs')
                ]
            self.w3 = Web3(provider)
        elif self.url.startswith('ws'):
            self.w3 = Web3(Web3.LegacyWebSocketProvider(self.url))
        else:
            raise ValueError('Unsupported RPC URL scheme.')
        if self.chain == ChainID.POLYGON or self.chain == ChainID.BSC:
            self.w3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        self.w3.eth = Web3EthWrapper(self.w3, self.chain)
        self.turn_off = False
        self.worker = threading.Thread(target=self.work_loop)
        self.worker.start()
        return True

    def started(self) -> bool:
        return not self.turn_off

    def stop(self) -> None:
        if self.turn_off:
            log.debug(self.TAG, f'stop({self}) already stopped')
            return
        log.debug(self.TAG, f'stop({self}) shutting down')
        self.turn_off = True
        self.worker.join()
        self.worker = None
        self.w3 = None
        log.debug(self.TAG, f'stop({self}) done')

    def gas_price(self, callback: Callable[[Wei | Error], None] = None) -> Wei | Error | None:
        call_param: tuple[Any, ...] = (callback, )
        signature: str = f'gas_price(async={True if callback else False})'
        def _() -> Wei:
            return Wei(self.w3.eth.gas_price)
        if self.turn_off:
            log.error(self.TAG, "Connection not started")
            return Error(ErrorCode.RPC_NOT_STARTED, call_param)
        if callback is None:
            return self.run_sync(_, signature, call_param)
        self.run_async(_, signature, call_param, callback)

    def get_contract(self, address: ChecksumAddress | None, abi: str, callback: Callable[[Contract | Error], None] = None,
                     *, bytecode: str | None = None) -> Contract | Error | None:
        call_param: tuple[Any, ...] = (address, abi, callback)
        signature: str = f'any_contract(address={address}, async={True if callback else False})'
        def _() -> Contract:
            return self.w3.eth.contract(address=address, abi=abi, bytecode=bytecode)
        if self.turn_off:
            log.error(self.TAG, "Connection not started")
            return Error(ErrorCode.RPC_NOT_STARTED, call_param)
        if callback is None:
            return self.run_sync(_, signature, call_param)
        self.run_async(_, signature, call_param, callback)

    def latest_block_number(self, callback: Callable[[int | Error], None] = None) -> int | Error | None:
        call_param: tuple[Any, ...] = (callback, )
        signature: str = f'latest_block_number(async={True if callback else False})'
        def _() -> int:
            return self.w3.eth.block_number
        if self.turn_off:
            log.error(self.TAG, "Connection not started")
            return Error(ErrorCode.RPC_NOT_STARTED, call_param)
        if callback is None:
            return self.run_sync(_, signature, call_param)
        self.run_async(_, signature, call_param, callback)

    def remote_chain_id(self) -> int | Error:
        """Read the provider's chain ID before trusting its data."""
        if self.turn_off:
            return Error(ErrorCode.RPC_NOT_STARTED, ())

        def read() -> int | Error:
            try:
                # self.w3.eth.chain_id is the configured chain, not a remote read.
                value: object = self.w3.manager.request_blocking('eth_chainId', [])
            except BadResponseFormat:
                return Error(ErrorCode.RPC_INVALID_RESPONSE, ())
            if isinstance(value, str) and re.fullmatch(r'0x[0-9a-fA-F]+', value):
                value = int(value, 16)
            if type(value) is int and value > 0:
                return value
            return Error(ErrorCode.RPC_INVALID_RESPONSE, ())

        result: int | Error | None = self.run_sync(read, 'remote_chain_id()', ())
        return result if result is not None else Error(ErrorCode.RPC_NOT_STARTED, ())

    def log_receipt(self, contract_address: LogAddresses, blk_num_from: int, blk_num_to: int, event_hash: LogTopics,
                    callback: Callable[[list[LogReceipt] | Error], None] | None = None) -> list[LogReceipt] | Error | None:
        call_param: tuple[Any, ...] = (contract_address, blk_num_from, blk_num_to, event_hash, callback)
        signature : str             = f'log_receipt(contract_address={contract_address}, blk_num_from={blk_num_from}, blk_num_to={blk_num_to}, event_hash={event_hash}, async={True if callback else False})'
        def _() -> list[LogReceipt]:
            return self.w3.eth.get_logs({
                'address'  : contract_address,
                'fromBlock': blk_num_from,
                'toBlock'  : blk_num_to,
                'topics'   : event_hash
            })
        if self.turn_off:
            log.error(self.TAG, "Connection not started")
            return Error(ErrorCode.RPC_NOT_STARTED, call_param)
        if callback is None:
            return self.run_sync(_, signature, call_param)
        self.run_async(_, signature, call_param, callback)

    def transaction_receipt(self, tx_hash: HexBytes, callback: Callable[[tuple[HexBytes, TxReceipt] | Error], None] = None) -> TxReceipt | Error | None:
        call_param: tuple[Any, ...] = (tx_hash, callback)
        signature: str = f'transaction_receipt(tx_hash={tx_hash.to_0x_hex()}, async={True if callback else False})'
        def _() -> TxReceipt:
            return self.w3.eth.get_transaction_receipt(tx_hash)
        if self.turn_off:
            log.error(self.TAG, "Connection not started")
            return Error(ErrorCode.RPC_NOT_STARTED, call_param)
        if callback is None:
            return self.run_sync(_, signature, call_param)
        def bridge(result: TxReceipt|Error) -> None:
            if isinstance(result, Error):
                callback(result)
            elif isinstance(result, AttributeDict):
                callback((tx_hash, result))
            else:
                msg: str = f'w3.eth.get_transaction_receipt(tx_hash="{tx_hash.to_0x_hex()}") return unexpected type "{type(result)}" with value "{result}"'
                log.error(self.TAG, 'Unexpected transaction receipt response')
                callback(Error(ErrorCode.UNKNOWN, call_param, msg))
        self.run_async(_, signature, call_param, bridge)

    def transaction(self, tx_hash: HexBytes) -> TxData | Error:
        if self.turn_off:
            return Error(ErrorCode.RPC_NOT_STARTED, (tx_hash,))
        return self.run_sync(lambda: self.w3.eth.get_transaction(tx_hash),
                             f'transaction(hash={tx_hash.to_0x_hex()})', (tx_hash,))

    def transaction_revert_reason(self, tx_hash: HexBytes) -> str | None:
        if self.turn_off:
            return None
        result = self.run_sync(lambda: self.w3.manager.request_blocking('debug_traceTransaction', [
            tx_hash.to_0x_hex(), {'tracer': 'callTracer', 'tracerConfig': {'onlyTopCall': True}, 'timeout': '5s'},
        ]), f'transaction_revert_reason(hash={tx_hash.to_0x_hex()})', (tx_hash,))
        if not isinstance(result, Mapping):
            return None
        reason = decode_revert(result.get('output', result.get('returnValue')))
        if reason:
            return reason
        if isinstance(result.get('revertReason'), str) and result['revertReason']:
            return result['revertReason']
        error = result.get('error')
        if error:
            return 'Execution reverted without return data.' if 'revert' in str(error).lower() else str(error)
        return None

    def nonce(self, address: ChecksumAddress, callback: Callable[[int | Error], None] = None,
              *, block: str = 'latest') -> int | Error | None:
        call_param: tuple[Any, ...] = (address, callback)
        signature: str = f'nonce(address={address}, async={True if callback else False})'
        def _() -> Nonce:
            return self.w3.eth.get_transaction_count(address, block)
        if self.turn_off:
            log.error(self.TAG, "Connection not started")
            return Error(ErrorCode.RPC_NOT_STARTED, call_param)
        if callback is None:
            return self.run_sync(_, signature, call_param)
        self.run_async(_, signature, call_param, callback)

    def sign_transaction(self, transaction: TxParams, private_key: Key, callback: Callable[[SignedTransaction | Error], None] = None) -> SignedTransaction | Error | None:
        call_param: tuple[Any, ...] = (transaction, private_key, callback)
        signature: str = f'sign_transaction(transaction={transaction}, async={True if callback else False})'
        def _() -> SignedTransaction:
            return self.w3.eth.account.sign_transaction(transaction, private_key.private().to_bytes())
        if self.turn_off:
            log.error(self.TAG, "Connection not started")
            return Error(ErrorCode.RPC_NOT_STARTED, call_param)
        if callback is None:
            return self.run_sync(_, signature, call_param)
        self.run_async(_, signature, call_param, callback)

    def send_transaction(self, transaction: SignedTransaction, callback: Callable[[HexBytes | Error], None] = None) -> HexBytes | Error | None:
        call_param: tuple[Any, ...] = (transaction, callback)
        signature: str = f'send_transaction(transaction={transaction.raw_transaction.to_0x_hex()}, async={True if callback else False})'
        def _() -> HexBytes:
            return self.w3.eth.send_raw_transaction(transaction.raw_transaction)
        if self.turn_off:
            log.error(self.TAG, "Connection not started")
            return Error(ErrorCode.RPC_NOT_STARTED, call_param)
        if callback is None:
            return self.run_sync(_, signature, call_param)
        self.run_async(_, signature, call_param, callback)

    def work_loop(self) -> None:
        while not self.turn_off:
            with self.condition:
                while 0 == len(self.tasks) and not self.turn_off:
                    try:
                        self.condition.wait(0.1)
                    except KeyboardInterrupt:
                        pass
                for task in self.tasks:
                    task()
                self.tasks.clear()

    def run_sync(self, action: Callable, signature: str, call_param: tuple[Any, ...]) -> Any:
        result    : Any             = None
        done      : bool            = False
        done_event: threading.Event = threading.Event()

        def task() -> None:
            nonlocal result
            nonlocal done
            nonlocal done_event
            while not self.turn_off:
                try:
                    result = action()
                except Exception as e:
                    result = _provider_error(e, call_param)
                    lines: list[str] = [f'{signature} raised {type(e)} exception, {result.code.value}']
                    log.debug(self.TAG, lines)
                break
            done = True
            done_event.set()
        with self.condition:
            self.tasks.append(task)
            self.condition.notify()
        while not done:
            try:
                done_event.wait(0.01)
            except KeyboardInterrupt:
                pass
        return result

    def run_async(self, action: Callable, signature: str, call_param: tuple[Any, ...], callback: Callable[[Any], None]) -> None:
        def task() -> None:
            result: Any = None
            while not self.turn_off:
                try:
                    result = action()
                except Exception as e:
                    result = _provider_error(e, call_param)
                    lines: list[str] = [f'{signature} raised {type(e)} exception, {result.code.value}']
                    log.debug(self.TAG, lines)
                break
            callback(result)
        with self.condition:
            self.tasks.append(task)
            self.condition.notify()
