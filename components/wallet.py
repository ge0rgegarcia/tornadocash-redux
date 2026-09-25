from hexbytes import HexBytes
from web3.types import TxParams

from . import log, rpc
from .mytype import Key


def send(connection: rpc.Interface, transaction: TxParams | rpc.Error | None, key: Key,
         tag: str, operation: str = '', *, log_signed_hash: bool = False) -> HexBytes | None:
    if transaction is None or rpc.is_error(transaction):
        return None
    failure = f'{operation}, failed' if operation else 'Failed'
    signed = connection.sign_transaction(transaction, key)
    if rpc.is_error(signed):
        log.error(tag, f'{failure} to sign transaction, RPC error: {signed.code.value}')
        return None
    tx_hash = connection.send_transaction(signed)
    if rpc.is_error(tx_hash):
        details = f', signed tx hash: {signed.hash.to_0x_hex()}' if log_signed_hash else ''
        log.error(tag, f'{failure} to send transaction, RPC error: {tx_hash}{details}')
        return None
    return tx_hash
