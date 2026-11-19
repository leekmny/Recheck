"""Generate a porting manifest beside the bundled sample's sources."""

import argparse
import base64
import hashlib
import json
from pathlib import Path


def as_b64(value):
    return base64.b64encode(value).decode('ascii')


def both(expected):
    return {'reference': expected, 'candidate': expected}


def http_pair(port, defect, _os):
    steps = [{'id': 'health', 'action': 'ready', 'path': '/health', 'status': 204}]
    expected = {}
    for name, weight, status, result in (
        ('ten', '10', 200, {'amount': 250}), ('zero', '0', 200, {'amount': 100}),
        ('upper_bound', '100', 200, {'amount': 1600}), ('negative', '-1', 422, {'error': 'weight'}),
        ('empty', '', 422, {'error': 'weight'}), ('bad_format', '4kg', 422, {'error': 'weight'}),
    ):
        steps.append({'id': name, 'action': 'http', 'method': 'POST', 'path': '/quote',
                      'query': {'unit': 'kg'}, 'json': {'weight': weight},
                      'observe_headers': ['Content-Type']})
        expected[f'step:{name}'] = {'status': status, 'headers': {'content-type': ['application/json']},
                                    'body_b64': as_b64(json.dumps(result, separators=(',', ':')).encode())}
    return {'entry': 'original.py', 'runtime': 'python', 'language': 'Python'}, {
        'entry': 'port.mjs', 'runtime': 'node', 'language': 'JavaScript',
        'args': ['--defect'] if defect else [],
    }, [{'id': 'quoted_prices', 'kind': 'http', 'base_url': f'http://127.0.0.1:{port}',
         'steps': steps, 'mapping': {'candidate': {
             'reason': 'Both routes quote the same weight in kilograms.',
             'steps': {step['id']: {'path': '/price' if step['action'] == 'http' else '/status'}
                       for step in steps},
         }}, 'expected': both(expected)}]


def browser_pair(port, defect, _os):
    def target(page):
        return {'entry': 'serve.py', 'runtime': 'python', 'language': 'JavaScript',
                'files': [page, 'quote.txt']}

    original, ported = '/original.html', '/port.html?defect=1' if defect else '/port.html'
    steps = [
        {'id': 'ready', 'action': 'ready', 'path': original},
        {'id': 'page', 'action': 'navigate', 'path': original},
        {'id': 'fill', 'action': 'fill', 'locator': {'label': 'Weight'}, 'value': '10'},
        {'id': 'quote', 'action': 'click', 'locator': {'role': 'button', 'name': 'Quote'}},
        {'id': 'display', 'action': 'wait', 'locator': {'role': 'status'}},
        {'id': 'price', 'action': 'read', 'locator': {'role': 'status'}},
        {'id': 'download', 'action': 'download',
         'locator': {'role': 'link', 'name': 'Download rate card'}},
        {'id': 'visual', 'action': 'screenshot'},
    ]
    return target('original.html'), target('port.html'), [{
        'id': 'parcel_quote', 'kind': 'browser', 'base_url': f'http://127.0.0.1:{port}',
        'steps': steps, 'mapping': {'candidate': {
            'reason': 'The mass input and estimate action implement the same quote workflow.',
            'steps': {'ready': {'path': ported}, 'page': {'path': ported},
                      'fill': {'locator': {'label': 'Mass'}},
                      'quote': {'locator': {'role': 'button', 'name': 'Estimate'}}},
        }}, 'expected': both({'step:price': '250'}),
    }]


def desktop_pair(_port, defect, os_name):
    def locate(window, name=None):
        return {'window': window} if name is None else {'window': window, 'name': name}

    original, ported = 'Parcel quote', 'Postal estimate'
    display = {}
    if os_name == 'linux':
        display['env'] = {'QT_LINUX_ACCESSIBILITY_ALWAYS_ON': '1', 'QT_XCB_GL_INTEGRATION': 'none'}
    steps = [
        {'id': 'ready', 'action': 'ready', 'locator': locate(original)},
        {'id': 'fill', 'action': 'fill', 'locator': locate(original, 'Weight'), 'value': '10'},
        {'id': 'quote', 'action': 'click', 'locator': locate(original, 'Quote')},
        {'id': 'display', 'action': 'wait', 'locator': locate(original, 'Quote result')},
        {'id': 'price', 'action': 'read', 'locator': locate(original, 'Quote result'), 'property': 'value'},
    ]
    return {'entry': 'original.py', 'runtime': 'python', 'language': 'Python', **display}, {
        'entry': 'port.py', 'runtime': 'python', 'language': 'Python',
        'args': ['--defect'] if defect else [], **display,
    }, [{'id': 'parcel_quote', 'kind': 'desktop', 'steps': steps, 'mapping': {'candidate': {
        'reason': 'The mapped controls implement the same quote workflow in a different window.',
        'steps': {id_: {'locator': locate(ported, name)} for id_, name in (
            ('ready', None), ('fill', 'Mass'), ('quote', 'Estimate'),
            ('display', 'Estimate result'), ('price', 'Estimate result'),
        )},
    }}, 'expected': both({'step:price': '250'})}]


