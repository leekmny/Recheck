"""Private, read-once copies of the file paths named by a porting run."""
from __future__ import annotations

import hashlib
import os
from itertools import chain
from pathlib import Path

from .porting_extractors import MAX_JAR_BYTES, MAX_SOURCE_BYTES
from .porting_manifest import MAX_BUNDLE, execution_files, pack_target, relative, safe_source
from .porting_static import structure_input_names


def _structure_limits(manifest):
    limits = {}
    if manifest.get('structure', {}).get('enabled', True) is not True:
        return limits
    for target in manifest['targets'].values():
        try:
            names = structure_input_names(target)
        except ValueError:
            # The structure stage reports an invalid analysis file selection.
            continue
        runtime = target['runtime']
        for name in names:
            try:
                relative(name)
            except ValueError:
                continue
            if runtime in {'native', 'dotnet'}:
                limits[name] = None
            elif limits.get(name, 0) is not None:
                maximum = (MAX_JAR_BYTES if runtime == 'java' and Path(name).suffix.lower() == '.jar'
                           else MAX_SOURCE_BYTES)
                limits[name] = max(limits.get(name, 0), maximum)
    return limits


def _identity(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _capture(source, destination, limit, cancel):
    hasher = hashlib.sha256()
    size = 0
    initial_path = source.lstat()
    with source.open('rb') as reader:
        initial_handle = os.fstat(reader.fileno())
        if not os.path.samestat(initial_path, initial_handle):
            raise ValueError(f'사본 준비 중 입력 파일이 교체되었습니다 ({source.name})')
        if limit is not None and initial_handle.st_size > limit:
            raise ValueError(f'입력 파일 크기 상한 {limit}바이트를 초과했습니다 ({source.name})')
        with destination.open('xb') as writer:
            while block := reader.read(1024 * 1024):
                if cancel.is_set():
                    raise ValueError('입력 사본 준비 중 비교가 취소되었습니다.')
                size += len(block)
                if limit is not None and size > limit:
                    raise ValueError(f'입력 파일 크기 상한 {limit}바이트를 초과했습니다 ({source.name})')
                writer.write(block)
                hasher.update(block)
        if (_identity(initial_handle) != _identity(os.fstat(reader.fileno()))
                or _identity(initial_path) != _identity(source.lstat()) or size != initial_handle.st_size):
            raise ValueError(f'사본 준비 중 입력 파일이 변경되었습니다 ({source.name})')
    return hasher.hexdigest()


class InputSnapshot:
    def __init__(self, root, source_root, manifest, cancel):
        self.root = root
        self.paths = {}
        self.hashes = {}
        self.errors = {}
        self.bundle_errors = {}
        limits = _structure_limits(manifest)
        bundles = dict.fromkeys(tuple(execution_files(target, scenario.get('fixtures', [])))
                                for target in manifest['targets'].values()
                                for scenario in manifest['scenarios'])
        sources = {}
        for name in dict.fromkeys(chain(limits, chain.from_iterable(bundles))):
            if cancel.is_set():
                return
            try:
                source = safe_source(source_root, relative(name))
                canonical = os.path.realpath(source, strict=True)
                if not Path(canonical).is_relative_to(source_root):
                    raise ValueError(f'명세 폴더 밖의 입력은 사용할 수 없습니다 ({name})')
                info = source.lstat()
                sources[name] = source, canonical, info
            except (OSError, ValueError) as exc:
                self.errors[name] = str(exc)

        for names in bundles:
            if not 1 <= len(names) <= 512:
                self.bundle_errors[names] = '전달 파일 수는 1–512개입니다.'
            elif missing := next((name for name in names if name not in sources), None):
                self.bundle_errors[names] = self.errors[missing]
            elif sum(sources[name][2].st_size for name in names) > MAX_BUNDLE:
                self.bundle_errors[names] = '전달 파일 전체 크기 상한은 16 MiB입니다.'
            else:
                for name in names:
                    if limits.get(name, 0) is not None:
                        limits[name] = max(limits.get(name, 0), MAX_BUNDLE)

        groups = {}
        by_identity = {}
        for name, limit in limits.items():
            if name not in sources:
                continue
            source, canonical, info = sources[name]
            # The resolved spelling identifies Windows case/short-name aliases.
            # Distinct hard links also share one measured byte stream.
            if info.st_ino:
                canonical = by_identity.setdefault((info.st_dev, info.st_ino), canonical)
            groups.setdefault(canonical, []).append((name, source, limit))

        for index, inputs in enumerate(groups.values()):
            if cancel.is_set():
                break
            _, source, _ = inputs[0]
            sizes = [limit for _, _, limit in inputs]
            limit = None if None in sizes else max(sizes)
            try:
                path = root / str(index) / source.name
                path.parent.mkdir()
                sha256 = _capture(source, path, limit, cancel)
            except (OSError, ValueError) as exc:
                self.errors.update((name, str(exc)) for name, _, _ in inputs)
            else:
                self.paths.update((name, path) for name, _, _ in inputs)
                self.hashes.update((name, sha256) for name, _, _ in inputs)

    def pack(self, target, fixtures):
        names = tuple(execution_files(target, fixtures))
        if names in self.bundle_errors:
            raise ValueError(self.bundle_errors[names])
        return pack_target(self.root, target, fixtures, resolve_input=self.resolve)

    def resolve(self, name):
        if name in self.paths:
            return self.paths[name]
        raise ValueError(self.errors.get(name, f'실행 시작 시 확보한 입력이 없습니다 ({name})'))
