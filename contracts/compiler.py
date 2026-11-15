from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Callable
import json
import solcx
import threading

from util import log


SOLC_VERSION = '0.8.30'
SETTINGS = {
    'optimizer': {'enabled': True, 'runs': 200},
    'evmVersion': 'paris',
    'outputSelection': {'*': {'*': ['abi', 'evm.bytecode.object']}},
}
_compile_lock = threading.Lock()


def source_files(directory: Path) -> dict[str, dict[str, str]]:
    sources = {path.name: {'content': path.read_text()} for path in (directory / 'src').glob('*.sol')}
    vendor = directory / 'vendor/openzeppelin'
    sources.update({
        '@openzeppelin/contracts/' + path.relative_to(vendor).as_posix(): {'content': path.read_text()}
        for path in vendor.rglob('*.sol')
    })
    return sources


def compile_contracts(sources: dict[str, dict[str, str]], on_progress: Callable[[str], None] | None = None) -> dict:
    if SOLC_VERSION not in map(str, solcx.get_installed_solc_versions()):
        if on_progress:
            on_progress(f'Downloading solc {SOLC_VERSION}…')
        solcx.install_solc(SOLC_VERSION)
    if on_progress:
        on_progress(f'Compiling batch contracts with solc {SOLC_VERSION}…')
    result = solcx.compile_standard({
        'language': 'Solidity', 'sources': sources, 'settings': SETTINGS,
    }, solc_version=SOLC_VERSION)
    return result['contracts']


def batch_depositor_artifact(directory: Path, mixed: bool,
                             on_progress: Callable[[str], None] | None = None) -> tuple[str, str]:
    cache_path = directory / 'build/batch_depositors.json'
    with _compile_lock:
        sources = source_files(directory)
        source_hash = sha256(json.dumps({
            'version': SOLC_VERSION, 'settings': SETTINGS, 'sources': sources,
        }, sort_keys=True).encode()).hexdigest()
        try:
            artifact = json.loads(cache_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            artifact = {}
        if artifact.get('source_hash') != source_hash:
            try:
                compiled = compile_contracts(sources, on_progress)
            except Exception as error:
                log.error('Batch', f'Failed to compile batch contracts: {error}')
                raise RuntimeError('Batch contract compilation failed. Check backend logs.') from error
            contracts = {}
            for name in ('BatchDepositorUniversal', 'BatchDepositorERC20'):
                contract = compiled[name + '.sol'][name]
                contracts[name] = {'abi': contract['abi'], 'bytecode': '0x' + contract['evm']['bytecode']['object']}
            artifact = {'source_hash': source_hash, 'contracts': contracts}
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                with NamedTemporaryFile(mode='w', dir=cache_path.parent, delete=False) as temporary:
                    json.dump(artifact, temporary)
                Path(temporary.name).replace(cache_path)
            except OSError as error:
                log.error('Batch', f'Failed to cache compiled batch contracts: {error}')
                raise RuntimeError('Could not cache compiled batch contracts. Check backend logs.') from error
        name = 'BatchDepositorUniversal' if mixed else 'BatchDepositorERC20'
        contract = artifact['contracts'][name]
        return json.dumps(contract['abi']), contract['bytecode']
