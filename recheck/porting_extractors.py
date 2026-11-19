"""Read source and JVM class files with their own representation-specific tools."""

from __future__ import annotations

import ast
from collections import Counter
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import time
import zipfile

from .models import resources_root
from .prerequisites import _candidate_paths
from .process import run_process

MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_JAR_BYTES = 64 * 1024 * 1024
MAX_JAR_CLASSES = 64
MAX_FUNCTIONS = 256
JS_SUFFIXES = {'node': {'.js', '.mjs', '.cjs', '.jsx'},
               'typescript': {'.ts', '.mts', '.cts', '.tsx'}}


def read_file(path, maximum):
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError(f'Analysis input exceeds {maximum} bytes')
    return data, dict(size=len(data), sha256=hashlib.sha256(data).hexdigest())


class PythonFunctions(ast.NodeVisitor):
    _functions = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
    _branches = (ast.If, ast.IfExp, ast.For, ast.AsyncFor, ast.While, ast.Match,
                 ast.ExceptHandler, ast.BoolOp, ast.comprehension)

    def __init__(self, filename):
        self.filename = filename
        self.scope = []
        self.functions = []
        self.total = 0

    def _visit_scope(self, node, name):
        self.scope.append(name)
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node):
        self._visit_scope(node, node.name)

    def visit_FunctionDef(self, node):
        self._record(node, node.name)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        self._record(node, '<lambda>')

    def _record(self, node, name):
        qualified = '.'.join((*self.scope, name))
        self.total += 1
        if len(self.functions) < MAX_FUNCTIONS:
            pending = [node]
            counts = Counter()
            branches, calls = 0, 0
            while pending:
                member = pending.pop()
                counts[type(member).__name__] += 1
                branches += isinstance(member, self._branches)
                calls += isinstance(member, ast.Call)
                if member is node or not isinstance(member, (*self._functions, ast.ClassDef)):
                    pending.extend(ast.iter_child_nodes(member))
            self.functions.append(dict(
                id=f'{self.filename}:{qualified}:{node.lineno}:{node.col_offset + 1}',
                name=qualified, file=self.filename, line=node.lineno, column=node.col_offset + 1,
                features=dict(tokens=dict(counts), size=sum(counts.values()),
                              branches=branches, calls=calls),
            ))
        self._visit_scope(node, name)


def extract_python(data, filename):
    visitor = PythonFunctions(filename)
    visitor.visit(ast.parse(data, filename=filename, mode='exec'))
    return visitor.functions, visitor.total


def python_tool():
    return dict(name='Python ast', version=platform.python_version(), executable=sys.executable,
                implementation=platform.python_implementation())


def tool_failure(process):
    if process.get('cancelled'):
        return 'cancelled', 'Extraction cancelled'
    if process.get('timed_out'):
        return 'failed', 'Extraction tool timed out'
    if process.get('truncated'):
        return 'failed', 'Extraction tool output exceeded its limit'
    if process.get('error') or process.get('cleanup_errors') or process.get('returncode') != 0:
        return 'failed', (process.get('error') or '\n'.join(process.get('cleanup_errors', []))
                          or process.get('stderr', '').strip()[:2000]
                          or f"Extraction tool exited with status {process.get('returncode')}")
    return None, None


