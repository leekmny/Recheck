"""Ordered scenario observations collected inside a configured execution worker."""

from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import AsyncExitStack, closing
import hashlib
import ipaddress
import json
import math
import ntpath
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import stat
import time
from urllib.parse import urlsplit

import httpx


MAX_BYTES = 512 * 1024
MAX_ENTRIES = 512
HTTP_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")


class DriverError(Exception):
    def __init__(self, message: str, *, evidence: dict | None = None):
        super().__init__(message)
        self.step_id: str | None = None
        self.evidence = evidence or {}


class DriverConfigurationError(DriverError):
    """The scenario cannot be applied as configured."""


class DriverUnavailable(DriverError):
    """The worker lacks an observation dependency or required environment."""


class ObservationError(DriverError):
    """The step did not produce a complete, valid observation."""


class ObservationTimeout(ObservationError):
    """An observation did not complete before its declared deadline."""


def remaining(deadline: float) -> float:
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise ObservationTimeout('Observation deadline elapsed')
    return seconds


def encode_bytes(value: bytes) -> str:
    return base64.b64encode(value).decode('ascii')


def decode_bytes(value: object) -> bytes:
    try:
        if not isinstance(value, str):
            raise TypeError
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise DriverConfigurationError('Expected a base64 string') from exc


def text_field(value, name, *, empty=False) -> str:
    if not isinstance(value, str) or (not value and not empty) or '\0' in value:
        raise DriverConfigurationError(f'{name} must be a string')
    return value


def within_directory(directory: Path, name: str) -> Path:
    """Resolve an explicit path without traversing symlinks or Windows junctions."""
    name = text_field(name, 'path')
    parts = PureWindowsPath(name)
    reserved = getattr(ntpath, 'isreserved', None)  # Available since Python 3.13.
    if parts.anchor or any(
        part == '..' or ':' in part or (reserved(part) if reserved else PureWindowsPath(part).is_reserved())
        for part in parts.parts
    ):
        raise DriverConfigurationError(f'Path must be relative to the run directory: {name}')
    current = directory
    for part in parts.parts:
        current = current / part
        try:
            info = current.lstat()
        except (FileNotFoundError, NotADirectoryError):
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ObservationError(f'Cannot observe a link or reparse point: {name}')
    if not current.resolve().is_relative_to(directory):
        raise DriverConfigurationError(f'Path escapes the run directory: {name}')
    return current


def loopback_origin(base_url: str | None) -> str:
    value = text_field(base_url, 'base_url')
    try:
        parsed = urlsplit(value)
        host = ipaddress.ip_address(parsed.hostname or '')
        port = parsed.port
    except ValueError as exc:
        raise DriverConfigurationError('base_url must name a loopback IP and port') from exc
    if (parsed.scheme != 'http' or not host.is_loopback or not port
            or parsed.username is not None or parsed.path not in ('', '/')
            or parsed.query or parsed.fragment
            or any(c in value for c in '\r\n\t\\%')):
        raise DriverConfigurationError('base_url must be an HTTP loopback origin with an explicit port')
    return f'http://{host if host.version == 4 else f"[{host}]"}:{port}'


def step_url(origin: str, value: object) -> str:
    path = text_field(value, 'path')
    if (not path.startswith('/') or path.startswith('//') or '\\' in path
            or any(ord(char) <= 32 or ord(char) == 127 for char in path)
            or '#' in path):
        raise DriverConfigurationError('path must be origin-relative, without an authority or fragment')
    return origin + path


def _step_deadline(step: dict, deadline: float) -> float:
    seconds = step.get('timeout_seconds')
    if seconds is None:
        return deadline
    if type(seconds) not in (int, float) or not 0 < seconds <= 3600:
        raise DriverConfigurationError('timeout_seconds must be in (0, 3600]')
    return min(deadline, time.monotonic() + seconds)


def _header_name(value: object) -> str:
    if not isinstance(value, str) or not HTTP_TOKEN.fullmatch(value):
        raise DriverConfigurationError('Invalid HTTP header name')
    return value.lower()


