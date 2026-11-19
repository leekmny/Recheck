"""Run explicit native signatures in a disposable, bounded child process."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory

from .porting_drivers import (
    DriverConfigurationError, DriverUnavailable, MAX_BYTES, ObservationError, ObservationTimeout,
    decode_bytes, remaining, text_field, within_directory,
)
from .porting_library_child import SCALARS
from .process import run_process


def _signature(step):
    abi = step.get('abi')
    if abi not in ('cdecl', 'stdcall'):
        raise DriverConfigurationError('Library call requires an explicit ABI (cdecl or stdcall)')
    if abi == 'stdcall' and os.name != 'nt':
        raise DriverUnavailable('The stdcall loader is available only on Windows')
    symbol = text_field(step.get('symbol'), 'symbol')
    if len(symbol) > 512:
        raise DriverConfigurationError('Library symbol name exceeds the length limit')
    arg_types, result_type, args = (step.get('arg_types'), step.get('return_type'), step.get('args'))
    if (not isinstance(arg_types, list) or len(arg_types) > 32 or not isinstance(args, list)
            or len(arg_types) != len(args)):
        raise DriverConfigurationError('Library call requires one declared type for every argument')
    if result_type not in (*SCALARS, 'void'):
        raise DriverConfigurationError('Unsupported library return type')

    budget = MAX_BYTES // 4
    for arg_type, value in zip(arg_types, args):
        if arg_type == 'buffer':
            if not isinstance(value, dict) or set(value) - {'size', 'initial_b64'}:
                raise DriverConfigurationError('A buffer argument requires size and optional initial_b64')
            size = value.get('size')
            if type(size) is not int or not 1 <= size <= budget:
                raise DriverConfigurationError('Library buffer size exceeds the observation budget')
            if len(decode_bytes(value.get('initial_b64', ''))) > size:
                raise DriverConfigurationError('Buffer data exceeds its declared size')
            budget -= size
        elif arg_type == 'cstring':
            budget -= len(text_field(value, 'cstring', empty=True).encode('utf-8')) + 1
        elif arg_type in ('f32', 'f64'):
            if type(value) not in (int, float) or not math.isfinite(value):
                raise DriverConfigurationError('Library float arguments must be finite numbers')
            if arg_type == 'f32' and abs(value) > 3.4028234663852886e38:
                raise DriverConfigurationError('Library float argument exceeds the f32 range')
        elif isinstance(arg_type, str) and (matched := re.fullmatch(r'([iu])(8|16|32|64)', arg_type)):
            signed = matched.group(1) == 'i'
            bits = int(matched.group(2)) - signed
            low = -(1 << bits) if signed else 0
            if type(value) is not int or not low <= value < (1 << bits):
                raise DriverConfigurationError(f'Library integer does not fit {arg_type}')
        else:
            raise DriverConfigurationError('Unsupported library argument type')
        if budget < 0:
            raise DriverConfigurationError('Library arguments exceed the byte limit')
    return {'abi': abi, 'symbol': symbol, 'arg_types': arg_types, 'return_type': result_type}


async def call_library(step: dict, target: Path | None, target_root: Path, directory: Path,
                       deadline: float, *, memory_limit: int | None, env: dict | None) -> dict:
    signature = _signature(step)
    if target is None:
        raise DriverConfigurationError('Library call requires a staged target path')
    if getattr(sys, 'frozen', False):
        raise DriverUnavailable('Run the library worker from a Python installation')
    target = Path(target)
    target_root = target_root.resolve(strict=True)
    if target.is_absolute():
        try:
            target = target.relative_to(target_root)
        except ValueError as exc:
            raise DriverConfigurationError('Library target must be inside the run directory') from exc
    target = within_directory(target_root, target.as_posix())
    if not target.is_file():
        raise DriverConfigurationError('Library target is not a staged file')

    parameters = json.dumps({**signature, 'args': step['args']}, allow_nan=False).encode('utf-8')
    child = Path(__file__).with_name('porting_library_child.py').resolve()
    pid = None

    def started(value):
        nonlocal pid
        pid = value

    with TemporaryDirectory(prefix='recheck-native-call-') as scratch:
        result_path = Path(scratch) / 'result.json'
        run = await run_process([sys.executable, '-I', child, target, result_path], stdin=parameters,
                                cwd=directory, env=env, on_start=started, kill_descendants=True,
                                memory_limit=memory_limit, timeout=remaining(deadline),
                                output_limit=MAX_BYTES)
        harness = {'tool': 'python-ctypes', 'started': pid is not None,
                   'returncode': run['returncode'], 'stdout_b64': run['stdout_b64'],
                   'stderr_b64': run['stderr_b64'], 'truncated': run['truncated']}
        evidence = {'signature': signature, 'harness': harness}
        if run['cleanup_errors']:
            raise ObservationError('Library harness cleanup failed', evidence={
                **evidence, 'cleanup_errors': run['cleanup_errors'],
            })
        if run['timed_out']:
            raise ObservationTimeout('Library call timed out', evidence=evidence)
        if run['truncated'] or run['error']:
            raise ObservationError('Library harness failed to collect complete output', evidence={
                **evidence, 'process_error': run['error'],
            })
        if run['returncode'] != 0:
            raise ObservationError('Library harness exited before producing a function result',
                                   evidence=evidence)
        try:
            with result_path.open('rb') as source:
                raw = source.read(MAX_BYTES + 1)
            if len(raw) > MAX_BYTES:
                raise ValueError('result exceeds byte limit')
            reply = json.loads(raw)
        except (OSError, ValueError) as exc:
            raise ObservationError('Library harness response is missing or malformed',
                                   evidence=evidence) from exc
        if not isinstance(reply, dict) or set(reply) not in (
            {'return', 'buffers', 'errno', 'last_error'}, {'error'},
        ):
            raise ObservationError('Library harness returned an invalid response', evidence=evidence)
        if 'error' in reply:
            raise ObservationError('Native library load or invocation failed',
                                   evidence={**evidence, 'error': reply['error']})
        return {**evidence, **reply}