async def extract_ecmascript(data, filename, workspace, timeout, cancel):
    node = shutil.which('node')
    parser = os.environ.get('RECHECK_TYPESCRIPT_MODULE')
    if not node or (parser and not Path(parser).is_absolute()):
        return dict(status='blocked', reason='Node.js and a trusted absolute TypeScript package path are required')
    helper = resources_root() / 'porting' / 'extract_ecmascript.cjs'
    if not helper.is_file():
        return dict(status='blocked', reason='The Recheck TypeScript parser helper is missing')
    env = os.environ.copy()
    for key in ('NODE_OPTIONS', 'NODE_PATH'):
        env.pop(key, None)
    process = await run_process([node, helper, filename, str(MAX_FUNCTIONS), parser or ''],
                                stdin=data, cwd=workspace, env=env, timeout=timeout,
                                output_limit=4 * 1024 * 1024, cancel=cancel)
    if process.get('returncode') == 42 and not process.get('cleanup_errors'):
        return dict(status='blocked', reason=process['stderr'].strip(), command=process['argv'],
                    extractor=dict(name='TypeScript compiler AST', version=None, executable=node))
    state, reason = tool_failure(process)
    if state:
        return dict(status=state, reason=reason, command=process['argv'])
    value = json.loads(process['stdout'])
    extractor = dict(name='TypeScript compiler AST', version=value['version'], executable=node,
                     node_version=value['node_version'], module=value['module'])
    result = dict(extractor=extractor, command=process['argv'], functions=value['functions'],
                  total=value['total'], diagnostics=value['diagnostics'])
    if value['diagnostics']:
        result.update(status='failed', reason='TypeScript parser reported source syntax errors',
                      functions=[], total=None)
    return result


def _javap():
    name = 'javap.exe' if os.name == 'nt' else 'javap'
    candidates = [shutil.which('javap'), *(str(Path(candidate).with_name(name))
                                         for candidate in _candidate_paths('java'))]
    return next((candidate for candidate in candidates if candidate and Path(candidate).is_file()), None)


_DECLARATION = re.compile(
    r'^(?:(?:public|protected|private|final|abstract|strictfp|static|sealed|non-sealed)\s+)*'
    r'(?:class|interface|enum|record)\s+([^\s<{]+)'
)
_INSTRUCTION = re.compile(r'^\s+(\d+):\s+([a-z][a-z0-9_]*)(?:\s+(.*))?$')
_SWITCH_TARGET = re.compile(r'^\s+(?:-?\d+|default):\s+(\d+)\s*$')


def parse_javap(output, filename):
    declaration = next((match for line in output.splitlines()
                        if (match := _DECLARATION.match(line))), None)
    if declaration is None:
        raise ValueError('javap output did not identify a class or interface')
    class_name = declaration[1]
    functions, method, switch_offset = [], None, None
    for line in output.splitlines():
        if line.startswith('  ') and not line.startswith('   '):
            method = None
            switch_offset = None
            member = line.strip()
            if '(' in member or member == 'static {};':
                name = '<clinit>' if member == 'static {};' else member.split('(', 1)[0].split()[-1]
                name = '<init>' if name == class_name else name
                method = dict(name=f'{class_name}.{name}', class_name=class_name, file=filename,
                              declaration=member, features=None, instructions=[], branch_targets=[])
            continue
        if method is None:
            continue
        if line.startswith('    descriptor: '):
            signature = line.split(':', 1)[1].strip()
            method.update(id=f"{filename}:{method['name']}{signature}", signature=signature)
            functions.append(method)
        instruction = _INSTRUCTION.match(line)
        if instruction:
            offset, opcode, operands = instruction.groups()
            method['instructions'].append(opcode)
            switch_offset = int(offset) if opcode.endswith('switch') else None
            if opcode.startswith(('if', 'goto', 'jsr')) and operands:
                target = operands.split()[0]
                if target.isdecimal():
                    method['branch_targets'].append([int(offset), int(target)])
        switch_target = _SWITCH_TARGET.match(line)
        if switch_target and switch_offset is not None:
            method['branch_targets'].append([switch_offset, int(switch_target[1])])

    for method in functions:
        tokens = Counter(method.pop('instructions'))
        if tokens:
            method['features'] = dict(
                tokens=dict(tokens), size=sum(tokens.values()),
                branches=sum(count for opcode, count in tokens.items()
                             if opcode.startswith(('if', 'goto', 'jsr')) or opcode.endswith('switch')),
                calls=sum(count for opcode, count in tokens.items() if opcode.startswith('invoke')),
            )
        else:
            method['unobserved_reason'] = 'Method has no observed bytecode body'
    return functions, class_name


