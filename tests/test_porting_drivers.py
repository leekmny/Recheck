import asyncio
import base64
from contextlib import contextmanager
from functools import partial
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import subprocess
import sys
from threading import Event, Thread
import time
from types import SimpleNamespace

import psutil
import pytest


class ExampleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/ready':
            self.send_response(204)
            self.end_headers()
            return
        if self.path == '/redirect':
            self.send_response(302)
            self.send_header('Location', 'http://192.0.2.1/')
            self.end_headers()
            return
        if self.path == '/large':
            body = b'a' * (512 * 1024 + 1)
            self.send_response(200)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        result = json.dumps([self.path, self.headers['X-Scenario'], body.decode()]).encode()
        self.send_response(422)
        self.send_header('X-App', 'demo')
        self.send_header('Content-Length', str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def log_message(self, *args):
        pass


@contextmanager
def listening(handler=ExampleHandler):
    with ThreadingHTTPServer(('127.0.0.1', 0), handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        thread.start()
        try:
            yield f'http://127.0.0.1:{server.server_port}'
        finally:
            server.shutdown()
            thread.join()


def run_observer(kind, steps, directory, **kwargs):
    from recheck.porting_drivers import observe

    return asyncio.run(observe(kind, steps, directory, deadline=time.monotonic() + 10, **kwargs))


def test_http_ready_input_and_error_response(tmp_path):
    with listening() as base:
        observations = run_observer('http', [
            {'id': 'start', 'action': 'ready', 'path': '/ready', 'status': 204},
            {'id': 'invalid', 'action': 'http', 'path': '/api', 'method': 'POST',
             'query': [('input', 'a+b'), ('input', '')], 'headers': {'X-Scenario': 'case'},
             'body': 'negative', 'observe_headers': ['x-app', 'x-missing']},
            {'id': 'redirect', 'action': 'http', 'path': '/redirect'},
        ], tmp_path, base_url=base)

    assert observations['start'] == {'ready': True}
    assert observations['invalid']['status'] == 422
    assert observations['invalid']['headers'] == {'x-app': ['demo'], 'x-missing': []}
    assert json.loads(base64.b64decode(observations['invalid']['body_b64'])) == [
        '/api?input=a%2Bb&input=', 'case', 'negative',
    ]
    assert observations['redirect']['status'] == 302


@pytest.mark.parametrize('destination', ['//192.0.2.1/', '/\\192.0.2.1/', 'file:///a'])
def test_http_blocks_non_local_step_and_preserves_observations(tmp_path, destination):
    from recheck.porting_drivers import DriverConfigurationError

    completed = {}
    with listening() as base, pytest.raises(DriverConfigurationError) as failure:
        run_observer('http', [
            {'id': 'start', 'action': 'ready', 'path': '/ready'},
            {'id': 'blocked', 'action': 'http', 'path': destination},
        ], tmp_path, base_url=base, observations=completed)
    assert failure.value.step_id == 'blocked'
    assert completed == {'start': {'ready': True}}


def test_http_readiness_wrong_status_does_not_count_as_response(tmp_path):
    from recheck.porting_drivers import ObservationTimeout, observe

    completed = {}
    with listening() as base, pytest.raises(ObservationTimeout) as failure:
        asyncio.run(observe('http', [{'id': 'not-ready', 'action': 'ready', 'path': '/missing',
                                      'timeout_seconds': 0.6}], tmp_path, base_url=base,
                            deadline=time.monotonic() + 5, observations=completed))
    assert failure.value.evidence['last_status'] == 404
    assert completed == {}


def test_http_retains_bounded_partial_response_without_claiming_success(tmp_path):
    from recheck.porting_drivers import ObservationError

    completed = {}
    with listening() as base, pytest.raises(ObservationError) as failure:
        run_observer('http', [{'id': 'large', 'action': 'http', 'path': '/large'}],
                     tmp_path, base_url=base, observations=completed)
    assert completed == {}
    assert failure.value.evidence['status'] == 200
    assert failure.value.evidence['truncated'] is True
    assert len(base64.b64decode(failure.value.evidence['body_b64'])) == 512 * 1024


def test_files_capture_absolute_changes_and_missing_paths(tmp_path):
    (tmp_path / 'output').mkdir()
    (tmp_path / 'output' / 'b.txt').write_bytes(b'\xff\r\n')
    (tmp_path / 'output' / 'a.txt').write_text('old', encoding='utf-8')
    steps = [{'id': 'files', 'action': 'files', 'paths': ['output', 'deleted', 'created']}]
    (tmp_path / 'deleted').write_bytes(b'delete me')
    before = run_observer('console', steps, tmp_path)['files']
    (tmp_path / 'deleted').unlink()
    (tmp_path / 'created').write_bytes(b'new')
    (tmp_path / 'output' / 'a.txt').write_text('fresh', encoding='utf-8')
    after = run_observer('console', steps, tmp_path)['files']

    assert before['created'] == after['deleted'] == {'kind': 'missing'}
    assert after['output'] == {'kind': 'directory'}
    assert list(after) == ['output', 'output/a.txt', 'output/b.txt', 'deleted', 'created']
    assert before['output/a.txt']['content_b64'] != after['output/a.txt']['content_b64']
    assert base64.b64decode(after['output/b.txt']['content_b64']) == b'\xff\r\n'


def test_files_refuse_parent_traversal(tmp_path):
    from recheck.porting_drivers import DriverConfigurationError

    with pytest.raises(DriverConfigurationError):
        run_observer('console', [{'id': 'files', 'action': 'files', 'paths': ['../secret']}],
                     tmp_path)


def test_sqlite_reads_explicit_fields_and_distinguishes_missing(tmp_path):
    database = tmp_path / '상태 기록.sqlite'
    steps = [{'id': 'state', 'action': 'sqlite', 'path': database.name,
              'table': 'order', 'columns': ['id', 'payload', 'comment'], 'order_by': ['id']}]
    assert run_observer('file', steps, tmp_path)['state'] == {'exists': False}
    with sqlite3.connect(database) as connection:
        connection.execute('CREATE TABLE "order" (id INTEGER, payload BLOB, comment TEXT)')
        connection.executemany('INSERT INTO "order" VALUES (?, ?, ?)', [
            (2, b'\xff', None), (1, b'', ''),
        ])

    state = run_observer('file', steps, tmp_path)['state']
    assert state == {'exists': True, 'table': 'order', 'columns': ['id', 'payload', 'comment'],
                     'rows': [[1, {'content_b64': ''}, ''], [2, {'content_b64': '/w=='}, None]]}


def test_sqlite_rejects_nonexistent_columns_instead_of_sql_string_literal(tmp_path):
    from recheck.porting_drivers import ObservationError

    with sqlite3.connect(tmp_path / 'db') as connection:
        connection.execute('CREATE TABLE existing (id INTEGER)')
    with pytest.raises(ObservationError, match='unknown column'):
        run_observer('file', [{'id': 'state', 'action': 'sqlite', 'path': 'db',
                               'table': 'existing', 'columns': ['missing']}], tmp_path)


@pytest.mark.parametrize('variant, page, field, button, result', [
    ('original', '/original.html', 'Weight', 'Quote', '250'),
    ('port', '/port.html', 'Mass', 'Estimate', '250'),
    ('defect', '/port.html?defect=1', 'Mass', 'Estimate', '249'),
])
def test_browser_real_workflow(tmp_path, variant, page, field, button, result):
    pytest.importorskip('playwright.async_api', reason='Playwright worker dependency unavailable')
    from recheck.porting_drivers import DriverUnavailable

    assets = Path(__file__).resolve().parents[1] / 'samples/porting/web'
    steps = [
        {'id': 'ready', 'action': 'ready', 'path': page},
        {'id': 'open', 'action': 'navigate', 'path': page},
        {'id': 'input', 'action': 'fill', 'locator': {'label': field}, 'value': '10'},
        {'id': 'quote', 'action': 'click', 'locator': {'role': 'button', 'name': button}},
        {'id': 'done', 'action': 'wait', 'locator': {'role': 'status'}, 'text': result},
        {'id': 'price', 'action': 'read', 'locator': {'role': 'status'}, 'property': 'text'},
        {'id': 'card', 'action': 'download', 'locator': {'role': 'link', 'name': 'Download rate card'}},
        {'id': 'look', 'action': 'screenshot'},
    ]
    with listening(partial(SimpleHTTPRequestHandler, directory=assets)) as base:
        try:
            observed = run_observer('browser', steps, tmp_path, base_url=base)
        except DriverUnavailable as exc:
            pytest.skip(str(exc))
    assert observed['price'] == result, variant
    assert base64.b64decode(observed['card']['content_b64']).startswith(b'Base fee 100.')
    assert observed['look']['visual_only'] is True
    assert base64.b64decode(observed['look']['content_b64']).startswith(b'\x89PNG\r\n\x1a\n')


def test_browser_blocks_a_redirect_to_another_loopback_service(tmp_path):
    pytest.importorskip('playwright.async_api', reason='Playwright worker dependency unavailable')
    from recheck.porting_drivers import DriverUnavailable, ObservationError

    visits = []

    class OtherService(ExampleHandler):
        def do_GET(self):
            visits.append(self.path)
            super().do_GET()

    with listening(OtherService) as forbidden:
        class Redirector(ExampleHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', forbidden + '/ready')
                self.end_headers()

        with listening(Redirector) as allowed:
            try:
                run_observer('browser', [{'id': 'redirect', 'action': 'navigate', 'path': '/'}],
                             tmp_path, base_url=allowed)
            except DriverUnavailable as exc:
                pytest.skip(str(exc))
            except ObservationError:
                pass
            else:
                pytest.fail('Cross-origin redirect should fail')
    assert visits == []


def test_html_renders_visual_evidence_separately_from_agreement(tmp_path):
    from recheck.porting_report import render_porting_report

    png = ('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8'
           '/x8AAwMCAO+aN1EAAAAASUVORK5CYII=')
    valid = {'visual_only': True, 'media_type': 'image/png', 'content_b64': png}
    invalid = {**valid, 'content_b64': '" onerror="unsafe'}
    run = {'run_id': 'test', 'status': 'success', 'scenarios': [{
        'id': '<sample>', 'kind': 'browser', 'outcome': 'matched', 'attempts': [{
            'index': 1, 'comparison': {'expected': {'reference': {'status': 'passed'},
                                                    'candidate': {'status': 'passed'}}},
            'observations': {side: {'observations': {'screen': valid, 'bad': invalid}}
                             for side in ('reference', 'candidate')},
        }],
    }]}
    rendered = render_porting_report(run, tmp_path / 'report.html').read_text(encoding='utf-8')
    assert '시각 관찰' in rendered
    assert rendered.count(f'src="data:image/png;base64,{png}"') == 2
    assert 'PNG 바이트 일치' in rendered
    assert '&lt;sample&gt;' in rendered
    assert '<sample>' not in rendered
    assert 'onerror="unsafe' not in rendered


def test_native_harness_rejects_missing_signature(tmp_path):
    from recheck.porting_drivers import DriverConfigurationError

    with pytest.raises(DriverConfigurationError, match='ABI'):
        run_observer('library', [{'id': 'bad', 'action': 'call', 'symbol': 'strnlen'}],
                     tmp_path)


@pytest.mark.skipif(os.name != 'nt', reason='Windows system ABI smoke test')
def test_native_harness_invokes_real_library(tmp_path):
    # Read a known platform DLL for the harness mechanics check. Build the
    # project-owned C/Rust fixtures separately to verify two ported libraries.
    source = Path(os.environ['SystemRoot']) / 'System32' / 'kernel32.dll'
    target = tmp_path / 'native copy.dll'
    shutil.copyfile(source, target)
    steps = [{'id': 'call', 'action': 'call', 'abi': 'stdcall', 'symbol': 'lstrlenA',
              'arg_types': ['cstring'], 'return_type': 'i32', 'args': ['test']}]
    result = run_observer('library', steps, tmp_path, target=target)['call']
    assert result['return'] == 4
    assert result['harness']['returncode'] == 0
    assert result['signature']['abi'] == 'stdcall'


def test_desktop_requires_worker_owned_pid(tmp_path):
    from recheck.porting_drivers import DriverConfigurationError

    with pytest.raises(DriverConfigurationError, match='PID'):
        run_observer('desktop', [{'id': 'window', 'action': 'ready', 'locator': {'name': 'Test'}}],
                     tmp_path)


def test_atspi_click_accepts_qt_press_action():
    from recheck.porting_desktop import LinuxBackend

    # Qt's real AT-SPI button advertises "Press", while GTK commonly uses "click".
    names = ['Set Focus', 'Press']
    invoked = []

    def invoke(index):
        invoked.append(names[index])
        return True

    action = SimpleNamespace(get_n_actions=lambda: len(names), get_action_name=names.__getitem__,
                             do_action=invoke)
    node = SimpleNamespace(get_action_iface=lambda: action)
    LinuxBackend.click(None, node)
    assert invoked == ['Press']


def test_desktop_native_timeout_does_not_block_worker_shutdown(tmp_path, monkeypatch):
    from recheck.porting_desktop import Desktop
    from recheck.porting_drivers import ObservationTimeout, observe

    # Exercise an unresponsive adapter call without requiring the runner's input desktop.
    stuck = Event()
    entered = Event()

    def hung_call(*_):
        entered.set()
        stuck.wait(timeout=2)

    monkeypatch.setattr(Desktop, '_once', hung_call)
    before = time.monotonic()
    try:
        with pytest.raises(ObservationTimeout):
            asyncio.run(observe('desktop', [
                {'id': 'window', 'action': 'ready', 'locator': {'name': 'Unresponsive'}},
            ], tmp_path, pid=os.getpid(), deadline=before + 0.2))
        assert entered.is_set()
        assert time.monotonic() - before < 1.25
    finally:
        stuck.set()


ASSETS = Path(__file__).resolve().parents[1] / 'samples/porting'


def test_native_buffer_and_error_contract_with_compiled_sample(tmp_path):
    tool = shutil.which('gcc') or shutil.which('clang')
    if not tool:
        pytest.skip('C compiler is unavailable for the bundled ABI fixture')
    if sys.platform == 'win32':
        name, flags = 'quote.dll', ['-shared']
    elif sys.platform == 'darwin':
        name, flags = 'quote.dylib', ['-dynamiclib']
    else:
        name, flags = 'quote.so', ['-shared', '-fPIC']
    target = tmp_path / name
    subprocess.run([tool, *flags, ASSETS / 'library/original.c', '-o', target],
                   check=True, capture_output=True, timeout=20)
    signature = {'action': 'call', 'symbol': 'parcel_quote', 'abi': 'cdecl',
                 'arg_types': ['i32', 'buffer', 'u32'], 'return_type': 'i32'}
    good = {'id': 'good', **signature, 'args': [10, {'size': 2}, 2]}
    bad = {'id': 'bad', **signature, 'args': [-1, {'size': 2, 'initial_b64': 'AQI='}, 2]}
    result = run_observer('library', [good, bad], tmp_path, target=target)
    assert result['good']['return'] == 0
    assert result['good']['buffers'] == {'1': '+gA='}
    assert result['bad']['return'] == -1
    assert result['bad']['buffers'] == {'1': 'AQI='}


@pytest.mark.parametrize('kind', ['http', 'web', 'desktop', 'state', 'library'])
def test_sample_generator_creates_distinct_valid_targets(tmp_path, kind):
    from recheck.porting_manifest import load_manifest, pack_target

    shutil.copyfile(ASSETS / 'make_scenario.py', tmp_path / 'make_scenario.py')
    shutil.copytree(ASSETS / kind, tmp_path / kind)
    path = tmp_path / kind / 'generated.json'
    subprocess.run([sys.executable, tmp_path / 'make_scenario.py', kind, '--os', 'linux',
                    '--defect', '--output', path], check=True, capture_output=True, timeout=5)
    manifest, root = load_manifest(path)
    fixtures = manifest['scenarios'][0].get('fixtures', [])
    hashes = [pack_target(root, spec, fixtures)['sha256'] for spec in manifest['targets'].values()]
    assert hashes[0] != hashes[1]


def node_executable():
    executable = shutil.which('node')
    if not executable:
        pytest.skip('Node.js worker runtime unavailable')
    return executable


@contextmanager
def started(argv, cwd, **extra):
    # Only the bundled fixtures are executed in these smoke tests.
    with subprocess.Popen(argv, cwd=cwd, **extra) as process:
        try:
            yield process
        finally:
            if process.poll() is None:
                children = psutil.Process(process.pid).children(recursive=True)
                for child in reversed(children):
                    try:
                        child.terminate()
                    except psutil.NoSuchProcess:
                        pass
                process.terminate()
            process.wait(timeout=5)


def unused_local_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize('variant, expected', [('original', 1600), ('port', 1600), ('defect', 1599)])
def test_http_observes_independent_server_implementations(tmp_path, variant, expected):
    if variant == 'original':
        command, health, quote = [sys.executable, ASSETS / 'http/original.py'], '/health', '/quote'
    else:
        command, health, quote = [node_executable(), ASSETS / 'http/port.mjs'], '/status', '/price'
        if variant == 'defect':
            command.append('--defect')
    port = unused_local_port()
    scenario = [
        {'id': 'ready', 'action': 'ready', 'path': health, 'status': 204},
        {'id': 'valid', 'action': 'http', 'path': quote, 'method': 'POST',
         'observe_headers': ['Content-Type'], 'json': {'weight': '100'}, 'query': {'unit': 'kg'}},
        {'id': 'invalid', 'action': 'http', 'path': quote, 'method': 'POST',
         'observe_headers': ['Content-Type'], 'json': {'weight': '-1'}, 'query': {'unit': 'kg'}},
    ]
    with started(command, tmp_path, env={**os.environ, 'RECHECK_PORT': str(port)}):
        result = run_observer('http', scenario, tmp_path, base_url=f'http://127.0.0.1:{port}')
    assert result['valid']['status'] == 200
    assert json.loads(base64.b64decode(result['valid']['body_b64'])) == {'amount': expected}
    assert result['invalid']['status'] == 422
    assert json.loads(base64.b64decode(result['invalid']['body_b64'])) == {'error': 'weight'}
    assert result['valid']['headers'] == {'content-type': ['application/json']}


@pytest.mark.parametrize('variant, expected', [('original', 250), ('port', 250), ('defect', 249)])
def test_files_and_sqlite_detect_a_state_only_defect(tmp_path, variant, expected):
    if variant == 'original':
        command = [sys.executable, ASSETS / 'state/original.py', '10']
    else:
        node = node_executable()
        support = subprocess.run([node, '-e', 'require("node:sqlite")'], capture_output=True, timeout=5)
        if support.returncode != 0:
            pytest.skip('Node.js bundled node:sqlite unavailable')
        command = [node, ASSETS / 'state/port.mjs', '10']
        if variant == 'defect':
            command.append('--defect')
    for name in ('status.txt', 'obsolete.txt'):
        shutil.copyfile(ASSETS / 'state' / name, tmp_path / name)
    steps = [
        {'id': 'files', 'action': 'files', 'paths': ['status.txt', 'obsolete.txt', 'ledger']},
        {'id': 'state', 'action': 'sqlite', 'path': 'parcel.sqlite', 'table': 'quote',
         'columns': ['id', 'amount']},
    ]
    before = run_observer('console', steps, tmp_path)
    subprocess.run(command, cwd=tmp_path, check=True, capture_output=True, timeout=5)
    after = run_observer('console', steps, tmp_path)
    assert before['state'] == {'exists': False}
    assert before['files']['ledger'] == after['files']['obsolete.txt'] == {'kind': 'missing'}
    assert before['files']['status.txt']['sha256'] != after['files']['status.txt']['sha256']
    assert base64.b64decode(after['files']['ledger/created.txt']['content_b64']) == b'quote=250\n'
    assert after['state']['rows'] == [[1, expected]]


@pytest.mark.skipif(os.environ.get('RECHECK_TEST_INTERACTIVE_DESKTOP') != '1',
                    reason='Requires an opted-in interactive validation desktop')
@pytest.mark.parametrize('variant, expected', [('original', '250'), ('port', '250'), ('defect', '249')])
def test_desktop_real_workflow(tmp_path, variant, expected):
    if variant == 'original':
        script, window, field, button, result = (
            'original.py', 'Parcel quote', 'Weight', 'Quote', 'Quote result',
        )
    else:
        script, window, field, button, result = (
            'port.py', 'Postal estimate', 'Mass', 'Estimate', 'Estimate result',
        )
    command = [sys.executable, ASSETS / 'desktop' / script]
    if variant == 'defect':
        command.append('--defect')
    steps = [
        {'id': 'ready', 'action': 'ready', 'locator': {'window': window}},
        {'id': 'fill', 'action': 'fill', 'locator': {'window': window, 'name': field}, 'value': '10'},
        {'id': 'click', 'action': 'click', 'locator': {'window': window, 'name': button}},
        {'id': 'wait', 'action': 'wait', 'locator': {'window': window, 'name': result}},
        {'id': 'price', 'action': 'read', 'locator': {'window': window, 'name': result}, 'property': 'value'},
    ]
    with started(command, tmp_path) as process:
        observed = run_observer('desktop', steps, tmp_path, pid=process.pid)
    assert observed['price'] == expected
