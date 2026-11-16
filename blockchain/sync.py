from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from time import monotonic
from typing import Callable, TypeVar
from web3 import Web3
from web3.types import LogReceipt, Wei
import threading
import time

from . import rpc
from util import helper_func, log
from util.helper_func import upper_first_char
from util.mytype import ChainID, chain_to_string, Metadata, Second
import config


class LogEvent(ABC):

    def __init__(self, signature: str) -> None:
        self.signature: str = signature

    def __str__(self) -> str:
        raise NotImplementedError

    def __dict__(self) -> dict:
        raise NotImplementedError

    @classmethod
    @abstractmethod
    def load(cls, _dict: dict):
        pass

    @classmethod
    @abstractmethod
    def event_name(cls) -> str:
        pass

    @classmethod
    @abstractmethod
    def event_hash(cls) -> HexBytes:
        pass


class EventDeposit(LogEvent):

    def __init__(self, timestamp : int = None,
                       blk_num   : int = None,
                       tx_hash   : HexBytes = None,
                       commitment: HexBytes = None,
                       leaf_index: int = None) -> None:
        super().__init__('Deposit(bytes32,uint32,uint256)')
        self.timestamp : int      = timestamp
        self.blk_num   : int      = blk_num
        self.tx_hash   : HexBytes = tx_hash
        self.commitment: HexBytes = commitment
        self.leaf_index: int      = leaf_index

    def __str__(self) -> str:
        return (f'timestamp={self.timestamp}, '
                f'blk_num={self.blk_num}, '
                f'tx_hash={self.tx_hash.to_0x_hex()}, '
                f'commitment={self.commitment.to_0x_hex()}, '
                f'leaf_index={self.leaf_index}')

    def __dict__(self) -> dict:
        return {
            'timestamp' : self.timestamp,
            'blk_num'   : self.blk_num,
            'tx_hash'   : self.tx_hash,
            'commitment': self.commitment,
            'leaf_index': self.leaf_index,
        }

    @classmethod
    def load(cls, _dict: dict) -> 'EventDeposit':
        return EventDeposit(
            _dict['timestamp'],
            _dict['blk_num'],
            _dict['tx_hash'],
            _dict['commitment'],
            _dict['leaf_index'])

    @classmethod
    def event_name(cls) -> str:
        return 'Deposit'

    @classmethod
    def event_hash(cls) -> HexBytes:
        return Web3.keccak(text='Deposit(bytes32,uint32,uint256)')


class EventWithdraw(LogEvent):

    def __init__(self, blk_num       : int = None,
                       tx_hash       : HexBytes = None,
                       nullifier_hash: HexBytes = None,
                       recipient     : ChecksumAddress = None,
                       fee           : Wei = None) -> None:
        super().__init__('Withdrawal(address,bytes32,address,uint256)')
        self.blk_num       : int             = blk_num
        self.tx_hash       : HexBytes        = tx_hash
        self.nullifier_hash: HexBytes        = nullifier_hash
        self.recipient     : ChecksumAddress = recipient
        self.fee           : Wei             = fee

    def __str__(self) -> str:
        return (f'blk_num={self.blk_num}, '
                f'tx_hash={self.tx_hash}, '
                f'nullifier_hash={self.nullifier_hash.to_0x_hex()}, '
                f'recipient={self.recipient}, '
                f'fee={self.fee}')

    def __dict__(self) -> dict:
        return {
            'blk_num'       : self.blk_num,
            'tx_hash'       : self.tx_hash,
            'nullifier_hash': self.nullifier_hash,
            'recipient'     : self.recipient,
            'fee'           : self.fee,
        }

    @classmethod
    def load(cls, _dict: dict) -> 'EventWithdraw':
        return EventWithdraw(
            _dict['blk_num'],
            _dict['tx_hash'],
            _dict['nullifier_hash'],
            _dict['recipient'],
            _dict['fee'])

    @classmethod
    def event_name(cls) -> str:
        return 'Withdraw'

    @classmethod
    def event_hash(cls) -> HexBytes:
        return Web3.keccak(text='Withdrawal(address,bytes32,address,uint256)')


