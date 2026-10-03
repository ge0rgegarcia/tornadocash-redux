from unittest.mock import Mock
import pytest

from contracts import compiler


@pytest.mark.parametrize('installed', [False, True])
def test_compiler_reports_each_stage_before_it_starts(monkeypatch: pytest.MonkeyPatch, installed: bool) -> None:
    messages: list[str] = []
    download = f'Downloading solc {compiler.SOLC_VERSION}…'
    compiling = f'Compiling batch contracts with solc {compiler.SOLC_VERSION}…'
    monkeypatch.setattr(compiler.solcx, 'get_installed_solc_versions', lambda: [compiler.SOLC_VERSION] if installed else [])

    def install(version: str) -> None:
        assert version == compiler.SOLC_VERSION and messages == [download]

    def compile_standard(data: dict, **kwargs: str) -> dict:
        assert messages == ([compiling] if installed else [download, compiling])
        return {'contracts': {'compiled': True}}

    installer = Mock(side_effect=install)
    monkeypatch.setattr(compiler.solcx, 'install_solc', installer)
    monkeypatch.setattr(compiler.solcx, 'compile_standard', compile_standard)
    assert compiler.compile_contracts({}, messages.append) == {'compiled': True}
    assert installer.call_count == int(not installed)


@pytest.mark.parametrize('failure', ['download', 'compile'])
def test_failed_stage_does_not_report_later_stages(monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    messages: list[str] = []
    monkeypatch.setattr(compiler.solcx, 'get_installed_solc_versions', lambda: [])
    error = RuntimeError('Compiler failed')
    install = Mock(side_effect=error if failure == 'download' else None)
    compile_standard = Mock(side_effect=error)
    monkeypatch.setattr(compiler.solcx, 'install_solc', install)
    monkeypatch.setattr(compiler.solcx, 'compile_standard', compile_standard)
    with pytest.raises(RuntimeError, match='Compiler failed'):
        compiler.compile_contracts({}, messages.append)
    expected = [f'Downloading solc {compiler.SOLC_VERSION}…']
    if failure == 'compile':
        expected.append(f'Compiling batch contracts with solc {compiler.SOLC_VERSION}…')
    assert messages == expected
    assert compile_standard.call_count == int(failure == 'compile')
