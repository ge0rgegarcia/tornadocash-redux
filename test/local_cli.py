"""Exercise the real batch CLI and Groth16 proofs on the running local fork."""

import argparse
import json
from pathlib import Path

from local_webui import ROOT, configure

from web3 import Web3
from eth_account import Account

import cli
import config
from components import log
from components.mytype import Key


def main(state: Path) -> None:
    metadata = configure(state)
    (state / 'result.json').unlink(missing_ok=True)
    existing_backups = set(Path(config.BACKUP_DIR).glob('*.txt'))
    config.ZK_WORKERS = 2
    chain = Web3(Web3.HTTPProvider('http://127.0.0.1:18545', request_kwargs={'timeout': 90}))
    assert chain.eth.chain_id == 31337
    key = Key(Account.create().key.hex())
    account = key.eth_address()
    recipient = Web3.to_checksum_address('0x' + 'ab' * 20)
    chain.provider.make_request('anvil_setBalance', [account, hex(100 * 10**18)])
    abi = json.loads((ROOT / 'abi/erc20.json').read_text())
    abi += [{'type': 'function', 'name': 'owner', 'inputs': [], 'outputs': [{'type': 'address'}], 'stateMutability': 'view'},
            {'type': 'function', 'name': 'mint', 'inputs': [{'name': 'to', 'type': 'address'},
              {'name': 'amount', 'type': 'uint256'}], 'outputs': [{'type': 'bool'}], 'stateMutability': 'nonpayable'}]
    token = chain.eth.contract(address=Web3.to_checksum_address(metadata['tokens']['dai']), abi=abi)
    owner = token.functions.owner().call()
    chain.provider.make_request('anvil_impersonateAccount', [owner])
    chain.provider.make_request('anvil_setBalance', [owner, hex(100 * 10**18)])
    try:
        transaction = token.functions.mint(account, 100000 * 10**18).transact({'from': owner, 'gas': 1000000})
        assert chain.eth.wait_for_transaction_receipt(transaction).status == 1
    finally:
        chain.provider.make_request('anvil_stopImpersonatingAccount', [owner])
    before = chain.eth.get_balance(recipient), token.functions.balanceOf(recipient).call()
    transactions = []
    send = cli.batch_tx.send

    def record(*args):
        transaction = send(*args)
        if transaction is not None:
            transactions.append(transaction.to_0x_hex())
        return transaction

    cli.batch_tx.send = record
    log.init(config.LOG_DIR, 'anvil-cli')
    log.set_level(log.Level.INFO)
    try:
        for batch in ({'eth': {'0.1': 2}}, {'dai': {'100': 1, '1000': 1}},
                      {'eth': {'0.1': 1}, 'dai': {'100': 1}}):
            cli.deposit_batch(key, repr({'sepolia': batch}))
        notes = [path.read_text().splitlines()[0].removeprefix('Note: ')
                 for path in sorted(set(Path(config.BACKUP_DIR).glob('*.txt')) - existing_backups)]
        assert len(notes) == 6
        cli.withdraw_batch(','.join(notes), recipient, key)
        after = chain.eth.get_balance(recipient), token.functions.balanceOf(recipient).call()
        assert after[0] - before[0] == 3 * 10**17
        assert after[1] - before[1] == 1200 * 10**18
        receipts = [chain.eth.get_transaction_receipt(transaction) for transaction in transactions]
        assert len(receipts) == 8 and all(receipt.status == 1 for receipt in receipts)
        report = {'chain_id': 31337, 'cases': ['native deposit', 'ERC20 deposit', 'mixed deposit',
                  'six real Groth16 proofs and one wallet withdrawal'], 'notes': 6,
                  'recipient_native_wei': after[0] - before[0], 'recipient_dai_wei': after[1] - before[1],
                  'transactions': [{'hash': receipt.transactionHash.to_0x_hex(), 'status': receipt.status,
                                    'gas_used': receipt.gasUsed} for receipt in receipts]}
        (state / 'result.json').write_text(json.dumps(report, indent=2) + '\n')
    finally:
        log.un_init()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, required=True, help='Working directory for the local chain test.')
    main(parser.parse_args().state_dir)