Topics     = tuple[HexBytes, ...]
Logs       = list[LogReceipt]
LogKey     = tuple[object, int, int, Topics]
LogRequest = tuple[frozenset[ChecksumAddress], int, int, Topics]

Result = TypeVar('Result')


@dataclass
class _Head:
    number    : int
    checked_at: float
    expires_at: float
    logs      : dict[LogKey, Logs] = field(default_factory=dict)


@dataclass
class _Pool:
    next_block  : int
    head        : _Head | None = None
    registration: object       = field(default_factory=object)


class ChainReader:
    """Share block and log reads across pools on one chain."""

    def __init__(self, connection: rpc.Interface, *, head_max_age: float = 60,
                 expected_chain_id: int | None = None) -> None:
        self.connection        : rpc.Interface                = connection
        self.head_max_age      : float                        = head_max_age
        self._expected_chain_id: int | None                   = expected_chain_id
        self._pools            : dict[ChecksumAddress, _Pool] = {}
        self._head             : _Head | None                 = None

        self._pools_lock  : threading.Lock = threading.Lock()
        self._head_lock   : threading.Lock = threading.Lock()
        self._logs_lock   : threading.Lock = threading.Lock()
        self._request_lock: threading.Lock = threading.Lock()

        self._failed_requests: dict[LogRequest | None, tuple[float, rpc.Error]] = {}

    def register(self, contract: ChecksumAddress, start: int) -> None:
        with self._pools_lock:
            self._pools[contract] = _Pool(start)

    def unregister(self, contract: ChecksumAddress) -> None:
        with self._pools_lock:
            self._pools.pop(contract, None)

    def persisted(self, contract: ChecksumAddress, next_block: int) -> None:
        with self._pools_lock:
            self._pools[contract].next_block = next_block

    def latest_block_number(self, contract: ChecksumAddress, refresh: bool = False) -> int | rpc.Error:
        # A fresh head completed while this caller was waiting satisfies its refresh.
        observed: _Head | None = self._head
        with self._head_lock:
            head: _Head | None = self._head
            with self._request_lock:
                error: rpc.Error | None = self._recent_error(None)
                if error is not None:
                    return error
                with self._pools_lock:
                    pool   : _Pool = self._pools[contract]
                    minimum: int   = max(0, pool.next_block - 1)
                    if (head is not None and head.number >= minimum and monotonic() < head.expires_at
                            and head is not pool.head and (not refresh or head is not observed)):
                        pool.head = head
                        return head.number

            def read_head() -> int | rpc.Error:
                if self._expected_chain_id is not None:
                    remote_chain: int | rpc.Error = self.connection.remote_chain_id()
                    if rpc.is_error(remote_chain):
                        return remote_chain
                    if remote_chain != self._expected_chain_id:
                        return rpc.Error(rpc.ErrorCode.RPC_CHAIN_MISMATCH, (),
                                         f'RPC reports chain ID {remote_chain}, expected {self._expected_chain_id}.')
                    self._expected_chain_id = None
                result: int | rpc.Error | None = self.connection.latest_block_number()
                if rpc.is_error(result):
                    return result
                if type(result) is not int or result < 0:
                    return rpc.Error(rpc.ErrorCode.RPC_INVALID_RESPONSE, (), 'RPC head must be a non-negative integer.')
                return result

            result: int | rpc.Error = self._request(None, read_head)
            if rpc.is_error(result):
                return result
            head = _Head(result, time.time(), monotonic() + self.head_max_age)
            with self._pools_lock:
                self._head = head
                if head.number < minimum:
                    return rpc.Error(rpc.ErrorCode.RPC_INVALID_RESPONSE, (), 'RPC head is behind the pool sync cursor.')
                pool.head = head
            return head.number

    def head_checked_at(self, contract: ChecksumAddress) -> float:
        with self._pools_lock:
            return self._pools[contract].head.checked_at

    def get_logs(self, contract: ChecksumAddress, start: int, end: int, topics: Topics) -> Logs | rpc.Error:
        with self._pools_lock:
            pool: _Pool        = self._pools[contract]
            head: _Head | None = pool.head
        if head is None or end != head.number:
            result: dict[str, Logs] | rpc.Error = self._read_pools([contract], start, end, topics)
            return result if rpc.is_error(result) else result[contract.lower()]

        # Share the last range of a round. Historical ranges can progress independently.
        with self._logs_lock:
            key: LogKey = (pool.registration, start, end, topics)
            if key in head.logs:
                return head.logs.pop(key)
            peers: list[tuple[ChecksumAddress, object]] = []
            with self._pools_lock:
                if head is self._head:
                    peers = [
                        (address, peer.registration)
                        for address, peer in self._pools.items()
                        if address != contract and peer.next_block == start
                        and (peer.registration, start, end, topics) not in head.logs
                    ]
            members: list[tuple[ChecksumAddress, object]] = [(contract, pool.registration), *peers]
            result = self._read_pools([address for address, _ in members], start, end, topics)
            if rpc.is_error(result):
                return result
            for address, registration in members[1:]:
                head.logs[registration, start, end, topics] = result[address.lower()]
            return result[contract.lower()]

    def _read_pools(self, addresses: list[ChecksumAddress], start: int, end: int,
                    topics: Topics) -> dict[str, Logs] | rpc.Error:
        address_filter: rpc.LogAddresses = addresses[0] if len(addresses) == 1 else addresses

        def read_logs() -> dict[str, Logs] | rpc.Error:
            result: Logs | rpc.Error | None = self.connection.log_receipt(address_filter, start, end, [list(topics)])
            if rpc.is_error(result):
                return result
            if not isinstance(result, list):
                return rpc.Error(rpc.ErrorCode.RPC_INVALID_RESPONSE, (), 'RPC did not return a list of logs.')

            by_address  : dict[str, Logs]                   = {address.lower(): [] for address in addresses}
            block_hashes: dict[int, bytes]                  = {}
            seen        : dict[tuple[int, int], LogReceipt] = {}
            for event in result:
                if not isinstance(event, Mapping):
                    return rpc.Error(rpc.ErrorCode.RPC_INVALID_RESPONSE, (), 'RPC returned a malformed log.')
                address     : str | None                = event.get('address')
                block       : int | None                = event.get('blockNumber')
                index       : int | None                = event.get('logIndex')
                event_topics: Sequence[HexBytes] | None = event.get('topics')
                block_hash  : bytes | None              = event.get('blockHash')
                if (not isinstance(address, str) or address.lower() not in by_address
                        or type(block) is not int or not start <= block <= end
                        or type(index) is not int or index < 0
                        or not isinstance(event_topics, (list, tuple)) or not event_topics or event_topics[0] not in topics
                        or not isinstance(block_hash, bytes) or len(block_hash) != 32
                        or event.get('removed', False) is not False):
                    return rpc.Error(rpc.ErrorCode.RPC_INVALID_RESPONSE, (), 'RPC returned a log outside its filter.')
                key: tuple[int, int] = (block, index)
                if block_hashes.setdefault(block, block_hash) != block_hash or (key in seen and seen[key] != event):
                    return rpc.Error(rpc.ErrorCode.RPC_INVALID_RESPONSE, (), 'Conflicting logs for the same block.')
                if key not in seen:
                    seen[key] = event
                    by_address[address.lower()].append(event)
            for logs in by_address.values():
                logs.sort(key=lambda event: (event['blockNumber'], event['logIndex']))
            return by_address

        request: LogRequest = (frozenset(addresses), start, end, topics)
        return self._request(request, read_logs)

    def _recent_error(self, request: LogRequest | None) -> rpc.Error | None:
        failure: tuple[float, rpc.Error] | None = self._failed_requests.get(request)
        return failure[1] if failure is not None and monotonic() < failure[0] else None

    def _request(self, request: LogRequest | None, action: Callable[[], Result | rpc.Error]) -> Result | rpc.Error:
        with self._request_lock:
            error: rpc.Error | None = self._recent_error(request)
            if error is not None:
                return error
            result: Result | rpc.Error = action()
            now   : float              = monotonic()
            self._failed_requests = {
                key: failure for key, failure in self._failed_requests.items() if failure[0] > now
            }
            if rpc.is_error(result):
                self._failed_requests[request] = (now + config.RPC_RETRY_INTERVAL_SEC, result)
            return result


