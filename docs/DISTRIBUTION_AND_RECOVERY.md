# 배포 경로와 복구

새 공개 Windows ZIP은 Recheck.exe와 실행용 Python·Qt, 고지·대응 소스 자료를 포함합니다.
기존 실행 파일·원래 태그·검증 기록은 별도 비공개 보관 저장소에 남습니다.
사용자는 [처음 실행](FIRST_RUN.md), 개발자는 아래의 새 배포본 검증·교체 절차를 따릅니다.
[공개 범위](PUBLICATION_SCOPE.md)와 [공개 이력 구성](PUBLIC_HISTORY.md)을 함께 따르세요.

이 소스 버전은 **0.1.2**입니다. 실행 파일은 [정확한 버전 릴리스](https://github.com/leekmny/Recheck/releases/tag/v0.1.2)의 `Recheck-0.1.2-Windows-x64.zip`을 사용합니다. 새 설치는 Gemini 3.5 Flash-Lite 무료 연결을 안내하며, 자신의 키 저장과 실제 응답 검증 전에는 연결되지 않습니다. 기존 AI 설정은 보존하고 GPT는 추천 선택으로 제공합니다. [Gemini 설정](GEMINI_FREE_SETUP.md)을 참고하세요.

## 실행 위치

개발 저장소의 정식 배포 출력은 `dist/Recheck`이며 이 PC의 외부 실행 폴더는 `리체크 배포판 프로토타입`입니다. 두 위치는 검증한 하나의 staging에서 생성하고 EXE·리소스·환경 파일 전체의 크기와 SHA-256이 같아야 합니다. ZIP은 앱 폴더 밖에 둡니다. EXE의 Windows 속성 → 자세히에서 버전을, `_internal/RECHECK_SOURCE_IDENTITY.json`에서 소스 식별자를 확인합니다. 소스 push만으로 기존 EXE는 바뀌지 않습니다.

## 검증 후 배포

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build.ps1 -Output verification/builds/my-release
```

공개 앱은 먼저 `scripts/verify_source.py --output <새 검사 폴더>`로 전체 pytest와 Node 검사를 실행하고 검사 전후 빌드 입력이 같을 때만 identity를 갱신합니다. 이어 `scripts/package_verified.py --output <새 패키지 폴더> --app-only`로 새 `package/staged-dist/Recheck`에 빌드합니다. 현재 실행 폴더는 건드리지 않습니다. 공개 앱 ZIP은 기존 Docker TAR를 포함하지 않으며 새 PC에서 명시적 분석 환경 준비로 구성합니다. 별도 오프라인 배포에서는 이미지 3개의 manifest·크기·SHA-256도 대조하고 재배포 자료를 따로 검토합니다.

실제 Windows EXE 시작과 frozen CLI 실행 비교 후, native 검사 결과와 전체 inventory를 담은 증거 파일로 반영합니다. 성공 값을 임의로 기록해 검사를 우회하면 안 됩니다.

`scripts/verify_native_release.py --help`의 `--original`, `--changed`, `--tests`, `--expected`로 직접 만든 알려진 차이 예제를 지정합니다. `--expected`는 실행 전에 고정한 입력 순서별 boolean JSON 배열입니다. 필수 `--oracle`은 같은 test_id 순서별 original/reimplementation의 stdout·stderr 문자열과 exit_code를 고정한 JSON 배열입니다. 실제 관측의 원시 UTF-8 바이트와 요청/실측 이미지 ID도 manifest와 대조합니다. 검사는 별도 앱 데이터에서 실제 GUI 시작과 frozen 엔진 동작을 확인하고 `native-proof.json`을 만듭니다. 이 검사는 Windows VM 첫 설치나 임의 Windows 대상 실행을 검증한 것은 아닙니다.

```powershell
.venv/Scripts/python scripts/release_distribution.py --stage verification/builds/my-release/package/staged-dist/Recheck --proof verification/builds/my-release/native-proof.json --target dist/Recheck --target ([IO.Path]::GetFullPath("../../리체크 배포판 프로토타입")) --record verification/builds/my-release/publication
```

`..`가 포함된 대상은 절대 경로로 정규화해서 전달합니다. 링크·junction·상위 경로 혼동을 막기 위해 그대로 남은 `..` 경로는 거부합니다. 검사 후 바뀐 파일과 실패한 증거도 거부합니다. 모든 사본과 ZIP을 먼저 검증한 뒤, 이전 폴더 전체를 부모의 `.recheck-previous-<작업ID>`에 보존하고 교체합니다. 사용자 추가 예제·문서·ZIP도 백업에 남으며 새 폴더에 과거 파일을 섞지 않습니다. publication.json에 EXE/ZIP 해시·전체 파일 목록·백업 위치를 기록합니다.

위 외부 대상은 이 PC의 `캡스톤/깃허브/Recheck` 저장소 위치를 기준으로 합니다. 다른 위치에 clone했다면 실제 실행 폴더의 절대 경로를 지정하세요.

## 교체 실패와 강제 종료

파일 잠금·복사 실패·해시 불일치 시 이전 폴더를 복원하고 실패 사본도 보존합니다. 다른 작업의 잠금을 덮어쓰지 않습니다. 강제 종료 후 정확한 journal 폴더로 복구합니다.

```powershell
.venv/Scripts/python scripts/release_distribution.py --record verification/builds/my-release/publication --recover
```

미완료 작업은 구버전으로 돌아가고 완료된 작업은 그대로 유지하며 해당 작업의 잠금만 해제합니다. 백업이 수동 변경·손상됐다면 중단하고 진단 자료를 보존합니다. 현재는 기록과 대상이 같은 볼륨에 있고 대상별 부모가 다른 경우를 지원합니다. 여러 폴더 교체 전체가 하나의 원자 연산은 아니며 중단 창은 journal로 복구합니다.

앱 데이터는 배포 밖 `%LOCALAPPDATA%/Recheck`, 시험은 `RECHECK_DATA_DIR`로 분리합니다. 교체는 설정·인증·이력·모델 볼륨을 수정하지 않습니다. 향후 schema 변경의 역호환까지 보장하지 않으므로 별도 백업과 마이그레이션 시험이 필요합니다. 보안 저장소를 VM이나 다른 PC에 복사해 로그인을 이전하지 않습니다. 분석 중 종료된 작업은 다음 시작에서 복구 대상으로 표시합니다. 손상·유실된 결과는 지난 분석 목록에서 성공으로 표시하지 않으며 원본 파일·DB를 보존합니다.

## 파일 지원 매트릭스

확장자보다 실제 ELF/PE 헤더를 검사하고 손상된 선택 헤더·섹션 범위를 거부합니다. 선택 화면에 실행 불가 이유를 표시합니다.

| 형식 | 정적 분석 | 실행 비교 | 제한 |
| --- | --- | --- | --- |
| ELF x86-64 콘솔/PIE | REA/Ghidra | 고정 Linux 이미지 | 자체 샘플 실제 비교; 등록 입력 범위 |
| PE x64 네이티브 콘솔 | REA/Ghidra | 미지원 | 자체 원본/변형 정적 비교 확인; Windows worker 미확보 |
| PE x86/x64/ARMNT/ARM64 네이티브 | REA/Ghidra 기본 정적 비교 | 미지원 | 아키텍처·헤더 유효성 검사; 실제 추출 범위·partial 상태 보존 |
| Windows GUI EXE / 네이티브 DLL | REA/Ghidra 기본 정적 비교 | 미지원 | 화면 조작·DLL 호출 관찰 계약은 없음 |
| .NET PE | REA 관리 메서드·시그니처·CIL 비교 | 미지원 | CLR 로드·실행·C# 소스 복원 없음; 메서드 대응 범위와 바이트 차이를 구분 |
| ELF 공유 라이브러리/x86/ARM | REA/Ghidra 기본 정적 비교 | 미지원 | 정적 분석 허용과 콘솔 실행 허용을 분리; 실측 지원 범위는 결과에서 확인 |
| 단일 아키텍처 Mach-O | Linux REA/Ghidra 정적 비교 | 미지원 | x86/x64/ARM/ARM64 인식; universal/fat은 단일 아키텍처 파일 필요 |
| ZIP·ZIP 기반 APK/JAR/MSIX/IPA 등 | 별도 구성 파일 비교 모드 | 미지원 | 경로·종류·크기·내용 SHA-256만 비교. 앱/서명/코드 의미는 검증하지 않음. [형식·상한](PACKAGE_COMPARISON.md) 참고 |
| 소스·스크립트·알 수 없는 형식 | 미지원 | 미지원 | 확장자 변경으로 지원되지 않음 |

동봉 Linux 컨테이너에서 PE를 실행하거나 첫 실행에서 Windows 이미지를 임의 빌드하지 않습니다. 오류·시간 초과·취소·출력 한도 초과·미지원은 일치나 0점으로 바꾸지 않고 측정 분모에서 제외합니다. 정적 누락·관계 범위·상한은 부분 상태로 남깁니다. 유사도는 휴리스틱이며 유한 입력 일치는 모든 입력의 동등성 증명이 아닙니다.

자동 비교는 먼저 REA 정적 분석을 진행합니다. 실행 지원이 없는 파일은 실행 단계를 차단하고 정적 근거·보고서를 남깁니다. `--mode static`으로 정적 비교만 선택할 수도 있습니다. .NET과 네이티브 코드는 서로 다른 근거이므로 같은 함수 유사도로 섞어 비교하지 않습니다. REA에 다른 APK·Electron·펌웨어 등의 도구가 존재하더라도 이 앱의 비교 스키마에 연결되지 않은 형식까지 지원한다고 표시하지 않습니다.

ZIP 구성 비교는 위 프로그램 분석과 별도 모드입니다. 파일을 디스크에 추출하거나 실행하지 않고 AI도 호출하지 않습니다. 두 파일의 전체 수집이 끝나야 추가·삭제·변경·유지를 계산합니다. 손상·미지원·취소·한도 초과는 같다는 결과로 바꾸지 않습니다.

## 검증 수준

단위 검사, 실제 Docker 실행, frozen EXE 실행, 새 VM 설치는 별개입니다. 조사한 VMware 등록은 Kali 한 개이고 제한된 후보 경로에서 Windows VM을 찾지 못했습니다. Kali는 변경하지 않았습니다. 깨끗한 Windows VM 설치·로그인·오프라인 복원·작은 화면 GUI 및 PE 실행 비교는 미검증입니다. 호스트 Docker 연결로 대신하지 않습니다.

Docker 안의 QEMU Windows VM은 가능한 대안이며 이 PC에서 KVM API·빈 VM 생성은 실측했습니다. Windows 설치 미디어·게스트 부팅·제품 worker·게스트 내부 Docker는 별도 준비와 검증이 필요합니다. [조사 결과와 준비 구성](WINDOWS_DOCKER.md)을 확인하세요.

`samples/accuracy`에는 소스와 실행 전에 고정한 정답표가 있습니다. ELF·PE 빌드, 실제 Linux 실행, AI 설명 채점은 구분합니다. AI 답변을 보고 정답을 바꾸거나 AI의 단독 자기 채점으로 정확도를 주장하지 않습니다. 실행 수·분모·오탐·누락·미측정·요청 예산·환경은 보고서에 함께 기록합니다.
