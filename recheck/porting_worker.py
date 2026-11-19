"""Single-job execution protocol for an explicitly isolated worker."""
from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import platform
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

from .porting_manifest import (
    BUILT_ENTRY_RUNTIMES, MAX_ENTRY_CAPTURE, MAX_MESSAGE, MAX_OBSERVATION, PROTOCOL, SIDES,
    applied_scenario, bundle_identity, canonical, digest, identifier, json_loads, relative,
    safe_source, target_spec, validate_scenario,
)
from .process import run_process

RUNTIME = {'python': 'python', 'node': 'node', 'typescript': 'node',
           'java': 'java', 'dotnet': 'dotnet'}
TOOLS = set(RUNTIME.values()) | {'gcc', 'g++', 'clang', 'clang++', 'rustc', 'go', 'tsc',
                                 'javac', 'cmake', 'make', 'cargo', 'cl'}
WORKER_MODULES = (
    '__init__.py', 'paths.py', 'process.py', 'porting_manifest.py', 'porting_worker.py',
    'porting_drivers.py', 'porting_browser.py', 'porting_desktop.py', 'porting_library.py',
    'porting_library_child.py',
)


def worker_platform():
    os_name = {'win32': 'windows', 'darwin': 'macos'}.get(sys.platform, sys.platform)
    machine = platform.machine().lower()
    arch = {'amd64': 'x64', 'x86_64': 'x64', 'aarch64': 'arm64'}.get(machine, machine)
    return {'os': os_name, 'arch': arch}


def worker_code_sha256():
    directory = Path(__file__).resolve().parent
    hashes = {name: digest((directory / name).read_bytes()) for name in WORKER_MODULES}
    return digest(canonical(hashes))


def execution_architecture():
    if sys.platform == 'win32':
        import ctypes
        from ctypes import wintypes

        try:
            api = ctypes.WinDLL('kernel32', use_last_error=True)
            api.GetCurrentProcess.restype = wintypes.HANDLE
            check = api.IsWow64Process2
            check.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.USHORT),
                              ctypes.POINTER(wintypes.USHORT))
            check.restype = wintypes.BOOL
            process, native = wintypes.USHORT(), wintypes.USHORT()
            if check(api.GetCurrentProcess(), ctypes.byref(process), ctypes.byref(native)):
                arch = {0x8664: 'x64', 0xaa64: 'arm64'}.get(native.value)
                if arch is not None:
                    return {'native_arch': arch, 'emulated': process.value not in (0, native.value)}
        except (OSError, AttributeError):
            pass
    elif sys.platform == 'darwin':
        try:
            probe = subprocess.run(['/usr/sbin/sysctl', '-in', 'sysctl.proc_translated'],
                                   capture_output=True, timeout=2, check=False)
            if probe.returncode == 0 and probe.stdout.strip() in (b'0', b'1'):
                translated = probe.stdout.strip() == b'1'
                return {'native_arch': 'arm64' if translated else worker_platform()['arch'],
                        'emulated': translated}
        except (OSError, subprocess.TimeoutExpired):
            pass
    return {'native_arch': None, 'emulated': None}


def environment():
    name = os.environ.get('RECHECK_WORKER_ID')
    isolation = os.environ.get('RECHECK_WORKER_ISOLATION')
    if not name or isolation not in {'dedicated-vm', 'docker'}:
        raise ValueError('Configure an explicit worker ID and isolation.')
    return dict(worker_platform(), id=name, isolation=isolation, os_version=platform.version(),
                python=platform.python_version(), worker_code_sha256=worker_code_sha256(),
                **execution_architecture())