async def _http(step: dict, client: httpx.AsyncClient, origin: str, deadline: float, ready: bool):
    url = step_url(origin, step.get('path', '/'))
    method = text_field(step.get('method', 'GET'), 'method').upper()
    if not HTTP_TOKEN.fullmatch(method) or method == 'CONNECT':
        raise DriverConfigurationError('Invalid HTTP method')
    headers = step.get('headers', {})
    selected = step.get('observe_headers', [])
    if not isinstance(headers, dict) or not isinstance(selected, list):
        raise DriverConfigurationError('HTTP headers must be a mapping and observe_headers a list')
    selected = list(dict.fromkeys(_header_name(key) for key in selected))
    headers = {_header_name(key): text_field(value, 'header value', empty=True)
               for key, value in headers.items()}
    if any(key in {'host', 'connection', 'transfer-encoding', 'content-length'}
           or key.startswith('proxy-') for key in headers):
        raise DriverConfigurationError('Cannot override HTTP routing or framing headers')
    body_fields = {'body', 'body_b64', 'json'}.intersection(step)
    if len(body_fields) > 1:
        raise DriverConfigurationError('Specify only one HTTP body field')
    body = b''
    if 'body' in body_fields:
        body = text_field(step['body'], 'body', empty=True).encode('utf-8')
    elif 'body_b64' in body_fields:
        body = decode_bytes(step['body_b64'])
    elif 'json' in body_fields:
        body = json.dumps(step['json'], ensure_ascii=False, allow_nan=False).encode('utf-8')
        headers.setdefault('content-type', 'application/json')
    if len(body) > MAX_BYTES:
        raise DriverConfigurationError('HTTP request body exceeds the observation byte limit')
    status = step.get('status')
    if status is not None and (type(status) is not int or not 100 <= status <= 599):
        raise DriverConfigurationError('HTTP readiness status must be an integer from 100 to 599')
    if ready and (method not in {'GET', 'HEAD'} or body_fields):
        raise DriverConfigurationError('Readiness probes must be GET or HEAD without a request body')

    async def request(timeout):
        async with client.stream(method, url, params=step.get('query'), headers=headers,
                                 content=body, timeout=timeout) as response:
            if ready:
                return response.status_code
            output = {'status': response.status_code, 'headers': {
                name: response.headers.get_list(name) for name in selected
            }}
            raw = bytearray()
            try:
                async for data in response.aiter_raw():
                    available = MAX_BYTES - len(raw)
                    raw.extend(data[:available])
                    if len(data) > available:
                        raise ObservationError('HTTP response exceeds the byte limit', evidence={
                            **output, 'body_b64': encode_bytes(raw), 'truncated': True,
                        })
            except (httpx.RequestError, asyncio.CancelledError):
                output['body_b64'] = encode_bytes(raw)
                partial.update(output)
                raise
            output['body_b64'] = encode_bytes(raw)
            return output

    partial = {}
    if not ready:
        try:
            return await asyncio.wait_for(request(remaining(deadline)), remaining(deadline))
        except (httpx.TimeoutException, TimeoutError) as exc:
            raise ObservationTimeout('HTTP request timed out', evidence=partial) from exc
        except httpx.RequestError as exc:
            raise ObservationError(f'HTTP request failed: {type(exc).__name__}', evidence=partial) from exc

    last = {}
    try:
        while True:
            probe = min(0.5, remaining(deadline))
            try:
                observed = await asyncio.wait_for(request(probe), probe)
                last['last_status'] = observed
                if observed == status or (status is None and 200 <= observed < 300):
                    return {'ready': True}
            except (httpx.RequestError, TimeoutError) as exc:
                last['last_error'] = type(exc).__name__
            await asyncio.sleep(min(0.075, remaining(deadline)))
    except ObservationTimeout as exc:
        raise ObservationTimeout('HTTP service did not become ready', evidence=last) from exc


