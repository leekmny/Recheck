# Recheck

**한국어** | [English](https://github.com/leekmny/Recheck/blob/main/README.en.md)

**0.2.1 소스 · Windows x64 미리보기 배포**

## 프로젝트 목적

Recheck는 사용자가 준비한 프로그램과 배포물을 비교하고, 확인한 동작·구조·구성의 차이를 재현 가능한 근거로 남기는 도구입니다. 같은 입력이나 시나리오에서 무엇이 달라졌는지, 각 대상이 기대 결과를 충족했는지, 어디까지 확인했는지를 기록합니다. 차이가 난 입력은 보관해 수정본을 다시 검사할 수 있습니다.

언어 간 포팅 검증은 이 목적을 적용하는 한 사례입니다. 같은 프로그램의 버전별 회귀 검사, 새 구현이나 교체 후보의 동작 확인, 함수 구조 검토, 배포 패키지의 구성 비교에도 사용합니다. 캡스톤에서는 레거시를 대체할 프로그램을 검증하는 사례에 집중했습니다.

소스·테스트·직접 만든 샘플·설계 및 검증 범위를 공개합니다. 원본 코드의 권리는 [LICENSE](LICENSE), 외부 구성요소의 조건은 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 확인하세요. 공개 저장소의 열람과 오픈소스 재사용 허용은 별개입니다. [캡스톤 요구사항·근거·발표 안내](https://github.com/leekmny/Recheck/blob/v0.1.7/docs/CAPSTONE_FEEDBACK.md)에 원본·재구현·외부 도구·Recheck의 역할, 지표가 충돌하는 예시, 실험의 범위와 남은 연구 질문을 정리했습니다.

함수 구조 점수는 비교 후보를 찾는 휴리스틱이며 동작 일치율이나 저작권 침해 확률이 아닙니다. 자동 복제·복호화·권리 판정은 이 비교 기능의 목적이 아닙니다. 화면의 다시 만들기·개선하기와 CLI의 `reconstruct`·`modernize`는 현재 요청·원본 보관 단계이며, 소스 생성·빌드·차등 검증 파이프라인은 미구현 상태로 차단됩니다.

분석 AI 안내는 추천 ChatGPT → Gemini 무료(API) → 공통 AI 계정 연결 순서입니다. 로그인·키 발급은 외부 브라우저가 기본이며, 체크박스로 내장 표시도 선택할 수 있습니다. [Google 제한과 기본값 결정의 근거](https://github.com/leekmny/Recheck/blob/main/docs/decisions/0006-external-browser-default.md)를 참고하세요.

**[0.2.1 Windows x64 미리보기](https://github.com/leekmny/Recheck/releases/tag/v0.2.1)** · [전체 릴리스](https://github.com/leekmny/Recheck/releases) · [이전 0.1.6 배포 기록](https://github.com/leekmny/Recheck/releases/tag/v0.1.6)

0.1.7에서는 관측하지 못한 함수의 구조 점수를 제외하고, 실행 결과의 비교 가능 건수·제외 사유·원시 출력 차이를 명시합니다. 이전 기록의 출력 정책을 확인할 수 없으면 원시 관찰 수와 판정 가능 수를 분리합니다.

0.1.6에서는 ChatGPT 로그인과 Gemini·OpenAI·Grok·DeepSeek·Claude 키 발급 안내를 통합하고, 연결된 AI의 상태 확인·연결 해제와 비교별 AI 선택을 정리했습니다. **설정 → 분석 AI 연결**에서 연결을 준비하고, **프로그램 비교**에서 이번 분석에 쓸 AI 하나를 고릅니다. 여러 AI에 같은 분석을 자동 요청하지 않습니다. 이전 0.1.5 EXE에는 새 통합 화면이 없습니다. 새 ZIP의 최종 실행 검증·게시 결과는 해당 릴리스와 [작업 기록](https://github.com/leekmny/Recheck/blob/main/docs/decisions/2026-11-17-embedded-login-work-log.md)에서 확인하세요. [연결 사용법과 제한](docs/AI_CONNECTIONS.md), [실제 계정 검증 상태](docs/AI_AUTH_STATUS.md)도 구분해 안내합니다.

`0.1.x`는 초기 개발 버전입니다. [이전 이름 대응표](docs/PUBLIC_HISTORY.md#공개-버전-이름-정리)에 공개 버전의 변경을 기록합니다. 소스 검사와 각 Windows 배포본의 실행 검증 범위는 해당 릴리스에서 구분해 확인하세요.

ZIP을 풀고 `Recheck` 폴더 안의 **`Recheck.exe`**를 실행합니다. 실행용 Python과 Qt가 포함되어 있어 앱을 켜기 위해 Git이나 Python을 따로 설치할 필요가 없습니다. GitHub의 `Source code` ZIP은 개발용 소스입니다. [처음 실행 안내](docs/FIRST_RUN.md), [바이너리 배포 자료](docs/BINARY_DISTRIBUTION.md)를 참고하세요. 과거 프로토타입과 원래 개발 기록은 비공개 보관 저장소에 보존합니다.

이 공개 저장소는 검토한 소스 스냅샷에서 새 Git 이력으로 시작합니다. 과거 커밋·PR·Actions 기록과 태그는 공개 이력에 포함하지 않습니다. [공개 이력 구성](docs/PUBLIC_HISTORY.md)에 보존 범위와 이후 작업 규칙을 설명합니다.

PySide6 GUI와 Python CLI는 같은 비교 엔진을 사용합니다. Ghidra·REA의 정적 관찰, 실행 관찰, 선택한 AI의 설명은 각각 근거와 상태를 확인할 수 있습니다.

**정적분석·동적분석·둘 다 분석**을 선택할 수 있으며 기본 선택은 둘 다 분석입니다. AI 설명은 별도로 켜거나 끕니다. 선택한 범위에 필요한 도구가 없으면 분석 전에 설치·준비 안내를 보여 주며 다른 모드로 자동 변경하지 않습니다. 기본 바이너리 동적 분석은 Linux x64 ELF 콘솔을 지원합니다. Windows 및 GUI 실행은 대상 명세와 해당 환경의 시나리오 비교 경로를 사용합니다. .NET은 메서드 시그니처·CIL 바이트·예외 메타데이터를 비교하며 이를 C# 소스 복원이나 실행 검증으로 표시하지 않습니다. [분석 모드 안내](docs/ANALYSIS_MODES.md)와 [지원 매트릭스](docs/DISTRIBUTION_AND_RECOVERY.md#파일-지원-매트릭스)를 확인하세요.

## 포팅 시나리오 검증

**프로그램 비교 → 포팅 시나리오 비교**에서 런타임·API·GUI·라이브러리 대상의 명세와 격리 환경을 선택할 수 있습니다. 같은 시나리오로 언어가 다른 구현과 같은 프로그램의 두 버전을 비교할 수 있습니다. 관찰된 동작 일치율, 양쪽 기대 결과, 구조 특징을 따로 확인합니다. [준비·재현·실측 범위](docs/PORTING_VALIDATION_UPDATE.md)

0.2.1은 비교 시작 때 준비한 입력 사본을 시나리오와 구조 분석에 함께 사용합니다. 관찰 결과는 SQLite에 단계별로 기록되므로 중단 후 저장된 관찰을 다시 열 수 있습니다. 이전 결과도 계속 조회할 수 있습니다.

## 현재 기능과 범위

- 프로그램 파일 선택, 테스트 입력 작성·가져오기, 격리 실행과 결과 비교
- 분석 모드별 도구 점검·설치 안내, 정적·동적·AI 상태를 분리한 결과와 보고서
- 실제 수집 범위·미관측 함수·유효 원시 실행 관찰을 보여 주는 확인 범위 화면
- 불일치·미측정 입력 보관과 새 기록으로 재검사, 실패 항목만 HTML/JSON 내보내기
- ZIP·ZIP 기반 패키지의 구성 파일 추가·삭제·변경 비교와 JSON/HTML 내보내기
- 함수·실행 근거 조회, AI 설명·추가 분석, 보고서와 지난 분석 확인
- 추천 ChatGPT 로그인·Gemini 무료 API·다른 AI 제공자의 연결 안내
- 도구 설치·관리: PATH 밖의 설치 위치 확인, 필요한 도구의 WinGet 설치와 공식 다운로드 안내
- Ollama, OpenAI API, Claude API, Gemini, OpenAI 호환 서버의 연결 설정
- 배포 환경 자동 가져오기: Java·Ghidra·REA 정적 분석과 프로그램 실행을 별도 컨테이너로 분리
- 로컬 AI 원클릭 준비: Ollama CPU 실행 환경과 Qwen3 4B Instruct 다운로드·응답 확인·선택

Linux x86-64 ELF 콘솔 프로그램의 실행 비교를 지원합니다. Windows 프로그램의 실행 비교에는 별도 Windows worker가 필요합니다. 일반 Windows 컨테이너는 데스크톱 GUI 설치 시험을 대신할 수 없지만, Linux Docker 안에서 QEMU로 전체 Windows VM을 구동하는 경로는 있습니다. 이 PC에서 KVM API·빈 VM 생성은 확인했으며 Windows 부팅·PE 실행·깨끗한 VM 설치는 아직 미검증입니다. [Windows Docker 조사와 준비 절차](docs/WINDOWS_DOCKER.md)를 참고하세요. 폴더 종합 비교와 Rebuild는 포함하지 않습니다. 등록 입력의 일치는 모든 입력의 동등성 증명이 아닙니다. [지원·배포·복구 안내](docs/DISTRIBUTION_AND_RECOVERY.md), [컨테이너 환경](docs/CONTAINER_RUNTIME.md)에 범위와 제한을 설명합니다.

## 앱 사용하기

Windows 배포 ZIP을 풀고 **Recheck 폴더 전체를 유지한 채 `Recheck.exe`**를 실행하세요. `_internal` 폴더에는 실행용 Python·Qt와 앱 리소스가 있습니다. 내장 분석 환경의 Docker 이미지는 처음 준비할 때 인터넷에서 구성합니다. 프로그램 실행 비교에는 Linux 컨테이너를 실행할 수 있는 Docker Desktop·WSL 2가 필요합니다. Gemini·GPT 등 외부 AI 연결에도 인터넷이 필요합니다. **AI 설명을 끄면 API 키나 AI 연결 없이 분석할 수 있습니다.**

1. **프로그램 비교**에서 정적분석·동적분석·둘 다 분석을 고르고 기존 프로그램과 새 프로그램을 선택합니다. 필요한 도구 확인과 설치 안내를 같은 화면에서 이용할 수 있습니다.
2. **도구 설치·관리 → 도구 확인**에서 선택한 분석에 필요한 도구를 준비합니다. 내장 환경으로 정적분석하거나 동적분석하려면 Docker Desktop을 실행한 뒤 **분석 도구 내려받아 준비**를 누릅니다. 최초 준비에는 인터넷과 수 GB의 디스크 공간이 필요하며 Java·Ghidra·REA도 이 환경에 포함됩니다. 직접 설치한 Ghidra·Java로 지원되는 형식을 정적분석하거나 ZIP·패키지 구성을 비교할 때는 Docker가 필요하지 않습니다. .NET 등 분석 경로별 지원 범위는 [분석 모드 안내](docs/ANALYSIS_MODES.md)를 확인하세요. 설치·이미지 준비는 해당 버튼을 눌렀을 때만 시작합니다.
3. **AI 설명을 사용할 때만** AI를 연결합니다. 추천 ChatGPT 로그인 또는 **Gemini 무료로 시작하기**에서 자신의 계정·키로 연결하고 실제 응답을 확인하세요. Gemini 무료 시작의 기본 모델은 Gemini 3.5 Flash-Lite이며, 연결 전에는 활성화되지 않습니다. **프로그램 비교**에서 준비된 AI 연결과 모델 하나를 선택합니다. 기존 연결·키·예산은 유지하며 실행 중에는 이번 선택을 고정합니다. [AI 연결 안내](docs/AI_CONNECTIONS.md), [Gemini 무료 연결 안내](docs/GEMINI_FREE_SETUP.md)를 참고하세요.
4. 동적분석이 포함된 경우 비교할 입력을 등록하고 **비교 시작하기**를 누릅니다. 정적분석만 선택하면 실행 입력이 숨겨집니다. 도구 또는 파일 지원 조건이 맞지 않으면 안내를 확인하고 준비하거나 분석 모드를 직접 바꾸세요.
5. 실제 출력·종료 코드·불일치 입력을 먼저 확인한 뒤 AI 설명과 보고서를 살펴봅니다.

결과의 **확인 범위**에서 함수·의사코드·관계·입력별 관측과 누락을 확인하세요. **문제 입력 저장 → 재검사 보관함**에서 원본·수정본을 선택하면 저장한 입력과 비교 정책으로 새 동적 분석을 만듭니다. **실패 항목 보고서**는 불일치·실행 미측정·정적 수집 미완료·AI 실패를 구분하고 원인·다음 조치를 HTML/JSON으로 저장합니다. 원문 입력·출력은 별도 선택이며 기존 결과와 개인 설정은 유지합니다. [확인 범위·재검사·실패 보고서 안내](docs/REVIEW_AND_RECHECK.md)를 참고하세요.

로컬 AI를 처음 준비하려면 **내 PC의 AI로 시작**을 선택하세요. Ollama를 시작하고 Qwen3 4B Instruct 모델을 처음 약 2.5GB 내려받습니다. 이미 이 로컬 AI를 선택한 사용자는 앱 시작 시 기존 환경에 재연결할 수 있습니다. 모델은 관리 전용 볼륨에 보관되며 배포 ZIP에는 포함되지 않습니다. 기본 로컬 환경은 CPU 전용이어서 응답이 느릴 수 있습니다. 실제 도구 호출 검증이 실패한 모델은 기본 비교·설명만 사용합니다. GPT 사용 중에는 Ollama나 모델을 시작·다운로드하지 않습니다.

AI 인증은 각 PC의 Recheck에서 별도로 연결합니다. GitHub 로그인이나 저장소 복제로 AI 계정 연결이 이전되지는 않습니다. 연결 방법과 제공자별 확인 범위는 [AI 연결 안내](docs/AI_CONNECTIONS.md), [인증 구현·검증 상태](docs/AI_AUTH_STATUS.md)에 정리했습니다.

ZIP 구성만 비교하려면 **프로그램 비교**에서 **ZIP·패키지 구성 파일 비교**를 선택하세요. 이 모드는 Docker·AI·실행 입력 없이 구성 파일의 경로·종류·크기·내용 해시를 비교합니다. APK·JAR·MSIX·IPA도 유효한 ZIP 컨테이너인 경우 이 범위에서 검사하며 앱 실행·코드 의미·서명은 판정하지 않습니다. [패키지 비교 안내](docs/PACKAGE_COMPARISON.md)에 사용법과 크기·형식 제한이 있습니다.

## 소스로 실행하고 수정하기

Windows에서 소스 ZIP을 풀었다면 **`Run-Recheck.cmd`**를 실행하세요. 기존 Python 설치를 PATH 밖에서도 찾고, 없으면 설치를 안내한 뒤 프로젝트 전용 환경을 준비합니다. Git은 소스 ZIP 실행에 필요하지 않습니다. 개발할 때는 Python 3.11 이상을 사용하며 의존성은 `requirements-lock.txt`의 고정 버전으로 설치합니다. 수동 실행은 다음과 같습니다.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-lock.txt
.venv\Scripts\python launch.py
```

수정 후 자동 검사를 실행합니다. GUI 동작, 실제 외부 도구·AI 연결은 필요한 경로를 따로 확인합니다.

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m recheck environment
```

저장소의 주요 구성은 다음과 같습니다.

| 경로 | 용도 |
| --- | --- |
| `recheck/` | GUI·CLI·비교 엔진 |
| `ai_providers/` | AI 제공자 연결 |
| `resources/` | 앱 리소스·Ghidra 스크립트·Dockerfile |
| `tests/` | 자동 회귀 검사 |
| `scripts/` | 빌드·검증 작업 |
| `docs/` | 사용·개발 안내 |

## Windows 배포본 빌드

공개 Windows ZIP은 실행 가능한 앱과 고지·대응 소스 자료를 포함하고, 분석용 Docker 이미지는 사용자가 앱에서 처음 준비합니다. 배포 담당자가 별도 오프라인 환경을 만들 때만 다음 개발 명령을 사용합니다. 오프라인 이미지에는 자체 재배포 검토가 필요합니다.

```powershell
.venv\Scripts\python scripts\build_managed_images.py --help
.venv\Scripts\python scripts\export_managed_environment.py --include-ollama --update-manifest --output-dir environment
```

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

공개 앱 빌드는 `scripts/verify_source.py --output <새 검사 폴더>`를 실행한 뒤 `scripts/package_verified.py --output <새 패키지 폴더> --app-only`로 만듭니다. 전체 pytest와 Node 정규화 검사가 통과하고 검사 전후 소스가 같을 때만 `RECHECK_SOURCE_IDENTITY.json`을 갱신합니다. PyInstaller는 새 staging 폴더에 빌드하며 현재 실행 폴더를 바꾸지 않습니다. 실제 EXE와 ZIP도 별도로 확인합니다.

정식 출력은 `dist/Recheck`입니다. 실제 EXE 검사 후 `release_distribution.py`로 정식 출력과 외부 사용자 폴더를 함께 교체합니다. 이전 폴더 전체를 보존하고 ZIP까지 모든 파일 해시를 대조하며, 중단 시 journal로 복구합니다. ZIP은 실행 폴더 밖에 둡니다. 상세 명령과 제한은 [배포·복구 안내](docs/DISTRIBUTION_AND_RECOVERY.md)를 따르세요. clone·commit·push만으로 EXE가 갱신되지는 않습니다.

## GitHub에서 이어서 개발하기

소스와 선별 문서는 열람할 수 있으며 수정 권한은 관리자와 초대한 Collaborator에게만 부여합니다. 변경은 브랜치·PR·검사를 거칩니다. 실제 분석 원본·인증 정보·개인 QA·VM·모델은 각 PC에 남깁니다. 문제 제보에는 [보안 안내](https://github.com/leekmny/Recheck/blob/main/SECURITY.md)를 따르세요. 실행 파일은 검사한 버전별 릴리즈 ZIP으로 제공합니다.

자세한 협업·검사·배포 절차는 [GitHub 작업 안내](docs/GITHUB_WORKFLOW.md)를 참고하세요. 중단 지점, REA 기능별 구현 범위와 남은 일은 [프로젝트 상태](docs/PROJECT_STATUS.md)에 정리했습니다.

팀원은 [기여·개발 기록 안내](https://github.com/leekmny/Recheck/blob/main/CONTRIBUTING.md)부터 읽고 작업 브랜치와 PR을 준비하세요. 중요한 선택과 채택·보류한 대안은 [설계 결정 기록(ADR)](https://github.com/leekmny/Recheck/blob/main/docs/decisions/README.md)에 남깁니다. [초기 내장 로그인 실험](https://github.com/leekmny/Recheck/blob/main/experiments/embedded_login/README.md)은 통합 전 관찰 기록이며, 현재 제품 통합은 [연결 설계](https://github.com/leekmny/Recheck/blob/main/docs/superpowers/specs/2026-11-17-embedded-provider-connections-design.md)를 따릅니다.

실행 ZIP은 **Releases**에서 제공합니다. **Packages**는 Docker 이미지·개발용 의존성을 위한 별도 레지스트리이며 현재 사용하지 않습니다. `No packages published`는 실행 ZIP이 없다는 뜻이 아닙니다.
