"""Prepare manifests for the locally authored fee fixture programs."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VERSION = 'recheck-porting-1'
CASES = [('zero', '0', '0'), ('below', '2', '200'), ('edge', '3', '300'),
         ('above', '4', '375'), ('higher', '12', '975'), ('maximum', '100', '7575'),
         ('negative', '-1', None), ('malformed', 'bad', None), ('empty', '', None),
         ('unicode', '한글 공백', None), ('missing', None, None)]


def manifest(system, arch, reference='c', candidate='rust', variant='normal'):
    def target(language, side):
        data = {'os': system, 'arch': arch}
        if language in ('python', 'node'):
            data.update(runtime=language, entry='fee.py' if language == 'python' else 'fee_node.mjs')
            if language == 'node' and variant == 'defect':
                data['env'] = {'FEE_DEFECT': '1'}
            return data
        if language == 'typescript':
            return dict(data, runtime=language, entry='fee.js', files=['fee.ts'],
                        build={'tool': 'tsc', 'args': ['--strict', '--target', 'ES2022', 'fee.ts']},
                        env={'FEE_DEFECT': '1'} if variant == 'defect' else {},
                        static={'path': 'fee.ts'})
        if language == 'java':
            return dict(data, runtime=language, entry='Fee.class', files=['Fee.java'],
                        java_main='Fee', build={'tool': 'javac', 'args': ['Fee.java']})
        if language == 'dotnet':
            return dict(data, runtime=language, entry='out/Fee.dll', files=['Fee.csproj', 'Fee.cs'],
                        build={'tool': 'dotnet', 'args': ['build', 'Fee.csproj', '-o', 'out',
                                                         '--source', '.', '--nologo']},
                        env={'FEE_DEFECT': '1'} if variant == 'defect' else {})
        extension = '.exe' if system == 'windows' else ''
        entry = f'{side} fee{extension}'
        path = 'fee.c' if language == 'c' else 'fee.rs'
        if language == 'c':
            args = ['-g', '-O0', path, '-o', entry]
        else:
            args = ['-C', 'debuginfo=1', '--crate-name', 'fee', path, '-o', entry]
            if system == 'windows' and arch == 'x64':
                args += ['--target', 'x86_64-pc-windows-gnu', '-C', 'link-self-contained=yes']
            if variant != 'normal':
                args += ['--cfg', variant]
        return dict(data, language=language, runtime='native', entry=entry, files=[path],
                    build={'tool': 'gcc' if language == 'c' else 'rustc', 'args': args})

    scenarios = []
    for identifier, argument, value in CASES:
        expected = dict(exit_code=0, stdout=value + '\n', stderr='') if value is not None else (
            dict(exit_code=2, stdout='', stderr='invalid\n'))
        scenarios.append(dict(id=identifier, kind='console', timeout=40,
                              input={'args': [] if argument is None else [argument]},
                              expected=dict(reference=expected, candidate=expected),
                              policy={'stdout': 'legacy', 'stderr': 'legacy'}))
    return dict(schema_version=VERSION, description=f'Fee tiers: {reference} to {candidate} ({variant})',
                targets={'reference': target(reference, 'reference'), 'candidate': target(candidate, 'candidate')},
                scenarios=scenarios, structure={'enabled': False}, concurrency=3)


def write(directory, name, data):
    path = directory / (name + '.json')
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return path
