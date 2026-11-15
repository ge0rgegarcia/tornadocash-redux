from eth_typing import ChecksumAddress
from hexbytes import HexBytes
from requests import Response
from typing import Callable
from web3 import Web3
from web3.types import Wei
import requests

from blockchain import rpc
from util import log
from util.helper_func import get_symbol_type, wait
from util.mytype import ChainID, chain_to_string, Second, Symbol, SymbolType, TornadoUnit
import config


def quote(relayer_url: str, recipient: ChecksumAddress, *, chain: ChainID, symbol: Symbol, unit: TornadoUnit,
          deployment_address: ChecksumAddress, total_wei: Wei, decimals: int, refund: Wei,
          get_gas_price: Callable[[], Wei | rpc.Error | None], tag: str,
          on_progress: Callable[[str], None]) -> tuple[ChecksumAddress, Wei] | None:
    # HTTP Get relayer status
    on_progress('Fetching relayer status')
    try:
        resp: Response = requests.get(relayer_url.rstrip('/') + '/status')
    except BaseException as e:
        log.error(tag, f'withdraw(to={recipient}), failed to get relayer status from {relayer_url}, exception: {e}')
        return None
    # Check HTTP response status code
    if resp.status_code < 200 or resp.status_code >= 300:
        log.error(tag, f'withdraw(to={recipient}), relayer status request failed with status code {resp.status_code}')
        return None
    # Parse HTTP response
    try:
        relayer_status: dict = resp.json()
    except BaseException as e:
        log.error(tag, f'withdraw(to={recipient}), failed to parse relayer status to JSON, exception: {e}, response: {resp}')
        return None
    relayer_eth_prices: dict[Symbol, Wei] = {}
    # Response to JSON
    try:
        if int(relayer_status['netId']) != chain.value:
            log.error(tag, f'withdraw(to={recipient}), relayer netId {relayer_status["netId"]} does not match chain {chain.value}')
            return None
        for k, v in relayer_status.get('ethPrices', {}).items():
            if k not in Symbol:
                continue
            relayer_eth_prices[Symbol(k)] = Wei(int(v))
        try:
            relayer_instance: ChecksumAddress = Web3.to_checksum_address(relayer_status['instances'][symbol.value]['instanceAddress'][unit.value])
        except BaseException as e:
            log.error(tag, f'withdraw(to={recipient}), failed to parse relayer instance address, exception: {e}, response: {relayer_status}')
            return None
        if relayer_instance != deployment_address:
            log.error(tag, f'withdraw(to={recipient}), relayer instance address {relayer_instance} does not match deployment '
                                f'({chain_to_string(chain)}@{unit.value}{symbol.value}) address {deployment_address}')
            return None
        if get_symbol_type(symbol) != SymbolType.NATIVE and symbol not in relayer_eth_prices:
            log.error(tag, f'withdraw(to={recipient}), relayer does not support symbol {symbol}')
            return None
        relayer_address = relayer_status['rewardAccount']
    except BaseException as e:
        log.error(tag, f'withdraw(to={recipient}), invalid relayer status format, exception: {e}, response: {relayer_status}')
        return None
    # Calculate fee
    on_progress('Calculating relayer fee')
    decimal_points: int = 0 if '.' not in config.RELAYER_FEE_RATE else len(config.RELAYER_FEE_RATE.split('.')[1])
    round_decimal : int = 10 ** decimal_points
    fee_percent   : Wei = Wei(
        (total_wei * int(float(config.RELAYER_FEE_RATE) * round_decimal)) // (round_decimal * 100)
    )
    gas_price: Wei | rpc.Error = get_gas_price()
    if rpc.is_error(gas_price):
        log.error(tag, f'withdraw(to={recipient}), failed to get gas price, RPC error: {gas_price.code.value}')
        return None
    expense: Wei = Wei(gas_price * 500_000)
    if get_symbol_type(symbol) == SymbolType.NATIVE:
        relayer_fee = Wei(expense + fee_percent)
    else:
        expense += refund
        expense *= 10 ** decimals
        expense /= relayer_eth_prices[symbol]
        relayer_fee = Wei(int(expense + fee_percent))
    # Check if fee greater than total
    if relayer_fee >= total_wei:
        log.error(tag, f'withdraw(to={recipient}), relayer fee {relayer_fee} is greater than or equal to total {total_wei}')
        return None
    log.info(tag, f'withdraw(to={recipient}), relayer fee {relayer_fee} Wei ({relayer_fee * 100 / total_wei:.2f}%), relayer address {relayer_address}, relayer URL {relayer_url}')
    return relayer_address, relayer_fee


