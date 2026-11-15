from hexbytes import HexBytes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from web3 import Web3
import argparse
import json
import threading


def create_server(deployment: dict, port: int = 18081) -> ThreadingHTTPServer:
    if deployment['rpc'] != 'http://127.0.0.1:18545':
        raise ValueError('The test relayer only uses the local Anvil RPC.')
    web3 = Web3(Web3.HTTPProvider(deployment['rpc']))
    chain_id = web3.eth.chain_id
    if chain_id != 31337:
        raise ValueError('The local RPC is not the disposable Anvil chain.')
    account = web3.eth.accounts[0]
    abi = json.loads((Path(__file__).parents[1] / 'contracts/abi/tornado.json').read_text())
    instances = {symbol: {'instanceAddress': pools} for symbol, pools in deployment['pools'].items()}
    status = {'netId': chain_id, 'rewardAccount': account, 'instances': instances,
              'ethPrices': {'dai': str(5 * 10**14)}}
    state = {'requests': 0, 'fail_on': 0, 'jobs': {}}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):

        def log_message(self, format: str, *args) -> None:
            pass

        def reply(self, code: int, value: dict) -> None:
            data = json.dumps(value).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == '/status':
                self.reply(200, status)
            elif self.path == '/control':
                self.reply(200, state)
            elif self.path.startswith('/v1/jobs/'):
                job = state['jobs'].get(self.path.rsplit('/', 1)[-1])
                self.reply(200 if job else 404, job or {'error': 'Unknown job'})
            else:
                self.reply(404, {'error': 'Unknown endpoint'})

        def do_POST(self) -> None:
            try:
                body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
                if self.path == '/control':
                    with lock:
                        state['fail_on'] = state['requests'] + int(body['fail_after']) + 1
                    self.reply(200, state)
                    return
                if self.path != '/v1/tornadoWithdraw':
                    self.reply(404, {'error': 'Unknown endpoint'})
                    return
                with lock:
                    state['requests'] += 1
                    identifier = str(state['requests'])
                    job = {'id': identifier, 'status': 'FAILED'}
                    state['jobs'][identifier] = job
                    if state['requests'] == state['fail_on']:
                        job['failedReason'] = 'Controlled test failure'
                    else:
                        try:
                            root, nullifier, recipient, relayer, fee, refund = body['args']
                            number = lambda value: int(value, 0) if isinstance(value, str) else value
                            pool = web3.eth.contract(address=Web3.to_checksum_address(body['contract']), abi=abi)
                            transaction = pool.functions.withdraw(
                                HexBytes(body['proof']), number(root).to_bytes(32, 'big'), number(nullifier).to_bytes(32, 'big'),
                                Web3.to_checksum_address(recipient), Web3.to_checksum_address(relayer), number(fee), number(refund),
                            ).transact({'from': account, 'value': number(refund), 'gas': 3000000})
                            receipt = web3.eth.wait_for_transaction_receipt(transaction)
                            job['txHash'] = transaction.to_0x_hex()
                            job['status'] = 'CONFIRMED' if receipt['status'] == 1 else 'FAILED'
                            if receipt['status'] != 1:
                                job['failedReason'] = 'Withdrawal reverted'
                        except Exception as error:
                            job['failedReason'] = str(error)
                self.reply(200, {'id': identifier})
            except Exception as error:
                self.reply(400, {'error': str(error)})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture', type=Path, required=True, help='Path to the local-chain fixture JSON.')
    parser.add_argument('--port', type=int, default=18081)
    arguments = parser.parse_args()
    with create_server(json.loads(arguments.fixture.read_text()), arguments.port) as server:
        print(f'Local relayer: http://127.0.0.1:{server.server_port}', flush=True)
        server.serve_forever()