class PoolPoller(object):
    """Track and advance the persisted synchronization of one pool."""

    class Activity(Enum):
        IDLE            = 'idle'
        FETCHING_HEAD   = 'fetching_head'
        FETCHING_EVENTS = 'fetching_events'
        RETRY_WAIT      = 'retry_wait'
        PERSISTING      = 'persisting'

    class Handler(object):

        def on_first_catchup(self) -> None:
            """Called when the first catchup is done."""
            pass

        def on_sync(self, block_from: int, block_to: int, deposits: list[EventDeposit], withdrawals: list[EventWithdraw]) -> None:
            """Called when a sync is done."""
            pass

        def on_latest_block(self, block_number: int) -> None:
            """Called when the latest block number is updated."""
            pass

    def __init__(self, chain: ChainID, connection: rpc.Interface | None = None, poll_interval: float | None = None,
                 reader: ChainReader | None = None) -> None:
        self.tag           : str                      = f'{__class__.__name__}@{upper_first_char(chain_to_string(chain))}'
        self.off           : bool                     = True
        self.chain         : ChainID                  = chain
        self.connection    : rpc.Interface | None     = connection  # If True, the RPC connection is managed outside
        self.external      : bool                     = connection is not None
        self.reader        : ChainReader | None       = reader
        self.meta          : Metadata                 = helper_func.load_metadata()[chain]
        self.contract      : ChecksumAddress | None   = None
        self.events        : list[LogEvent]           = [EventDeposit(), EventWithdraw()]
        self.current       : int                      = 0
        self.cond          : threading.Condition      = threading.Condition()
        self.worker        : threading.Thread | None  = None
        self.handlers      : list[PoolPoller.Handler] = []
        self.poll_interval : float                    = config.BLOCKCHAIN_LOG_EVENT_POLL_INTERVAL_SEC if poll_interval is None else poll_interval
        self.start_block   : int                      = 0
        self.latest_block  : int | None               = None
        self.last_check_sec: float | None             = None
        self.error         : str | None               = None
        self.activity      : PoolPoller.Activity      = PoolPoller.Activity.IDLE
        self.caught_up     : bool                     = False
        self.refresh_needed: bool                     = False
        self.deposits      : int                      = 0
        self.withdrawals   : int                      = 0
        if self.connection is None:
            self.connection = rpc.Connection(self.chain, config.RPC_URLS[self.chain])

    '''
    Start polling
    @param contract     Contract addresses
    @param start_block  Start from which block number (inclusive)
    '''
    def start(self, contract: ChecksumAddress, start_block: int,
              *, start_gate: threading.Event | None = None) -> bool:
        if not self.off:
            log.warn(self.tag, 'start() already started')
            return False
        if self.worker is not None:
            self.stop()
        if not self.external:
            if not self.connection.start():
                log.error(self.tag, f'Failed to start RPC connection for {self.chain}')
                return False
        elif not self.connection.started():
            log.error(self.tag, 'The external RPC connection is not started')
            return False
        registered: bool = False
        try:
            if self.reader is not None:
                self.reader.register(contract, start_block)
                registered = True
            with self.cond:
                self.off            = False
                self.contract       = contract
                self.current        = self.start_block = start_block
                self.latest_block   = self.last_check_sec = self.error = None
                self.caught_up      = False
                self.refresh_needed = False
                self.activity       = PoolPoller.Activity.FETCHING_HEAD
                self.deposits       = self.withdrawals = 0
                self.cond.notify_all()
            self.worker = threading.Thread(target=self._loop, args=(start_gate,))
            self.worker.start()
        except Exception:
            if registered:
                self.reader.unregister(contract)
            with self.cond:
                self.off      = True
                self.activity = PoolPoller.Activity.IDLE
                self.contract = None
                self.worker   = None
                self.cond.notify_all()
            if not self.external:
                self.connection.stop()
            raise
        log.debug(self.tag, f'start(contract={contract}, start_block={start_block}, events={[e.event_name() for e in self.events]})')
        return True

    def stop(self) -> None:
        if self.off and self.worker is None:
            log.warn(self.tag, 'stop() already stopped')
            return
        log.debug(self.tag, 'stop() shutting down')
        with self.cond:
            self.off = True
            self.cond.notify_all()
        self.worker.join()
        if not self.external:
            if self.connection.started():
                self.connection.stop()
        self.worker   = None
        self.contract = None
        log.debug(self.tag, 'stop() done')

    def is_started(self) -> bool:
        return not self.off

    def wait_sync(self, cancel: Callable[[], bool] | None = None, refresh: bool = False) -> bool:
        """Wait for a fresh persisted head; cancel only checks flags, refresh requests a new head."""
        with self.cond:
            while not self.off:
                if cancel is not None and cancel():
                    return False
                sync      : dict         = self.status()
                last_check: float | None = sync['last_check_sec']
                fresh     : bool         = last_check is not None and time.time() - last_check <= 60
                if sync['synchronized'] and fresh and not refresh:
                    return True
                if (refresh or (not fresh and self.activity != PoolPoller.Activity.FETCHING_HEAD)) and not self.refresh_needed:
                    self.refresh_needed = True
                    self.caught_up      = False
                    if sync['synchronized'] and self.activity == PoolPoller.Activity.IDLE:
                        self.cond.notify_all()
                refresh = False
                self.cond.wait(0.5)
            return False

    def status(self) -> dict:
        """Progress advances only after the synchronization handlers succeed."""
        with self.cond:
            synced   : int | None = None if self.latest_block is None else self.current - 1
            remaining: int | None = None if self.latest_block is None else max(0, self.latest_block - self.current + 1)
            if self.off:
                phase: str = 'error' if self.error else 'stopped'
            elif self.error:
                phase = 'retrying'
            elif self.latest_block is None:
                phase = 'connecting'
            else:
                phase = 'ready' if self.caught_up and remaining == 0 and not self.refresh_needed else 'syncing'
            progress: float | None = None
            if phase in ('syncing', 'ready'):
                progress = float(synced >= 0) if self.latest_block == 0 else min(1.0, max(0.0, synced / self.latest_block))
            return {
                'running'         : not self.off,
                'phase'           : phase,
                'activity'        : self.activity.value,
                'start_block'     : self.start_block,
                'synced_block'    : synced,
                'latest_block'    : self.latest_block,
                'remaining_blocks': remaining,
                'progress'        : progress,
                'synchronized'    : phase == 'ready',
                'last_check_sec'  : self.last_check_sec,
                'error'           : self.error,
                'deposits'        : self.deposits,
                'withdrawals'     : self.withdrawals,
            }

    def add_handler(self, handler: Handler) -> None:
        with self.cond:
            self.handlers.append(handler)

    def remove_all_handlers(self) -> None:
        with self.cond:
            self.handlers.clear()

    def _loop(self, start_gate: threading.Event | None = None) -> None:
        try:
            if start_gate is not None:
                while not self.off and not start_gate.wait(config.RPC_RETRY_INTERVAL_SEC):
                    pass
            self._poll()
        except Exception:
            with self.cond:
                self.error = 'Synchronization failed.'
                self.off   = True
                self.cond.notify_all()
            log.error(self.tag, self.error)
        finally:
            with self.cond:
                self.off      = True
                self.activity = PoolPoller.Activity.IDLE
                self.cond.notify_all()
            try:
                if self.reader is not None:
                    self.reader.unregister(ChecksumAddress(self.contract))
            finally:
                if not self.external and self.connection.started():
                    self.connection.stop()

    def _wait_to_retry(self, error: rpc.Error, operation: str) -> None:
        with self.cond:
            self.error    = error.code.value
            self.activity = PoolPoller.Activity.RETRY_WAIT
            self.cond.notify_all()
        log.error(self.tag, f'Failed to {operation}, RPC error: {error.code.value}')
        helper_func.wait(config.RPC_RETRY_INTERVAL_SEC, self.cond)

    def _poll(self) -> None:
        first_loop    : bool               = True
        contract      : ChecksumAddress    = ChecksumAddress(self.contract)
        reader        : ChainReader | None = self.reader
        topics        : Topics             = tuple(event.event_hash() for event in self.events)
        topic_groups  : list[Topics]       = [topics] if reader is not None else [(topic,) for topic in topics]
        deposit_topic : HexBytes           = EventDeposit.event_hash()
        withdraw_topic: HexBytes           = EventWithdraw.event_hash()
        while not self.off:
            latest : int | rpc.Error | None = 0
            refresh: bool                   = False
            # Get latest block number
            while not self.off:
                with self.cond:
                    self.activity       = PoolPoller.Activity.FETCHING_HEAD
                    refresh             = refresh or self.refresh_needed
                    self.refresh_needed = False
                latest = reader.latest_block_number(contract, refresh) if reader is not None else self.connection.latest_block_number()
                if not rpc.is_error(latest):
                    if type(latest) is int and latest >= 0:
                        break
                    latest = rpc.Error(rpc.ErrorCode.RPC_INVALID_RESPONSE, (), 'RPC head must be a non-negative integer.')
                self._wait_to_retry(latest, 'get latest block number')
            if self.off:
                break
            checked_at: float = reader.head_checked_at(contract) if reader is not None else time.time()
            with self.cond:
                self.latest_block   = latest
                self.last_check_sec = checked_at
                self.error          = None
                self.activity       = PoolPoller.Activity.IDLE
                self.cond.notify_all()
            count_block: int = max(0, latest - self.current + 1)

            # Notify latest block number
            for h in self.handlers:
                h.on_latest_block(latest)
            # Log how many blocks behind
            if first_loop:
                log.info(self.tag, f'{count_block} blocks behind, latest block number: {latest}')

            # Split into N blocks per request
            batch_size : int = config.BLOCKCHAIN_LOG_EVENT_POLL_BLOCKS_PER_REQUEST[self.chain]
            batch_count: int = count_block // batch_size
            residual   : int = count_block  % batch_size

            chunks: list[tuple[int, int]] = [(self.current + i * batch_size, self.current + (i + 1) * batch_size - 1) for i in range(batch_count)]
            if residual > 0:
                chunks.append((self.current + batch_count * batch_size, self.current + batch_count * batch_size + residual - 1))

            # Process each chunk
            break_early : bool = False
            for chunk in chunks:
                deposits      : list[EventDeposit]  = []
                withdrawals   : list[EventWithdraw] = []
                count_deposit : int                 = 0
                count_withdraw: int                 = 0
                if self.off:
                    break_early = True
                    break
                # Get event logs
                event_logs: list[LogReceipt] = []
                for group in topic_groups:
                    while not self.off:
                        with self.cond:
                            self.activity = PoolPoller.Activity.FETCHING_EVENTS
                        result: Logs | rpc.Error | None
                        if reader is not None:
                            result = reader.get_logs(contract, chunk[0], chunk[1], group)
                        else:
                            result = self.connection.log_receipt(contract, chunk[0], chunk[1], list(group))
                        if isinstance(result, list):
                            with self.cond:
                                self.error    = None
                                self.activity = PoolPoller.Activity.IDLE
                                self.cond.notify_all()
                            event_logs.extend(result)
                            break
                        if not rpc.is_error(result):
                            result = rpc.Error(rpc.ErrorCode.RPC_INVALID_RESPONSE, (), 'RPC did not return a list of logs.')
                        self._wait_to_retry(result, 'get event logs')
                    if self.off:
                        break
                    if reader is None:
                        helper_func.wait(config.RPC_RETRY_INTERVAL_SEC, self.cond)
                if self.off:
                    break_early = True
                    break

                # Process event logs
                for event in event_logs:
                    topics_in_log: Sequence[HexBytes] = event['topics']
                    data         : HexBytes           = event['data']
                    if (len(topics_in_log) != 2 or any(len(topic) != 32 for topic in topics_in_log)
                            or event.get('removed', False)
                            or not chunk[0] <= event['blockNumber'] <= chunk[1]
                            or len(event['transactionHash']) != 32):
                        raise ValueError('Invalid deposit or withdrawal event.')
                    if deposit_topic == topics_in_log[0] and len(data) == 64:
                        deposits.append(EventDeposit(
                            int.from_bytes(event['data'][32:64], byteorder='big'),  # timestamp
                            event['blockNumber'],                                   # blk_num
                            event['transactionHash'],                               # tx_hash
                            event['topics'][1],                                     # commitment
                            int.from_bytes(event['data'][:32], byteorder='big'),    # leaf_index
                        ))
                        count_deposit += 1
                    elif withdraw_topic == topics_in_log[0] and len(data) == 96:
                        withdrawals.append(EventWithdraw(
                            event['blockNumber'],                                        # blk_num
                            event['transactionHash'],                                    # tx_hash
                            event['data'][32:64],                                        # nullifier_hash
                            Web3.to_checksum_address(event['data'][12:32].to_0x_hex()),  # recipient
                            Wei(int.from_bytes(event['data'][64:96], byteorder='big')),  # fee
                        ))
                        count_withdraw += 1
                    else:
                        raise ValueError('Invalid deposit or withdrawal event.')

                deposits.sort(key=lambda event: event.leaf_index)
                # Shared RPC reads are serialized across the chain.
                if reader is None:
                    helper_func.wait(config.RPC_RETRY_INTERVAL_SEC, self.cond)

                # Log and notify
                log.debug(self.tag, f'Polled {chunk[1] - chunk[0] + 1} blocks, from {chunk[0]} to {chunk[1]}, '
                                    f'contract {self.contract}, deposit: {count_deposit}, withdraw: {count_withdraw}')

                # Callback and update
                with self.cond:
                    self.activity = PoolPoller.Activity.PERSISTING
                for h in self.handlers:
                    h.on_sync(chunk[0], chunk[1], deposits, withdrawals)
                with self.cond:
                    self.current      = chunk[1] + 1
                    self.deposits    += count_deposit
                    self.withdrawals += count_withdraw
                    self.activity     = PoolPoller.Activity.IDLE
                    self.cond.notify_all()
                if reader is not None:
                    reader.persisted(contract, chunk[1] + 1)
            if not break_early:
                # Notify latest block number
                for h in self.handlers:
                    h.on_latest_block(latest)
                # Notify first catchup
                if first_loop:
                    first_loop = False
                    log.info(self.tag, f'Synced to block {latest}')
                    for h in self.handlers:
                        h.on_first_catchup()
                with self.cond:
                    self.caught_up = not self.refresh_needed
                    self.error     = None
                    self.activity  = PoolPoller.Activity.IDLE
                    self.cond.notify_all()
                # Sleep interval if no new block
                with self.cond:
                    self.cond.wait_for(lambda: self.off or self.refresh_needed, timeout=Second(self.poll_interval))