def read_file(path: Path, deadline: float, byte_limit: int = MAX_BYTES) -> dict:
    remaining(deadline)
    if not stat.S_ISREG(path.stat().st_mode):
        raise ObservationError('Observation requires a regular file')
    with path.open('rb') as source:
        data = source.read(byte_limit + 1)
    if len(data) > byte_limit:
        raise ObservationError('File observation exceeds the byte limit')
    return {'kind': 'file', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'content_b64': encode_bytes(data)}


def _files(step: dict, directory: Path, deadline: float) -> dict:
    roots = step.get('paths')
    if not isinstance(roots, list) or not 1 <= len(roots) <= MAX_ENTRIES:
        raise DriverConfigurationError('files.paths must list 1–512 relative paths')
    observed = {}
    budget = MAX_BYTES

    def capture(name):
        nonlocal budget
        remaining(deadline)
        path = within_directory(directory, name)
        relative = path.relative_to(directory).as_posix()
        if relative in observed:
            return
        if len(observed) >= MAX_ENTRIES:
            raise ObservationError('File observation exceeds the entry limit', evidence=observed)
        if not path.exists():
            observed[relative] = {'kind': 'missing'}
        elif path.is_dir():
            observed[relative] = {'kind': 'directory'}
            children = []
            for child in path.iterdir():
                if len(children) >= MAX_ENTRIES:
                    raise ObservationError('Directory exceeds the entry limit', evidence=observed)
                children.append(child.relative_to(directory).as_posix())
            for child in sorted(children):
                capture(child)
        else:
            observed[relative] = read_file(path, deadline, budget)
            budget -= observed[relative]['size']

    try:
        for name in roots:
            capture(name)
    except ObservationError as exc:
        exc.evidence = observed
        raise
    return observed


def _sqlite(step: dict, directory: Path, deadline: float) -> dict:
    path = within_directory(directory, step.get('path'))
    table = text_field(step.get('table'), 'table')
    columns = step.get('columns')
    order_by = step.get('order_by', [])
    if not isinstance(columns, list) or not 1 <= len(columns) <= 64 or not isinstance(order_by, list):
        raise DriverConfigurationError('SQLite requires explicit columns and an optional order_by list')
    columns = [text_field(item, 'column') for item in columns]
    order_by = [text_field(item, 'order_by column') for item in order_by]
    if len(set(columns)) != len(columns) or not set(order_by) <= set(columns):
        raise DriverConfigurationError('SQLite columns must be unique and order_by must select from them')
    if not path.exists():
        return {'exists': False}
    for suffix in ('', '-wal', '-shm', '-journal'):
        path_or_sidecar = within_directory(directory, path.relative_to(directory).as_posix() + suffix)
        if path_or_sidecar.exists() and not stat.S_ISREG(path_or_sidecar.stat().st_mode):
            raise ObservationError('SQLite data files must be regular files')

    def quote(identifier):
        return '"' + identifier.replace('"', '""') + '"'

    def convert(value):
        if isinstance(value, bytes):
            return {'content_b64': encode_bytes(value)}
        if isinstance(value, float) and not math.isfinite(value):
            return {'sqlite_real': str(value)}
        return value

    rows = []
    evidence = {'exists': True, 'table': table, 'columns': columns, 'rows': rows}
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True,
                                     timeout=min(0.2, remaining(deadline)))) as connection:
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_BYTES)
            connection.execute('PRAGMA trusted_schema=OFF')
            connection.execute('PRAGMA query_only=ON')
            connection.set_progress_handler(lambda: time.monotonic() >= deadline, 500)
            table_kinds = {row[1]: row[2] for row in connection.execute('PRAGMA main.table_list')}
            if table_kinds.get(table) != 'table' or table.startswith('sqlite_'):
                raise ObservationError('SQLite observation requires an existing ordinary table')
            actual = {row[1] for row in connection.execute(f'PRAGMA main.table_info({quote(table)})')}
            if not set(columns) <= actual:
                raise ObservationError('SQLite observation specifies an unknown column')
            select = ', '.join(quote(item) for item in columns)
            ordering = list(dict.fromkeys(order_by + columns))
            order = ', '.join(f'{quote(item)} COLLATE BINARY, typeof({quote(item)})'
                              for item in ordering)
            query = f'SELECT {select} FROM main.{quote(table)} ORDER BY {order} LIMIT {MAX_ENTRIES + 1}'
            budget = MAX_BYTES
            for row in connection.execute(query):
                remaining(deadline)
                values = [convert(value) for value in row]
                budget -= len(json.dumps(values, ensure_ascii=True))
                if len(rows) == MAX_ENTRIES or budget < 0:
                    raise ObservationError('SQLite observation exceeds the row or byte limit',
                                           evidence=evidence)
                rows.append(values)
    except sqlite3.Error as exc:
        if time.monotonic() >= deadline:
            raise ObservationTimeout('SQLite observation timed out', evidence=evidence) from exc
        raise ObservationError(f'SQLite observation failed: {exc}', evidence=evidence) from exc
    return evidence