async def extract_java(data, filename, workspace, timeout, cancel):
    javap = _javap()
    if not javap:
        return dict(status='blocked', reason='JDK javap is unavailable')
    env = os.environ.copy()
    for key in ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'CLASSPATH'):
        env.pop(key, None)
    deadline = time.monotonic() + timeout
    version = await run_process([javap, '-version'], cwd=workspace, env=env,
                                timeout=timeout, output_limit=4096, cancel=cancel)
    state, reason = tool_failure(version)
    if state:
        return dict(status=state, reason=reason, command=version['argv'])
    result = dict(extractor=dict(name='OpenJDK javap', version=version['stdout'].strip(), executable=javap),
                  classes=[], functions=[], total=0)
    if filename.lower().endswith('.class'):
        entries = [(filename, data)]
        await _extract_classes(entries, result, javap, workspace, env, deadline, cancel)
    else:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = _jar_classes(archive, filename, result)
            await _extract_classes(entries, result, javap, workspace, env, deadline, cancel)
    if (result.get('status') == 'partial' and result['classes']
            and all(row['status'] == 'failed' for row in result['classes'])):
        result.update(status='failed', reason='javap did not extract any selected class')
    return result


def _jar_classes(archive, filename, result):
    entries = [entry for entry in archive.infolist() if entry.filename.endswith('.class')]
    selected = {}
    for entry in entries:
        if entry.filename.startswith('META-INF/'):
            continue
        if entry.filename in selected:
            result['classes'].append(dict(file=f'{filename}!/{entry.filename}', status='failed',
                                          reason='Duplicate class entry was skipped'))
        else:
            selected[entry.filename] = entry
    result.update(class_entries=len(entries), selected_class_entries=min(len(selected), MAX_JAR_CLASSES))
    if result['class_entries'] != result['selected_class_entries']:
        result['limitations'] = ['Up to 64 distinct base JAR classes are inspected. META-INF variants and duplicate entries are excluded.']
        result.update(status='partial', total=None)
    for entry in list(selected.values())[:MAX_JAR_CLASSES]:
        name = f'{filename}!/{entry.filename}'
        try:
            if entry.file_size > MAX_SOURCE_BYTES:
                raise ValueError('JAR class exceeds the per-class size limit')
            yield name, archive.read(entry)
        except (OSError, ValueError, RuntimeError, zipfile.BadZipFile) as exc:
            result['classes'].append(dict(file=name, status='failed', reason=str(exc)))
            result.update(status='partial', total=None)


async def _extract_classes(entries, result, javap, workspace, env, deadline, cancel):
    staged = workspace / 'input.class'
    for filename, data in entries:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or (cancel is not None and cancel.is_set()):
            cancelled = cancel is not None and cancel.is_set()
            result.update(status='cancelled' if cancelled else 'partial', total=None,
                          reason='Class extraction cancelled' if cancelled else 'Class extraction time limit reached')
            break
        info = dict(file=filename, sha256=hashlib.sha256(data).hexdigest())
        result['classes'].append(info)
        if len(data) < 8 or data[:4] != b'\xca\xfe\xba\xbe':
            info.update(status='failed', reason='Invalid JVM classfile header')
            result.update(status='partial', total=None)
            continue
        info.update(class_major_version=int.from_bytes(data[6:8], 'big'),
                    class_minor_version=int.from_bytes(data[4:6], 'big'))
        staged.write_bytes(data)
        command = [javap, '-J-Duser.language=en', '-J-Duser.country=US',
                   '-J-Dfile.encoding=UTF-8', '-p', '-s', '-c', staged]
        process = await run_process(command, cwd=workspace, env=env, timeout=remaining,
                                    output_limit=4 * 1024 * 1024, cancel=cancel)
        info['command'] = process['argv']
        state, reason = tool_failure(process)
        if not state:
            try:
                methods, class_name = parse_javap(process['stdout'], filename)
            except ValueError as exc:
                state, reason = 'failed', str(exc)
        if state:
            info.update(status=state, reason=reason)
            result.update(status='cancelled' if state == 'cancelled' else 'partial', total=None)
            if state == 'cancelled':
                break
            continue
        info.update(status='succeeded', class_name=class_name, methods=len(methods))
        room = MAX_FUNCTIONS - len(result['functions'])
        result['functions'].extend(methods[:room])
        if result['total'] is not None:
            result['total'] += len(methods)