def native_file(path):
    with path.open('rb') as stream:
        header = stream.read(64)
        if header.startswith(b'\x7fELF') and len(header) >= 32:
            if header[4] != 2 or header[5] not in (1, 2):
                raise ValueError('A 64-bit ELF with a valid byte order is required.')
            order = '<' if header[5] == 1 else '>'
            file_type, machine = struct.unpack_from(order + 'HH', header, 16)
            if file_type in (2, 3):
                entry = struct.unpack_from(order + 'Q', header, 24)[0]
                return 'linux', {62: 'x64', 183: 'arm64'}.get(machine), file_type == 3 and entry == 0
        if header.startswith(b'MZ') and len(header) >= 64:
            stream.seek(struct.unpack_from('<I', header, 60)[0])
            pe = stream.read(26)
            if len(pe) >= 26 and pe[:4] == b'PE\0\0':
                machine = struct.unpack_from('<H', pe, 4)[0]
                flags, magic = struct.unpack_from('<HH', pe, 22)
                if magic == 0x20b and flags & 2:
                    return 'windows', {0x8664: 'x64', 0xaa64: 'arm64'}.get(machine), bool(flags & 0x2000)
        if header[:4] in (b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf') and len(header) >= 32:
            cpu, _, file_type = struct.unpack_from('<III' if header[0] == 0xcf else '>III', header, 4)
            if file_type in (2, 6):
                return 'macos', {0x1000007: 'x64', 0x100000c: 'arm64'}.get(cpu), file_type == 6
    raise ValueError('A 64-bit ELF, PE, or thin Mach-O is required.')


def _tool(name):
    if name not in TOOLS:
        raise ValueError(f'Unconfigured tool: {name}')
    executable = sys.executable if name == 'python' else shutil.which(name)
    if not executable or Path(executable).suffix.lower() in ('.bat', '.cmd'):
        raise ValueError(f'Tool is unavailable as a direct executable: {name}')
    return str(Path(executable).absolute())


def _result(result, pid, controlled=False):
    code = result.get('returncode')
    status = 'completed'
    if result.get('cleanup_errors'):
        status, reason = 'observation_error', 'cleanup_failed'
    elif result.get('error'):
        status, reason = ('observation_error', 'capture_failed') if pid else (
            'environment_error', 'start_failed')
    elif result.get('timed_out'):
        status, reason = 'timed_out', 'deadline'
    elif result.get('truncated'):
        status, reason = 'output_truncated', 'output_limit'
    elif result.get('cancelled'):
        status, reason = ('completed', 'driver_shutdown') if controlled else ('cancelled', 'cancelled')
        code = None
    elif code == 0:
        reason = 'exit_zero'
    elif code is not None and os.name != 'nt' and code < 0:
        reason = 'signal'
    elif code is not None and os.name == 'nt' and code & 0xffffffff in {
        0xc0000005, 0xc000001d, 0xc0000094, 0xc00000fd, 0xc0000409,
    }:
        reason = 'windows_exception'
    else:
        reason = 'nonzero_unclassified'
    return dict(status=status, reason=reason, exit_code=code, started=bool(pid),
                **{key: result[key] for key in ('argv', 'duration', 'stdout_b64', 'stderr_b64',
                                                 'error', 'cleanup_errors')})


async def _version(executable, name, deadline, *, cwd, env, cancel, flag=None):
    flag = flag or ('-version' if name in ('java', 'javac') else '--version')
    result = await run_process([executable, flag], timeout=max(0.01, min(5, deadline - time.monotonic())),
                               output_limit=16384, cwd=cwd, env=env, cancel=cancel)
    return dict(executable=executable, reported=(result['stdout'] + result['stderr']).strip()
                if result['returncode'] == 0 and not result['truncated'] else None,
                probe_arg=flag, source='observed')


def _dotnet_framework(root, entry):
    path = entry.with_suffix('.runtimeconfig.json').relative_to(root).as_posix()
    try:
        with safe_source(root, path).open('rb') as stream:
            content = stream.read(65537)
        if len(content) > 65536:
            return None
        options = json_loads(content.decode('utf-8-sig'))['runtimeOptions']
        if isinstance(options, dict) and not {'framework', 'frameworks'} <= options.keys():
            references = options.get('frameworks', [options.get('framework')])
            if (isinstance(references, list) and len(references) == 1
                    and isinstance(references[0], dict) and isinstance(references[0].get('name'), str)):
                return references[0]['name']
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


async def _runtime_identity(name, expected, entry, root, deadline, **probe):
    executable = _tool(name)
    evidence = await _version(executable, name, deadline, **probe,
                              flag='--list-runtimes' if name == 'dotnet' else None)
    reported = evidence.get('reported') or ''
    evidence.update(requested=expected, version=None)
    if name == 'dotnet':
        frameworks = [dict(name=match[1], version=match[2])
                      for line in reported.splitlines()
                      if (match := re.fullmatch(r'(\S+)\s+(\S+)\s+\[.+\]', line.strip()))]
        app_framework = _dotnet_framework(root, entry)
        evidence.update(frameworks=frameworks, app_framework=app_framework)
        if expected and {'name': app_framework, 'version': expected} in frameworks:
            evidence.update(version=expected, selection='fx-version')
    else:
        pattern = {'python': r'^Python (\S+)$', 'node': r'^v(\S+)$',
                   'java': r'^(?:openjdk|java) version "([^"]+)"'}[name]
        match = re.search(pattern, reported, flags=re.MULTILINE)
        if match:
            evidence['version'] = match[1]
    return evidence


def _java_classpath_entry(path, root):
    if os.pathsep in str(path):
        raise ValueError(f'Java classpath entry contains the platform path separator: {path.name}')
    current = root
    info = root.lstat()
    for part in path.relative_to(root).parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ValueError(f'Java classpath may not follow a linked path: {current.name}')
    if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
        raise ValueError(f'Java classpath requires a directory or regular archive: {path.name}')
    return stat.S_ISDIR(info.st_mode)


def _java_archive_provides_class(path, class_name, declared):
    if path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError(f'Java classpath archive exceeds 64 MiB: {path.name}')
    try:
        with zipfile.ZipFile(path) as archive:
            if class_name in archive.namelist():
                return True
            for item in archive.infolist():
                name = item.filename
                if name.startswith('META-INF/versions/') and name.endswith('/' + class_name):
                    if declared:
                        raise ValueError(f'Java entry archive must contain the base main class: {path.name}')
                    return True
                if name.upper() != 'META-INF/MANIFEST.MF':
                    continue
                with archive.open(item) as stream:
                    content = stream.read(65537)
                if len(content) > 65536:
                    raise ValueError(f'Java classpath manifest exceeds 64 KiB: {path.name}')
                for line in content.splitlines():
                    if not line:
                        break
                    if line.lower().startswith(b'class-path:'):
                        raise ValueError(
                            f'Java classpath archive before entry has an implicit Class-Path: {path.name}')
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise ValueError(f'Java classpath archive could not be inspected: {path.name}') from exc
    return False


def _java_classpath(target, entry, root, directory):
    main = target['java_main']
    class_name = main.replace('.', '/') + '.class'
    paths = []
    identified = False
    for item in target.get('java_classpath', ['.']):
        path = directory if item == '.' else root / relative(item)
        is_directory = _java_classpath_entry(path, root)
        paths.append(str(path))
        if identified:
            continue
        provider = None
        if is_directory:
            filename = (path / class_name).relative_to(root).as_posix()
            try:
                provider = safe_source(root, filename)
            except FileNotFoundError:
                pass
        elif _java_archive_provides_class(path, class_name, path == entry):
            provider = path
        if provider is not None:
            if provider != entry:
                raise ValueError(f'Java main class {main} is shadowed by {provider.relative_to(root)}.')
            identified = True
    if not identified:
        raise ValueError(f'Java classpath main {main} does not resolve to the declared entry.')
    return ['-cp', os.pathsep.join(paths), main]


def _command(target, entry, root, directory):
    if target['runtime'] == 'native':
        system, arch, library = native_file(entry)
        if (system, arch) != (target['os'], target['arch']) or library:
            raise ValueError(f'Cannot launch native artifact {system}/{arch} as configured.')
        if os.name != 'nt':
            entry.chmod(0o700)
        return [str(entry)]
    executable = _tool(RUNTIME[target['runtime']])
    if target['runtime'] != 'java':
        return [executable, str(entry)]
    if 'java_main' in target:
        return [executable, *_java_classpath(target, entry, root, directory)]
    if entry.suffix.lower() != '.jar':
        raise ValueError('Java requires a JAR or explicit java_main and java_classpath.')
    return [executable, '-jar', str(entry)]



class _Cancellation:
    def __init__(self, remote, local):
        self.remote = remote
        self.local = local

    def is_set(self):
        return self.remote.is_set() or self.local.is_set()


async def _execute(request, root, target, observation, cancel, capture=None):
    from .porting_drivers import DriverError, observe

    scenario, applied = request['scenario'], request['applied']
    kind = scenario.get('kind', 'console')
    deadline = time.monotonic() + scenario.get('timeout', 20)
    home = root.parent / 'home'
    home.mkdir(mode=0o700)
    directory = root if target.get('cwd', '.') == '.' else root / relative(target['cwd'])
    directory.mkdir(parents=True, exist_ok=True)
    inherited = {key: os.environ[key] for key in ('PATH', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'LANG',
                 'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR', 'RUSTUP_HOME',
                 'CARGO_HOME', 'DBUS_SESSION_BUS_ADDRESS') if key in os.environ}
    env = dict(inherited, HOME=str(home), USERPROFILE=str(home), TMP=str(home), TEMP=str(home),
               TMPDIR=str(home), PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
               DOTNET_CLI_HOME=str(home), DOTNET_CLI_TELEMETRY_OPTOUT='1',
               DOTNET_NOLOGO='1', TZ='UTC')
    if os.name != 'nt':
        env.setdefault('XDG_RUNTIME_DIR', str(home))
    env.update(target.get('env', {}))
    env.update(applied['env'])
    url = applied['base_url']
    if url:
        parsed = urlsplit(url)
        if parsed.hostname not in ('127.0.0.1', '::1') or parsed.port is None:
            raise ValueError('Worker services require a literal loopback host and explicit port.')
        env['RECHECK_PORT'] = str(parsed.port)
    pid = None
    stop = threading.Event()
    memory = scenario.get('memory_mb', 512) * 1024 * 1024

    def on_start(value):
        nonlocal pid
        pid = value
        observation['process_executions'] += 1

    async def process(argv, stdin=b''):
        return await run_process(
            argv, stdin=stdin, cwd=directory, env=env, cancel=_Cancellation(cancel, stop),
            on_start=on_start,
            timeout=max(0.01, deadline - time.monotonic()), memory_limit=memory,
            kill_descendants=True, output_limit=scenario.get('output_limit', 262144),
        )

    if build := target.get('build'):
        if request.get('allow_build') is not True:
            observation.update(status='blocked', reason='build_not_approved')
            return
        executable = _tool(build['tool'])
        observation['build_tool'] = await _version(executable, build['tool'], deadline,
                                                   cwd=directory, env=env, cancel=cancel)
        built = await process([executable, *build.get('args', [])])
        observation['build'] = output = _result(built, pid)
        if output['status'] != 'completed' or output['exit_code'] != 0:
            observation.update(status=output['status'] if output['status'] != 'completed'
                               else 'environment_error', reason='build_failed')
            return
        pid = None
    entry = safe_source(root, target['entry'])
    with entry.open('rb') as stream:
        artifact = stream.read(64 * 1024 * 1024 + 1)
    if len(artifact) > 64 * 1024 * 1024:
        raise ValueError('Entry file exceeds 64 MiB.')
    observation['entry_sha256'] = digest(artifact)
    if capture is not None:
        if len(artifact) <= MAX_ENTRY_CAPTURE:
            capture.update(status='available', data_b64=base64.b64encode(artifact).decode('ascii'))
        else:
            capture['status'] = 'size_limit'
    if target['runtime'] in RUNTIME:
        tool = RUNTIME[target['runtime']]
        expected = target.get('runtime_version')
        observation['runtime'] = version = await _runtime_identity(
            tool, expected, entry, root, deadline, cwd=directory, env=env, cancel=cancel)
        if cancel.is_set():
            observation.update(status='cancelled', reason='controller_disconnected_or_cancelled')
            return
        if expected is not None and expected != version['version']:
            observation.update(status='environment_error', reason='runtime_version_mismatch')
            return

    async def steps(driver, phase, **kwargs):
        if not applied[phase]:
            return
        observing = asyncio.create_task(observe(
            driver, applied[phase], directory, deadline=deadline,
            observations=observation['observations'], **kwargs))
        try:
            while not observing.done():
                if cancel.is_set():
                    raise asyncio.CancelledError
                if task is not None and task.done() and phase == 'steps':
                    raise DriverError('Target process exited during observation.')
                await asyncio.wait((observing,), timeout=0.03)
            await observing
        finally:
            if not observing.done():
                observing.cancel()
                await asyncio.gather(observing, return_exceptions=True)

    task = None
    try:
        await steps('console', 'prepare')
        if kind == 'library':
            if target['runtime'] != 'native' or native_file(entry) != (target['os'], target['arch'], True):
                raise ValueError('Library header does not match worker platform.')
            await steps(kind, 'steps', target=entry, target_root=root, memory_limit=memory, env=env)
            observation.update(status='completed', reason='library_calls_completed', started=True)
        else:
            command = [*_command(target, entry, root, directory), *target.get('args', []),
                       *applied['args']]
            if observation.get('runtime', {}).get('selection') == 'fx-version':
                command[1:1] = ['exec', '--fx-version', observation['runtime']['version']]
            stdin = base64.b64decode(applied['stdin_b64'], validate=True)
            if kind == 'console':
                observation.update(_result(await process(command, stdin), pid))
                if observation['status'] == 'completed':
                    await steps(kind, 'steps')
            else:
                if kind in ('http', 'browser') and not url:
                    raise ValueError('HTTP and browser scenarios require base_url.')
                if url and kind in ('http', 'browser'):
                    try:
                        _, connected = await asyncio.wait_for(
                            asyncio.open_connection(parsed.hostname, parsed.port), timeout=0.3)
                    except (OSError, TimeoutError):
                        pass
                    else:
                        connected.close()
                        await connected.wait_closed()
                        raise ValueError('The selected loopback port was occupied before launch.')
                task = asyncio.create_task(process(command, stdin))
                while pid is None and not task.done() and time.monotonic() < deadline:
                    await asyncio.sleep(0.02)
                if pid is not None and not task.done():
                    await steps(kind, 'steps', base_url=url, pid=pid)
                controlled = not task.done() and not cancel.is_set()
                stop.set()
                observation.update(_result(await task, pid, controlled))
                if not controlled and observation['status'] == 'completed':
                    observation.update(status='observation_error', reason='target_exited_early')
        if observation['status'] == 'completed':
            await steps('console', 'cleanup')
    except asyncio.CancelledError:
        if task:
            stop.set()
            observation.update(_result(await task, pid))
        observation.update(status='cancelled', reason='controller_disconnected_or_cancelled')
    except DriverError as exc:
        if task:
            stop.set()
            observation.update(_result(await task, pid, controlled=True))
        status = {'ObservationTimeout': 'timed_out', 'DriverUnavailable': 'unsupported',
                  'DriverConfigurationError': 'environment_error'}.get(type(exc).__name__,
                                                                       'observation_error')
        observation.update(status=status, reason=type(exc).__name__, step_id=exc.step_id,
                           error=str(exc), driver_evidence=getattr(exc, 'evidence', None))
    finally:
        if task and not task.done():
            stop.set()
            await task
        if kind == 'library':
            calls = sum(step['action'] == 'call' and step['id'] in observation['observations']
                        for step in applied['steps'])
            failed = observation.get('driver_evidence') or {}
            observation['process_executions'] += calls + int(bool(failed.get('harness', {}).get('started')))


def _bound_observation(observation):
    steps = observation['observations']
    base = dict(observation, observations={})
    # A canonical member contributes its key and value plus a comma, except for the last one.
    step_sizes = {step_id: len(canonical({step_id: value})) - 1 for step_id, value in steps.items()}
    step_bytes = sum(step_sizes.values())
    if len(canonical(base)) + max(0, step_bytes - 1) <= MAX_OBSERVATION:
        return

    driver_evidence = base.pop('driver_evidence', None)
    omitted_fields = ['driver_evidence'] if driver_evidence is not None else []
    omitted_steps = []
    details = dict(prior_status=base['status'], prior_reason=base['reason'],
                   omitted_step_ids=omitted_steps, omitted_step_count=0, omitted_fields=omitted_fields)
    base.update(status='output_truncated', reason='observation_limit', observation_limit=details)
    # Even without step values, escaped command arguments or diagnostic text can exhaust the cap.
    worst_details = dict(details, omitted_step_ids=list(steps), omitted_step_count=len(steps))
    build = base.get('build', {})
    for record, key, label in ((base, 'argv', 'argv'), (build, 'argv', 'build.argv'),
                               (base, 'error', 'error'), (build, 'error', 'build.error'),
                               (base, 'cleanup_errors', 'cleanup_errors'),
                               (build, 'cleanup_errors', 'build.cleanup_errors')):
        if len(canonical(dict(base, observation_limit=worst_details))) <= MAX_OBSERVATION:
            break
        if key in record:
            del record[key]
            omitted_fields.append(label)

    base_bytes = len(canonical(base)) - len(canonical(details))
    while steps and base_bytes + max(0, step_bytes - 1) + len(canonical(details)) > MAX_OBSERVATION:
        step_id, _ = steps.popitem()
        step_bytes -= step_sizes[step_id]
        omitted_steps.append(step_id)
        details['omitted_step_count'] += 1
    omitted_steps.reverse()
    base['observations'] = steps
    if driver_evidence is not None:
        omitted_fields.remove('driver_evidence')
        if len(canonical(dict(base, driver_evidence=driver_evidence))) <= MAX_OBSERVATION:
            base['driver_evidence'] = driver_evidence
        else:
            omitted_fields.insert(0, 'driver_evidence')
    observation.clear()
    observation.update(base)


async def handle(request, worker, cancel=None):
    if request.get('protocol') != PROTOCOL:
        raise ValueError('Unknown worker protocol.')
    job_id = identifier(request.get('job_id'))
    result = dict(protocol=PROTOCOL, job_id=job_id, environment=worker)
    if request.get('op') == 'probe':
        return dict(result, status='ready')
    if request.get('op') != 'run':
        raise ValueError('Unknown worker operation.')
    result['request_sha256'] = digest(canonical(request))
    target = request['target']
    target_spec(target)
    validate_scenario(request['scenario'])
    side = request.get('side')
    if side not in SIDES or request.get('applied') != applied_scenario(request['scenario'], side):
        raise ValueError('Applied inputs differ from the declared logical mapping.')
    bundle = request['bundle']
    files = {relative(name): base64.b64decode(data, validate=True)
             for name, data in bundle['files'].items()}
    identity = bundle_identity(files)
    if bundle['sha256'] != identity['sha256']:
        raise ValueError('Target bundle SHA-256 mismatch.')
    result['target_sha256'] = identity['sha256']
    observation = dict(status='environment_error', reason='setup', observations={}, started=False,
                       process_executions=0, stdout_b64='', stderr_b64='', exit_code=None,
                       target_sha256=identity['sha256'])
    result['observation'] = observation
    if (worker['os'], worker['arch']) != (target['os'], target['arch']):
        observation.update(status='unsupported', reason='platform_mismatch')
        return result
    capture = {} if (request.get('capture_entry') is True and 'build' in target
                     and target['runtime'] in BUILT_ENTRY_RUNTIMES) else None
    try:
        with tempfile.TemporaryDirectory(prefix='recheck worker ') as name:
            root = Path(name) / 'project'
            root.mkdir()
            for relative_name, data in files.items():
                path = root / relative_name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            await _execute(request, root, target, observation,
                           cancel if cancel is not None else threading.Event(), capture)
    except (OSError, ValueError, ImportError) as exc:
        observation.update(status='environment_error', reason='worker_setup_failed', error=str(exc))
    _bound_observation(observation)
    if capture and observation['started']:
        result['entry_capture'] = capture
    return result


def main():
    try:
        stream = sys.stdin.buffer
        size = struct.unpack('>I', stream.read(4))[0]
        if not 1 <= size <= MAX_MESSAGE:
            raise ValueError('Worker request exceeds 32 MiB.')
        data = stream.read(size)
        if len(data) != size:
            raise ValueError('Worker request ended before the declared frame length.')
        cancel = threading.Event()

        def wait_for_disconnect():
            try:
                os.read(stream.fileno(), 1)
            except OSError:
                pass
            finally:
                cancel.set()

        threading.Thread(target=wait_for_disconnect, daemon=True).start()
        with contextlib.redirect_stdout(sys.stderr):
            reply = asyncio.run(handle(json_loads(data), environment(), cancel=cancel))
        sys.stdout.buffer.write(canonical(reply))
        return 0
    except Exception as exc:
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
