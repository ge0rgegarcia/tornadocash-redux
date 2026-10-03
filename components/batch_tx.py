from pathlib import Path
from typing import Any
from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from web3 import Web3
from web3.contract import Contract
from web3.exceptions import ContractLogicError
from web3.types import Nonce, TxParams, Wei

from contracts import compiler
from . import log, rpc, util, wallet
from .pool import Tornado
from .mytype import ChainID, Key, Note, Symbol, SymbolType
import config


ETHEREUM_TX_GAS_LIMIT = 16_777_216  # EIP-7825
GAS_LIMIT_MESSAGE = f'Batch exceeds the Ethereum transaction gas limit of {ETHEREUM_TX_GAS_LIMIT:,} gas. Reduce the batch size.'


def _first(items: list[tuple[Tornado, Any]]) -> Tornado:
    if not items:
        raise ValueError('The batch is empty.')
    first: Tornado = items[0][0]
    if any(tornado.chain != first.chain for tornado, _ in items):
        raise ValueError('A batch transaction must use one blockchain.')
    return first


def _contract(tornado: Tornado, address: ChecksumAddress, path: str) -> Contract | rpc.Error:
    with open(path) as source:
        return tornado.connection.get_contract(address, source.read())


def _needs_more_gas(contract: Contract, method: str, args: tuple, params: TxParams) -> bool:
    # A capped estimate can hide an out-of-gas failure behind a contract revert.
    try:
        call = contract.get_function_by_name(method)(*args)
        block = contract.w3.eth.block_number
        try:
            call.call({**params, 'gas': ETHEREUM_TX_GAS_LIMIT}, block_identifier=block)
        except ContractLogicError:
            call.call({**params, 'gas': 2 * ETHEREUM_TX_GAS_LIMIT}, block_identifier=block)
            return True
    except Exception:
        pass
    return False


def _build(tornado: Tornado, contract: Contract | rpc.Error, method: str | None, args: tuple,
           account: ChecksumAddress, nonce: Nonce | None, value: Wei = Wei(0)) -> TxParams | rpc.Error | None:
    name = method or 'constructor'
    if rpc.is_error(contract):
        log.error('Batch', f'{name}(), failed to get contract, RPC error: {contract.code.value}')
        return None
    if nonce is None:
        tornado._progress('Fetching batch transaction nonce')
        nonce = tornado.connection.nonce(account, block='pending')
        if rpc.is_error(nonce):
            log.error('Batch', f'{name}(), failed to get nonce, RPC error: {nonce.code.value}')
            return None
    tornado._progress('Building batch transaction')
    params: TxParams = {
        'from': account, 'chainId': tornado.chain.value, 'nonce': Nonce(nonce), 'value': value,
    }
    transaction: TxParams | rpc.Error = rpc.contract_build_tx(contract, method, params, *args)
    if tornado.chain == ChainID.ETHEREUM:
        if rpc.is_error(transaction):
            exceeds_limit = (f'gas required exceeds allowance ({ETHEREUM_TX_GAS_LIMIT})' in transaction.msg.lower()
                             or (method is not None and transaction.code == rpc.ErrorCode.EXECUTION_REVERTED
                                 and _needs_more_gas(contract, method, args, params)))
        else:
            exceeds_limit = transaction.get('gas', 0) > ETHEREUM_TX_GAS_LIMIT
        if exceeds_limit:
            log.error('Batch', GAS_LIMIT_MESSAGE)
            return rpc.Error(rpc.ErrorCode.GAS_LIMIT_EXCEEDED, ())
    if rpc.is_error(transaction):
        log.error('Batch', f'{name}(), failed to build transaction, RPC error: {transaction}')
        return transaction
    if method is None:
        transaction.pop('to', None)
    return transaction


def token_amounts(deposits: list[tuple[Tornado, HexBytes]]) -> dict[Symbol, Wei]:
    _first(deposits)
    amounts: dict[Symbol, Wei] = {}
    for tornado, _ in deposits:
        if util.get_symbol_type(tornado.symbol) == SymbolType.ERC20:
            amount: Wei = util.unit_to_wei(tornado.unit, tornado.meta.decimals[tornado.symbol])
            amounts[tornado.symbol] = Wei(amounts.get(tornado.symbol, 0) + amount)
    return amounts


def deploy_tx(deposits: list[tuple[Tornado, HexBytes]], account: ChecksumAddress,
              nonce: Nonce | None = None) -> TxParams | rpc.Error | None:
    first = _first(deposits)
    if not token_amounts(deposits):
        raise ValueError('This batch does not need a batch contract.')
    mixed = any(util.get_symbol_type(tornado.symbol) == SymbolType.NATIVE for tornado, _ in deposits)
    abi, bytecode = compiler.batch_depositor_artifact(Path(config.BATCH_CONTRACTS_PATH), mixed, first._progress)
    contract = first.connection.get_contract(None, abi, bytecode=bytecode)
    return _build(first, contract, None, (first.proxy_address,), account, nonce)


