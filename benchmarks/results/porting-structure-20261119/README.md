# 실제 산출물 구조 비교 · 2026-11-19

[측정 결과 JSON](summary.json)에 산출물 SHA-256, 도구 버전, 분석 범위, 선택 함수의 특징과 CIL 해시를 기록했습니다. 아래 결과는 Windows x64의 RECHECK 구조 분석 단계에서 관측했습니다.

| 대상 | 관측 결과 | 비교 범위 |
| --- | --- | --- |
| Linux x64 ELF C → Rust 라이브러리 | 정상 포팅 68.3333%, 의도적 결함 67.5333% | Ghidra 12.1.4, `porting`, `parcel_quote` 1개. C 13/17개, Rust 538/588개 함수 관측으로 부분 결과 |
| Windows x64·ARM64 PE, macOS x64·ARM64 Mach-O, Linux ARM64 ELF | REA 6.9.0과 Ghidra 12.1.4의 정적 추출 경로 실측. 각 100% | 교차 빌드된 라이브러리의 `parcel_fee` 1개씩 비교. REA 인벤토리 관계 관측 범위 미확인으로 부분 결과 |
| C# .NET 두 라이브러리 | 관리 메서드 식별 2/2 일치. `Total` CIL은 다르고 생성자 CIL 바이트·헤더는 일치 | REA 6.9.0, PE/CLI 2개 메서드. 식별 일치와 CIL 관측을 각각 기록 |

함수 선택은 실행 전에 `parcel_quote`로 지정했고 `porting` 프로필의 가중치를 적용했습니다. 각 점수의 분모는 비교 가능하게 관측된 함수 수 중 큰 값입니다. 테스트 범위 내 동작 일치율과 요구사항 확인은 별도의 시나리오 결과에 기록합니다. 함수 매칭의 후보 정확도와 검토 시간 효과는 미실측입니다.

교차 빌드 검사는 `parcel_fee`의 즉시값 17과 19를 사용하는 서로 다른 바이너리를 비교했습니다. 이 경우 CFG 수치와 명령 분포가 같아 점수가 같았습니다. 대상별 실제 실행 여부는 동적 검증 기록에서 확인합니다.

교차 빌드를 다시 실행했을 때 두 소스와 ELF·Mach-O의 SHA-256이 일치했습니다. PE 네 파일에서는 COFF 타임스탬프 바이트만 달랐습니다. JSON에는 각 점수를 산출한 최초 빌드의 SHA-256을 보존했습니다.

## 산출물 재현

저장소 루트에서 PowerShell로 실행합니다. .NET 예제는 설치된 Windows .NET Framework 컴파일러를 사용합니다.

```powershell
$portingProbe = Join-Path ([IO.Path]::GetTempPath()) ('recheck-structure-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $portingProbe | Out-Null
$env:RECHECK_DATA_DIR = Join-Path $portingProbe 'settings'
$portingLibrary = Join-Path (Get-Location) 'samples/porting/library'
$portingImage = 'rust@sha256:e90e846de4124376164ddfbaab4b0774c7bdeef5e738866295e5a90a34a307a2'
docker pull $portingImage
docker run --rm --network none --mount "type=bind,src=$portingLibrary,dst=/src,readonly" --mount "type=bind,src=$portingProbe,dst=/out" --entrypoint /bin/sh $portingImage -c 'set -eu; gcc -shared -fPIC -O2 -o /out/original.so /src/original.c; rustc --crate-type cdylib -C opt-level=2 --crate-name parcel_quote /src/port.rs -o /out/port.so; rustc --crate-type cdylib -C opt-level=2 --crate-name parcel_quote --cfg defect /src/port.rs -o /out/defect.so'
$portingFormatBuild = Join-Path (Get-Location) 'benchmarks/results/porting-structure-20261119/build-native-formats.sh'
docker run --rm --mount "type=bind,src=$portingFormatBuild,dst=/probe.sh,readonly" --mount "type=bind,src=$portingProbe,dst=/out" --entrypoint /bin/sh $portingImage /probe.sh

$portingCsc = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$portingSource = 'namespace Porting { public class Fee { public static int Total(int x) { if (x < 0) return 0; return x + {0}; } } }'
$portingSource.Replace('{0}', '2') | Set-Content -Encoding ASCII -LiteralPath (Join-Path $portingProbe 'Fee.cs')
$portingSource.Replace('{0}', '3') | Set-Content -Encoding ASCII -LiteralPath (Join-Path $portingProbe 'FeePort.cs')
foreach ($portingName in ('Fee', 'FeePort')) {
    $portingDll = Join-Path $portingProbe "$portingName.dll"
    & $portingCsc /nologo /target:library /optimize+ "/out:$portingDll" (Join-Path $portingProbe "$portingName.cs")
    if ($LASTEXITCODE -ne 0) { throw 'C# compilation failed' }
}
```