def submit(relayer_url: str, recipient: ChecksumAddress, relayer_address: ChecksumAddress, zk_proof: dict, *,
           deployment_address: ChecksumAddress, symbol: Symbol, unit: TornadoUnit, tag: str,
           on_progress: Callable[[str], None], can_submit: Callable[[], bool] | None) -> HexBytes | None:
    on_progress('Submitting withdrawal to relayer')
    if can_submit is not None and not can_submit():
        log.error(tag, 'Withdrawal canceled: backend request is no longer active')
        return None
    # Submit withdrawal to relayer and get job ID
    try:
        resp: Response = requests.post(
            url=relayer_url.rstrip('/') + '/v1/tornadoWithdraw',
            json={
                'contract': deployment_address,
                'proof': zk_proof['solidity']['proof'],
                'args': [
                    zk_proof['solidity']['publicSignals'][0],  # root
                    zk_proof['solidity']['publicSignals'][1],  # nullifierHash
                    recipient,                                 # recipient address
                    relayer_address,                           # relayer address
                    zk_proof['solidity']['publicSignals'][4],  # fee
                    zk_proof['solidity']['publicSignals'][5],  # refund
                ]
            }
        )
    except BaseException as e:
        log.error(tag, f'withdraw(to={recipient}), failed to submit withdrawal to relayer: {e}')
        return None
    if resp.status_code < 200 or resp.status_code >= 300:
        log.error(tag, f'withdraw(to={recipient}), relayer request failed with status code {resp.status_code}')
        return None
    try:
        job_id: str = resp.json()['id']  # {'id': '06194b2a-f51a-4428-848b-cf4c5adff7d0'}
    except BaseException as e:
        log.error(tag, f'withdraw(to={recipient}), failed to parse ID from relayer response: {e}, response: {resp}')
        return None
    log.info(tag, f'withdraw(to={recipient}), relayer job id "{job_id}"')

    # Wait for relayer to process the job and return the transaction hash
    count_attempts    : int = 0
    count_attempts_max: int = 10
    log.info(tag, f'withdraw(to={recipient}), waiting for job to be processed')
    on_progress('Waiting for relayer job')
    wait(Second(5))
    def recursive_query():
        nonlocal count_attempts
        nonlocal count_attempts_max
        on_progress(f'Checking relayer job: attempt {count_attempts + 1}/{count_attempts_max}')
        try:
            resp: Response = requests.get(relayer_url.rstrip('/') + f'/v1/jobs/{job_id}')
        except BaseException as e:
            log.error(tag, f'withdraw(to={recipient}), failed to get job status from relayer: {e}')
            return None
        if resp.status_code < 200 or resp.status_code >= 300:
            log.error(tag, f'withdraw(to={recipient}), query relayer job failed with status code {resp.status_code}')
            return None
        # Response JSON Example: {
        #     'id': '7985f894-ec91-4183-b62e-4147753a3fdc',
        #     'type': 'TORNADO_WITHDRAW',
        #     'status': 'FAILED',
        #     'contract': '0xA160cdAB225685dA1d56aa342Ad8841c3b53f291',
        #     'proof': '0x15778ac72233a060a5da3e134c3a2f169cd5ad7a5fea1eb62b20b95cd0c7d4fd196cf95aaa616f77a6252769e486fcb4f5668f35db0847eee237d6c102f38a0422c0d58923ed8cc32680ca2b94deb58bfd6129960828a53272cb490dcf255dc324e712818c69298fb216145e86adc075d612f710e5aa3578c66f204ba3c81456036b9616782348ff331698215265ed0bdf514c975ffb11219711017361aee72f03ce139bfbb13e39e3b1b4df397280602aa999101db82ea5495abaf359e34aea1d7660721bcb862d78d883d83de484dbb7c023cb0215c808c18a67a8c17888041369fe158f1e5ee45e6d22e0f663df900a883934106148b5cbb5089b563fe819',
        #     'args': ['0x1545d0fd81f378de328f10a21f954c56ddf780728ff3ea1f437873774f25b191', '0x1a1f75570a3bf2f38bf85e87eff2f3a8f6666e19676df1a49063f48801998194', '0x02395233b8175b0a04D5A0AD0F62Eaf7aFE55d5c', '0x000000Cd6521Ed1a65FAe0678eA15aF4EEAD74fe', '0x000000000000000000000000000000000000000000000000000aa87bee538000', '0x0000000000000000000000000000000000000000000000000000000000000000'],
        #     'failedReason': 'Provided fee is not enough. Probably it is a Gas Price spike, try to resubmit.'
        # }
        # Possible Status: ['QUEUED', 'ACCEPTED', 'SENT', 'MINED', 'RESUBMITTED', 'CONFIRMED', 'FAILED' ]
        try:
            result: dict = resp.json()
        except BaseException as e:
            log.error(tag, f'withdraw(to={recipient}), failed to parse relayer job response to JSON, exception: {e}, response: {resp}')
            return None
        if result['status'] in ('QUEUED', 'ACCEPTED', 'SENT', 'MINED', 'RESUBMITTED', 'CONFIRMED', 'FAILED'):
            on_progress(f'Relayer job: {result["status"].lower()}')
        if result['status'] == 'MINED' or result['status'] == 'CONFIRMED':
            try:
                relayer_tx_hash: str = result['txHash']
                log.info(tag, f'Withdraw {unit.value} {symbol.value.upper()} succeed, tx hash: {relayer_tx_hash}')
                return HexBytes.fromhex(relayer_tx_hash[2:] if relayer_tx_hash.startswith('0x') else relayer_tx_hash)
            except BaseException:
                log.error(tag, f'withdraw(to={recipient}), failed to read the relayer transaction hash')
                return None
        elif result['status'] == 'FAILED':
            log.error(tag, f'withdraw(to={recipient}), relayer job failed')
            return None
        else:
            count_attempts += 1
            if count_attempts >= count_attempts_max:
                log.error(tag, f'withdraw(to={recipient}), job status is "{result["status"]}", max attempts reached ({count_attempts_max}), giving up')
                return None
            log.warn(tag, f'withdraw(to={recipient}), job status is "{result["status"]}", waiting 5 seconds before next attempt')
            wait(Second(5))
            return recursive_query()
    return recursive_query()