async def observe(kind: str, steps: list[dict], directory: Path, *, base_url: str | None = None,
                  deadline: float, target: Path | None = None, pid: int | None = None,
                  observations: dict | None = None, target_root: Path | None = None,
                  memory_limit: int | None = None, env: dict | None = None) -> dict:
    """Execute an ordered observation sequence. The caller owns the target lifecycle."""
    completed = observations if observations is not None else {}
    directory = directory.resolve(strict=True)
    if not isinstance(steps, list):
        raise DriverConfigurationError('steps must be a list')
    origin = loopback_origin(base_url) if kind in {'http', 'browser'} else None
    async with AsyncExitStack() as resources:
        client, browser, desktop = None, None, None
        for step in steps:
            step_id = None
            try:
                if not isinstance(step, dict):
                    raise DriverConfigurationError('Each observation step must be an object')
                step_id = text_field(step.get('id'), 'step id')
                if step_id in completed:
                    raise DriverConfigurationError('Duplicate step id')
                action = text_field(step.get('action', step.get('op')), 'action')
                until = _step_deadline(step, deadline)
                remaining(until)
                if action in {'files', 'sqlite'}:
                    collector = _files if action == 'files' else _sqlite
                    result = await asyncio.to_thread(collector, step, directory, until)
                elif kind in {'http', 'browser'} and (action == 'http' or
                        (action == 'ready' and not {'locator', 'selector'}.intersection(step))):
                    if client is None:
                        client = await resources.enter_async_context(httpx.AsyncClient(
                            trust_env=False, follow_redirects=False,
                            headers={'Accept-Encoding': 'identity'},
                        ))
                    result = await _http(step, client, origin, until, action == 'ready')
                elif kind == 'browser':
                    from .porting_browser import open_browser

                    if browser is None:
                        browser = await resources.enter_async_context(open_browser(origin, directory, until))
                    result = await browser.run(action, step, until)
                elif kind == 'desktop':
                    from .porting_desktop import Desktop

                    if desktop is None:
                        desktop = Desktop(pid)
                    result = await desktop.run(action, step, until)
                elif kind == 'library' and action == 'call':
                    from .porting_library import call_library

                    result = await call_library(step, target, target_root or directory, directory,
                                                until, memory_limit=memory_limit, env=env)
                else:
                    raise DriverConfigurationError(f'Unsupported {kind} observation action: {action}')
                completed[step_id] = result
            except DriverError as exc:
                exc.step_id = step_id
                raise
            except (TypeError, ValueError, UnicodeError) as exc:
                failure = DriverConfigurationError(f'Invalid observation input: {exc}')
                failure.step_id = step_id
                raise failure from exc
            except OSError as exc:
                failure = ObservationError(f'Observation failed: {exc}')
                failure.step_id = step_id
                raise failure from exc
    return completed
