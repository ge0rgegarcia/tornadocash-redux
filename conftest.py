from pathlib import Path
from web3 import Web3
import os
import pytest
import shutil
import socket
import subprocess
import time


@pytest.fixture(scope='session')
def anvil(tmp_path_factory):
    binary = shutil.which(os.environ.get('ANVIL_BIN') or 'anvil')
    if binary is None:
        pytest.skip('Add Anvil to PATH or set ANVIL_BIN to run local-chain tests.')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    directory = tmp_path_factory.mktemp('anvil')
    for name in ('tmp', 'cache', 'foundry'):
        (directory / name).mkdir()
    environment = {**os.environ, 'FOUNDRY_DIR': str(directory / 'foundry'),
                   'XDG_CACHE_HOME': str(directory / 'cache'), 'TMPDIR': str(directory / 'tmp')}
    logfile = directory / 'anvil.log'
    with logfile.open('w') as output:
        process = subprocess.Popen([str(Path(binary).resolve()), '--host', '127.0.0.1', '--port', str(port),
                                    '--chain-id', '11155111', '--hardfork', 'shanghai', '--quiet'],
                                   cwd=directory, env=environment, stdout=output, stderr=subprocess.STDOUT)
        web3 = Web3(Web3.HTTPProvider(f'http://127.0.0.1:{port}', request_kwargs={'timeout': 1}))
        try:
            deadline = time.monotonic() + 10
            while not web3.is_connected():
                if process.poll() is not None or time.monotonic() >= deadline:
                    pytest.fail(f'Anvil failed to start. See {logfile}.')
                time.sleep(0.05)
            yield web3
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