def approve_tx(deposits: list[tuple[Tornado, HexBytes]], account: ChecksumAddress, symbol: Symbol,
               nonce: Nonce | None = None, *, depositor: ChecksumAddress) -> TxParams | rpc.Error | None:
    first: Tornado = _first(deposits)
    amounts: dict[Symbol, Wei] = token_amounts(deposits)
    if symbol not in amounts:
        raise ValueError('This token has no deposits in the batch.')
    tornado: Tornado = next(item for item, _ in deposits if item.symbol == symbol)
    return _build(first, tornado.token_contract, 'approve', (depositor, amounts[symbol]), account, nonce)


def deposit_tx(deposits: list[tuple[Tornado, HexBytes]], account: ChecksumAddress,
               nonce: Nonce | None = None, *, depositor: ChecksumAddress | None = None) -> TxParams | rpc.Error | None:
    first: Tornado = _first(deposits)
    erc20: bool = bool(token_amounts(deposits))
    if erc20 and depositor is None:
        raise ValueError('Deploy a batch contract before depositing ERC20 tokens.')
    calls: list[tuple] = []
    groups: dict[ChecksumAddress, list[tuple[ChecksumAddress, HexBytes]]] = {}
    value: int = 0
    for tornado, commitment in deposits:
        native: bool = util.get_symbol_type(tornado.symbol) == SymbolType.NATIVE
        amount: Wei = util.unit_to_wei(tornado.unit, tornado.meta.decimals[tornado.symbol])
        value += amount if native else 0
        if erc20:
            token: ChecksumAddress = Web3.to_checksum_address('0x' + '00' * 20) if native else tornado.meta.token_address[tornado.symbol]
            groups.setdefault(token, []).append((tornado.deployment_address, commitment))
        else:
            data: str = tornado.proxy_contract.encode_abi('deposit', args=[tornado.deployment_address, commitment, b''])
            calls.append((tornado.proxy_address, False, amount, data))
    if erc20:
        abi, _ = compiler.batch_depositor_artifact(Path(config.BATCH_CONTRACTS_PATH), bool(value), first._progress)
        contract = first.connection.get_contract(depositor, abi)
        if not rpc.is_error(contract):
            executed = rpc.contract_call(contract, 'executed')
            if rpc.is_error(executed):
                raise RuntimeError('Could not query the batch contract status.')
            if executed:
                raise ValueError('This batch contract has already executed. Check the deposit transaction in your wallet.')
        return _build(first, contract, 'deposit', (list(groups.items()),), account, nonce, Wei(value))
    return _build(first, _contract(first, first.meta.multicall_address, config.MULTICALL_ABI_PATH),
                  'aggregate3Value', (calls,), account, nonce, Wei(value))


def withdraw_tx(withdrawals: list[tuple[Tornado, Note]], account: ChecksumAddress, recipient: ChecksumAddress,
                nonce: Nonce | None = None) -> TxParams | rpc.Error | None:
    first: Tornado = _first(withdrawals)
    notes: set[tuple[ChecksumAddress, HexBytes]] = set()
    for tornado, note in withdrawals:
        identifier: tuple[ChecksumAddress, HexBytes] = (tornado.deployment_address, note.nullifier_hash)
        if identifier in notes:
            raise ValueError('The batch contains the same withdrawal more than once.')
        notes.add(identifier)
    calls: list[tuple] = []
    for index, (tornado, note) in enumerate(withdrawals, 1):
        tornado._progress(f'Preparing withdrawal {index}/{len(withdrawals)}')
        prepared = tornado.prepare_withdraw(note, recipient)
        if prepared is None:
            return None
        path, proof = prepared
        data: str = tornado.proxy_contract.encode_abi('withdraw', args=[
            tornado.deployment_address, HexBytes(proof['solidity']['proof']), path.root, note.nullifier_hash,
            recipient, Web3.to_checksum_address('0x' + '00' * 20), 0, 0,
        ])
        calls.append((tornado.proxy_address, False, 0, data))
    return _build(first, _contract(first, first.meta.multicall_address, config.MULTICALL_ABI_PATH),
                  'aggregate3Value', (calls,), account, nonce)


def send(tornado: Tornado, transaction: TxParams | rpc.Error | None, key: Key) -> HexBytes | None:
    result = wallet.send(tornado.connection, transaction, key, 'Batch', log_signed_hash=True)
    if result is None:
        return None
    log.info('Batch', f'Transaction submitted, tx hash: {result.to_0x_hex()}')
    return result