def library_pair(_port, defect, os_name):
    suffix = {'windows': 'dll', 'linux': 'so', 'macos': 'dylib'}[os_name]
    original, ported = f'original.{suffix}', f'port.{suffix}'
    c_flags = {'windows': ['-shared'], 'linux': ['-fPIC', '-shared'], 'macos': ['-dynamiclib']}[os_name]
    signature = {'abi': 'cdecl', 'symbol': 'parcel_quote', 'arg_types': ['i32', 'buffer', 'u32'],
                 'return_type': 'i32'}
    harness = {'tool': 'python-ctypes', 'started': True, 'returncode': 0, 'stdout_b64': '',
               'stderr_b64': '', 'truncated': False}
    scenarios = []
    for name, weight, size, code, buffer in (
        ('valid_quote', 10, 2, 0, b'\xfa\x00'), ('upper_bound', 100, 2, 0, b'\x40\x06'),
        ('negative_rejected', -1, 2, -1, b'\x00\x00'), ('short_buffer', 1, 1, -2, b'\x00'),
    ):
        expected = {'step:call': {'signature': signature, 'harness': harness, 'return': code,
                                   'buffers': {'1': as_b64(buffer)}, 'errno': 0,
                                   'last_error': 0 if os_name == 'windows' else None}}
        scenarios.append({'id': name, 'kind': 'library', 'expected': both(expected), 'steps': [
            {'id': 'call', 'action': 'call', **signature, 'args': [weight, {'size': size}, size]},
        ]})
    return {
        'entry': original, 'runtime': 'native', 'language': 'C', 'files': ['original.c'],
        'build': {'tool': 'clang' if os_name == 'macos' else 'gcc',
                  'args': ['-std=c11', *c_flags, '-O2', 'original.c', '-o', original]},
    }, {
        'entry': ported, 'runtime': 'native', 'language': 'Rust', 'files': ['port.rs'],
        'build': {'tool': 'rustc', 'args': [
            '--edition=2021', '--crate-type', 'cdylib', '-O', 'port.rs', '-o', ported,
            *(['--cfg', 'defect'] if defect else []),
        ]},
    }, scenarios


def state_pair(_port, defect, _os):
    def file_state(body):
        return {'kind': 'file', 'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
                'content_b64': as_b64(body)}

    expected = {'exit_code': 0, 'step:after_db': {'exists': True, 'table': 'quote',
                'columns': ['id', 'amount'], 'rows': [[1, 250]]}, 'step:after_files': {
                    'status.txt': file_state(b'quoted\n'), 'obsolete.txt': {'kind': 'missing'},
                    'ledger': {'kind': 'directory'}, 'ledger/created.txt': file_state(b'quote=250\n'),
                }}
    files = {'action': 'files', 'paths': ['status.txt', 'obsolete.txt', 'ledger']}
    state = {'action': 'sqlite', 'path': 'parcel.sqlite', 'table': 'quote',
             'columns': ['id', 'amount'], 'order_by': ['id']}
    return {'entry': 'original.py', 'runtime': 'python', 'language': 'Python'}, {
        'entry': 'port.mjs', 'runtime': 'node', 'language': 'JavaScript',
    }, [{'id': 'state_after_quote', 'kind': 'console', 'fixtures': ['status.txt', 'obsolete.txt'],
         'input': {'args': ['10'], 'env': {'NODE_NO_WARNINGS': '1'}},
         'mapping': {'candidate': {'reason': 'Both programs consume the same weight.',
                                   'arg_prefix': ['--defect'] if defect else []}},
         'prepare': [{'id': 'before_files', **files}, {'id': 'before_db', **state}],
         'steps': [{'id': 'after_files', **files}, {'id': 'after_db', **state}],
         'expected': both(expected)}]


EXAMPLES = {'http': http_pair, 'web': browser_pair, 'desktop': desktop_pair,
            'library': library_pair, 'state': state_pair}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('example', choices=EXAMPLES)
    parser.add_argument('--os', required=True, choices=['linux', 'macos', 'windows'])
    parser.add_argument('--arch', default='x64', choices=['x64', 'arm64'])
    parser.add_argument('--worker', default='default')
    parser.add_argument('--port', default=47625, type=int)
    parser.add_argument('--defect', action='store_true')
    parser.add_argument('--output', type=Path, required=True,
                        help='Manifest path in the example source directory')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be from 1 to 65535')
    if args.output.resolve().parent != (Path(__file__).resolve().parent / args.example):
        parser.error('Place --output in the selected example directory')
    reference, candidate, scenarios = EXAMPLES[args.example](args.port, args.defect, args.os)
    shared = {'os': args.os, 'arch': args.arch, 'worker': args.worker}
    data = {'schema_version': 'recheck-porting-1',
            'targets': {'reference': dict(reference, **shared), 'candidate': dict(candidate, **shared)},
            'scenarios': scenarios}
    if args.example == 'web':
        # The Python entry only serves the pages. Their application code is inline JavaScript.
        data['structure'] = {'enabled': False}
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
        output.write('\n')


if __name__ == '__main__':
    main()