Ghidra 12.1.4 압축 파일의 SHA-256을 확인하고 해제한 디렉터리를 `GHIDRA_HOME`으로 지정합니다. `javap`가 포함된 JDK 21을 `JAVA_HOME`으로 지정하고, Docker Linux 환경에서 관리 분석 이미지를 준비합니다. [Ghidra 12.1.4 릴리스](https://github.com/NationalSecurityAgency/ghidra/releases/tag/Ghidra_12.1.4_build)

```powershell
.venv/Scripts/python -m recheck.cli prepare-tools --build
$env:RECHECK_STRUCTURE_PROBE = $portingProbe
@'
import asyncio
import json
import os
from pathlib import Path

from recheck.models import Settings
from recheck.porting_static import compare_structure


async def main():
    root = Path(os.environ['RECHECK_STRUCTURE_PROBE'])
    jobs = [
        ('original.so', 'port.so', 'native', 'linux', 'x64', 'direct', 'porting', ['parcel_quote']),
        ('original.so', 'defect.so', 'native', 'linux', 'x64', 'direct', 'porting', ['parcel_quote']),
        ('Fee.dll', 'FeePort.dll', 'dotnet', 'windows', 'x64', 'rea', 'default', []),
    ]
    for target_os, arch, suffix in (('windows', 'x64', 'dll'), ('windows', 'arm64', 'dll'),
                                    ('macos', 'x64', 'dylib'), ('macos', 'arm64', 'dylib'),
                                    ('linux', 'arm64', 'so')):
        jobs.append((f'original-{arch}.{suffix}', f'candidate-{arch}.{suffix}', 'native',
                     target_os, arch, 'rea', 'default', ['parcel_fee', '_parcel_fee']))
    for left, right, runtime, target_os, arch, backend, profile, selected in jobs:
        targets = {
            side: dict(entry=entry, runtime=runtime, os=target_os, arch=arch)
            for side, entry in (('reference', left), ('candidate', right))
        }
        if selected:
            for target in targets.values():
                target['static'] = {'include': selected}
        result = await compare_structure(
            {'targets': targets, 'structure': {'profile': profile}}, root,
            workspace=root / (right + '-evidence'), settings=Settings(static_backend=backend),
        )
        (root / (right + '.json')).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                              encoding='utf-8')
        print(right, result['status'], result['similarity'])


asyncio.run(main())
'@ | .venv/Scripts/python -
```

Python AST와 실제 Java 클래스/JAR, JavaScript·TypeScript AST는 아래 검사에서 확인합니다. Java 검사는 설치 JDK로 클래스를 컴파일하고 `javap`를 사용합니다. TypeScript 검사는 운영자가 설치한 파서만 로드합니다.

```powershell
$portingParser = Join-Path $portingProbe 'parser'
npm.cmd install --prefix $portingParser --ignore-scripts --no-audit --no-fund --save-exact typescript@5.9.3
$env:RECHECK_TYPESCRIPT_MODULE = Join-Path $portingParser 'node_modules/typescript'
.venv/Scripts/python -m pytest -q tests/test_porting_static.py
```
