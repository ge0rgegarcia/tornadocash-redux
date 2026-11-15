"""Run the unchanged WebUI against the disposable Anvil Sepolia fork."""

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from web3 import Web3

import config
from components import log
from components.mytype import ChainID, Second, Symbol, TornadoUnit
import webui


def configure(state: Path) -> dict:
    state = state.resolve()
    for name in ('solcx', 'logs', 'db', 'backup', 'tmp'):
        (state / name).mkdir(parents=True, exist_ok=True)
    os.environ.setdefault('SOLCX_BINARY_PATH', str(state / 'solcx'))
    os.environ.setdefault('TMPDIR', str(state / 'tmp'))
    metadata = json.loads((ROOT / 'metadata.json').read_text())['sepolia']
    for symbol, units in {'eth': ('0.1', '1'), 'dai': ('100', '1000')}.items():
        for key in ('deployed_address', 'deployed_block_number'):
            metadata[symbol][key] = {unit: metadata[symbol][key][unit] for unit in units}
    (state / 'metadata.json').write_text(json.dumps({'sepolia': metadata}, indent=2) + '\n')
    fixture = {'rpc': 'http://127.0.0.1:18545', 'chain_id': 31337,
               'proxy': metadata['proxy_address'], 'multicall': metadata['multicall_address'],
               'tokens': {'dai': metadata['dai']['token_address']},
               'pools': {symbol: metadata[symbol]['deployed_address'] for symbol in ('eth', 'dai')}}
    (state / 'local-chain.json').write_text(json.dumps(fixture, indent=2) + '\n')
    contracts = state / 'contracts'
    shutil.copytree(ROOT / 'contracts/src', contracts / 'src', dirs_exist_ok=True)
    shutil.copytree(ROOT / 'contracts/vendor', contracts / 'vendor', dirs_exist_ok=True)
    for symbol, units in {'eth': ('0.1', '1'), 'dai': ('100', '1000')}.items():
        for unit in units:
            source = Path(config.DATABASE_PATHS[ChainID.SEPOLIA][Symbol(symbol)][TornadoUnit(unit)])
            destination = state / 'db' / f'{symbol}-{unit}.sqlite'
            if source.exists() and not destination.exists():
                with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as original:
                    with sqlite3.connect(destination) as isolated:
                        original.backup(isolated)
    # Keep the production chain labels and notes, but sign only for local Anvil.
    ChainID.SEPOLIA._value_ = 31337
    ChainID._value2member_map_[31337] = ChainID.SEPOLIA
    config.METADATA_PATH = str(state / 'metadata.json')
    config.BATCH_CONTRACTS_PATH = str(contracts)
    config.RPC_URLS = {ChainID.SEPOLIA: 'http://127.0.0.1:18545'}
    config.DATABASE_PATHS = {ChainID.SEPOLIA: {
        Symbol(symbol): {TornadoUnit(unit): str(state / 'db' / f'{symbol}-{unit}.sqlite')
                         for unit in metadata[symbol]['deployed_address']}
        for symbol in ('eth', 'dai')
    }}
    config.BACKUP_DIR = str(state / 'backup')
    config.LOG_DIR = str(state / 'logs')
    config.WEBUI_HOST, config.WEBUI_WS_PORT = '127.0.0.1', 18765
    config.WEBUI_SERVE_UI = False
    config.WEBUI_CHAINS = {ChainID.SEPOLIA}
    config.BLOCKCHAIN_LOG_EVENT_POLL_INTERVAL_SEC = Second(2)
    config.BLOCKCHAIN_LOG_EVENT_POLL_BLOCKS_PER_REQUEST[ChainID.SEPOLIA] = 50_000
    config.RPC_RETRY_INTERVAL_SEC = Second(0.05)
    return fixture


def fund(address: str, fixture: dict) -> None:
    chain = Web3(Web3.HTTPProvider('http://127.0.0.1:18545'))
    if chain.eth.chain_id != 31337:
        raise RuntimeError('The local RPC is not the disposable Anvil chain.')
    address = Web3.to_checksum_address(address)
    chain.provider.make_request('anvil_setBalance', [address, hex(100 * 10**18)])
    abi = [{'type': 'function', 'name': 'mint', 'stateMutability': 'nonpayable', 'outputs': [],
            'inputs': [{'name': 'token', 'type': 'address'}, {'name': 'to', 'type': 'address'},
                       {'name': 'amount', 'type': 'uint256'}]}]
    faucet = chain.eth.contract(address='0xC959483DBa39aa9E78757139af0e9a2EDEb3f42D', abi=abi)
    transaction = faucet.functions.mint(
        Web3.to_checksum_address(fixture['tokens']['dai']), address, 10_000 * 10**18,
    ).transact({'from': chain.eth.accounts[0]})
    if chain.eth.wait_for_transaction_receipt(transaction)['status'] != 1:
        raise RuntimeError('The local DAI faucet transaction failed.')
    print(f'Funded {address} with 100 local ETH and 10,000 local DAI.', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True, help='Working directory for the local chain test.')
    parser.add_argument('--wallet', help='Fund this test wallet with local ETH and DAI before starting.')
    args = parser.parse_args()
    fixture = configure(args.state_dir)
    if args.wallet:
        fund(args.wallet, fixture)
    log.init(config.LOG_DIR, 'anvil-webui')
    log.set_level(log.Level.INFO)
    log.set_console_enable(True)
    try:
        asyncio.run(webui.run())
    except KeyboardInterrupt:
        pass
    finally:
        log.un_init()
