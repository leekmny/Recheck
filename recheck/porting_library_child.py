"""Executable used only by the isolated worker's native-call driver."""

from __future__ import annotations

import base64
import ctypes
import json
import math
import os
from pathlib import Path
import sys


SCALARS = {
    'i8': ctypes.c_int8, 'u8': ctypes.c_uint8, 'i16': ctypes.c_int16, 'u16': ctypes.c_uint16,
    'i32': ctypes.c_int32, 'u32': ctypes.c_uint32, 'i64': ctypes.c_int64, 'u64': ctypes.c_uint64,
    'f32': ctypes.c_float, 'f64': ctypes.c_double,
}
ARGUMENTS = {**SCALARS, 'cstring': ctypes.c_char_p, 'buffer': ctypes.POINTER(ctypes.c_ubyte)}


def invoke(path, spec):
    windows = os.name == 'nt'
    if windows:
        # Disable crash dialogs in this process so the worker receives an exit status.
        ctypes.WinDLL('kernel32').SetErrorMode(0x0001 | 0x0002)
    else:
        import resource

        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    loader = ctypes.WinDLL if spec['abi'] == 'stdcall' else ctypes.CDLL
    library = loader(path, use_errno=True, use_last_error=windows)
    function = getattr(library, spec['symbol'])
    function.argtypes = [ARGUMENTS[item] for item in spec['arg_types']]
    function.restype = SCALARS.get(spec['return_type'])  # None is a declared void result.
    buffers = {}
    native_args = []
    for index, (kind, value) in enumerate(zip(spec['arg_types'], spec['args'])):
        if kind == 'buffer':
            allocation = (ctypes.c_ubyte * value['size'])()
            data = base64.b64decode(value.get('initial_b64', ''), validate=True)
            allocation[:len(data)] = data
            buffers[str(index)] = allocation
            native_args.append(allocation)
        elif kind == 'cstring':
            native_args.append(value.encode('utf-8'))
        else:
            native_args.append(value)
    ctypes.set_errno(0)
    if windows:
        ctypes.set_last_error(0)
    result = function(*native_args)
    errno = ctypes.get_errno()
    last_error = ctypes.get_last_error() if windows else None
    if isinstance(result, float) and not math.isfinite(result):
        result = {'native_float': str(result)}
    return {'return': result, 'errno': errno, 'last_error': last_error,
            'buffers': {key: base64.b64encode(bytes(value)).decode('ascii')
                        for key, value in buffers.items()}}


def main():
    source, result_path = sys.argv[1:]
    try:
        sample = json.load(sys.stdin)
        result = invoke(source, sample)
    except (OSError, ValueError, TypeError, AttributeError, ctypes.ArgumentError) as exc:
        result = {'error': {'type': type(exc).__name__, 'message': str(exc)[:1024]}}
    Path(result_path).write_text(json.dumps(result, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
