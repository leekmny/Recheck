"""Native static eligibility is independent of stdin/stdout execution eligibility."""

import asyncio
import struct

import pytest

from recheck.engine import Engine
from recheck.models import Settings, inspect_binary
from recheck.analysis_environment import StaticRouter
from test_binary_admission import portable_executable


@pytest.mark.parametrize('options', [
    {'flags': 0x2002}, {'subsystem': 2},
    {'machine': 0x14C, 'magic': 0x10B},
    {'machine': 0x1C4, 'magic': 0x10B}, {'machine': 0xAA64},
])
def test_native_pe_can_be_compared_statically_without_console_execution(tmp_path, options):
    path = tmp_path / 'target.bin'
    path.write_bytes(portable_executable(**options))
    info = inspect_binary(path)
    assert info['static_supported'] is True
    assert info['execution_supported'] is False
    assert info['static_reason']
    assert info['execution_reason']


def test_managed_pe_uses_separate_static_metadata_route(tmp_path):
    path = tmp_path / 'assembly.dll'
    path.write_bytes(portable_executable(clr=True))
    info = inspect_binary(path)
    assert info['static_supported'] is True
    assert info['analysis_kind'] == 'managed'
    assert info['execution_supported'] is False
    assert '.NET' in info['static_reason']


def test_explicit_direct_backend_never_imports_managed_pe_into_ghidra(tmp_path):
    path = tmp_path / 'assembly.dll'
    path.write_bytes(portable_executable(clr=True))

    class ForbiddenTool:
        async def analyze(self, *args, **kwargs):
            pytest.fail('Managed code must not be treated as a native Ghidra target')

    router = StaticRouter(Settings(static_backend='direct'), direct=ForbiddenTool())
    result = asyncio.run(router.analyze(path, tmp_path / 'work'))
    assert result['status'] == 'blocked'
    assert 'REA' in result['error']


def test_elf_library_retains_static_analysis_without_execution(tmp_path):
    data = bytearray(64)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HH', data, 16, 3, 62)
    path = tmp_path / 'library.so'
    path.write_bytes(data)
    info = inspect_binary(path)
    assert info['static_supported'] is True
    assert info['execution_supported'] is False


def test_elf32_minimum_header_is_a_static_candidate(tmp_path):
    data = bytearray(52)
    data[:7] = b'\x7fELF\x01\x01\x01'
    struct.pack_into('<HH', data, 16, 2, 3)
    path = tmp_path / 'x86.elf'
    path.write_bytes(data)
    info = inspect_binary(path)
    assert info['static_supported'] is True
    assert info['architecture'] == 'x86'
    assert info['execution_supported'] is False


@pytest.mark.parametrize('cpu', [0x1000007, 0x100000C])
def test_thin_macho_can_be_compared_without_a_macos_execution_backend(tmp_path, cpu):
    data = struct.pack('<8I', 0xFEEDFACF, cpu, 3, 2, 1, 24, 0, 0)
    data += struct.pack('<6I', 0x1B, 24, 1, 2, 3, 4)
    path = tmp_path / 'thin.dylib'
    path.write_bytes(data)
    info = inspect_binary(path)
    assert info['format'] == 'Mach-O' and info['os'] == 'macos'
    assert info['static_supported'] is True and info['execution_supported'] is False


def test_macho_load_command_cannot_extend_beyond_declared_table(tmp_path):
    data = struct.pack('<8I', 0xFEEDFACF, 0x1000007, 3, 2, 1, 8, 0, 0)
    data += struct.pack('<2I', 0x1B, 24)
    path = tmp_path / 'bad.macho'
    path.write_bytes(data)
    with pytest.raises(ValueError, match='Mach-O'):
        inspect_binary(path)


@pytest.mark.parametrize('mode', ['automatic', 'static', 'dynamic'])
@pytest.mark.parametrize('cfg_observed', [True, False])
def test_default_comparison_keeps_static_results_for_native_dll(
    tmp_path, monkeypatch, mode, cfg_observed,
):
    target = tmp_path / 'sample.dll'
    target.write_bytes(portable_executable(flags=0x2002))
    engine = Engine(Settings(), tmp_path / 'data')

    class StaticTool:
        async def analyze(self, *args, **kwargs):
            blocks = [dict(id='0x1000', start='0x1000', end='0x1000', size=1)] if cfg_observed else []
            return dict(status='success', complete=True, total_functions=1, functions=[dict(
                id='0x1000', name='entry', address='0x1000', blocks=blocks, edges=[],
                calls=[], mnemonics=['ret'], decompile='return 1;',
            )])

    async def environment(cancel):
        return {'ghidra': {'backend': 'direct', 'ready': True}}

    def no_execution(*args):
        pytest.fail('A DLL must never reach the executable container adapter')

    monkeypatch.setattr(engine, 'environment', environment)
    monkeypatch.setattr(engine, '_ghidra', StaticTool)
    monkeypatch.setattr(engine, '_execution_adapter', no_execution)
    result = asyncio.run(engine.analyze(target, target, [{'id': 'input'}], mode=mode))
    if mode == 'dynamic':
        assert result['stages']['metadata']['status'] == 'failed'
        assert not result['static']
    else:
        assert result['stages']['metadata']['status'] == 'success'
        assert result['stages']['structure']['status'] == 'success'
        if cfg_observed:
            assert len(result['structure']['matches']) == 1
            assert result['structure']['score'] == 1.0
        else:
            assert result['structure']['matches'] == []
            assert result['structure']['score'] is None
            assert result['structure']['score_status'] == 'unavailable'
        assert result['files']['original']['snapshot']
        assert result['stages']['dynamic']['status'] == 'blocked'
        assert result['dynamic'] == []
        # Automatic keeps missing runtime visible rather than declaring full success.
        assert result['status'] == ('success' if mode == 'static' else 'partial')
