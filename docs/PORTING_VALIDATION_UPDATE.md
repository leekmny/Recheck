# 언어 간 포팅의 기능 보존 검증 및 구조적 특성 비교

Recheck는 프로그램과 배포물의 차이를 재현 가능한 근거와 함께 확인하는 도구다. 0.2.0에서 확장한 시나리오 비교는 언어 간 포팅 검증 외에도 버전 간 회귀 검사와 교체 후보 검증에 적용한다. 같은 논리 입력을 적용해 출력·오류 처리·HTTP 응답·UI 상태·파일·SQLite의 차이를 확인하며, 비교 가능한 정적 표현에서 함수 대응과 구현 차이를 살핀다. 관찰 범위와 미검증 항목은 재현 자료에 함께 남긴다.

이 경로를 사용해 VERTEX의 TypeScript 원본과 Rust 포팅판은 22/22, Study Quiz의 JavaScript 원본과 Go 포팅판은 18/18 시나리오에서 일치했다. Linux x64 Docker에서 측정한 입력 범위의 결과다. [VERTEX Rust 비교 근거](https://github.com/leekmny/vertex-elevator-lab-rust/blob/main/docs/verification/rust-port-parity.json) · [Study Quiz Go 비교 근거](https://github.com/leekmny/study-quiz-go/blob/main/verification/recheck-summary.json)

## 실행 및 결과 구조

`recheck-porting-1` 명세에서 대상과 입력·실행 방식·비교 정책을 지정한다. 엔진은 명시한 소스·산출물·초기 데이터를 해시로 식별하고 격리 환경에 각 사본을 전달한다. 1회용 Docker 컨테이너와 고정 호스트 키를 확인하는 SSH worker를 제공한다. worker의 실제 OS·아키텍처, ID, 작업·요청·대상 식별값을 확인한다.

`recheck-porting-result-1` 결과는 기존 이력 저장소에 별도 스키마로 기록한다. 기존 바이너리 분석 스키마 `1.0`과 계산 산식은 유지한다. GUI의 **프로그램 비교 → 포팅 시나리오 비교**, CLI, 저장 JSON, HTML이 같은 결과를 쓴다. JSON과 HTML에는 인자·환경 값·원시 관찰이 포함되므로 공유하기 전에 검토한다. AI 연결 없이 실행한다.

- **테스트 범위 내 동작 일치율**은 일치한 시나리오 수를 등록 반복 모두에서 양쪽 관찰이 유효한 시나리오 수로 나눈 값이다. 분모가 0이면 미측정이다. 등록 수·시도 수·관찰 단계 수·빌드 및 대상 프로세스 실행 수는 따로 표시한다.
- **기대 결과**를 원본과 새 구현에 각각 적용한다. 같은 충돌은 동작 일치로 관찰될 수 있으며 정상 처리 계약은 위반한다. 약속한 오류 반환은 오류 처리 계약을 충족한다. 양쪽 비교용 필드 선택과 수치 오차가 개별 계약의 전체 출력 기대값을 줄이지 않는다. 필수 계약에 미측정이 남으면 수용을 유보한다. CLI의 종료 코드는 수집 상태를 나타내며, 계약 수용 상태는 결과의 `behavior.acceptance`에서 확인한다.
- **구조 특징 기반 유사도**는 표현별 관찰 범위에 의존하는 휴리스틱이다. 비교 불가일 때 점수는 미측정이다. .NET의 메서드 대응 범위와 CIL 본문 변화를 별도로 보여준다. 구조 근거의 추가 효과는 이번에 측정하지 않았다. [구조 검증과 재현 방법](../benchmarks/results/porting-structure-20261119/README.md)

## 플랫폼·실행 지원

| 플랫폼 | 구현 경로 | 실제 확인 범위 |
| --- | --- | --- |
| Linux x64 | 불변 이미지 ID의 Docker 또는 SSH | Docker ELF C→Rust와 API·C ABI·파일·SQLite 등 실측 |
| Linux ARM64 | ARM64 Docker 또는 SSH | x64 Docker 에뮬레이션에서 C→Rust ELF 정상 3/3, 결함 2/3, 다른 알고리즘 3/3. 네이티브 ARM64 하드웨어 미확보 |
| Windows x64 | 전용 VM SSH worker와 대화형 세션 UIA | 저장소 자체 C→Rust PE와 Chromium·UIA를 Windows에서 실측. 격리 Windows VM 미확보 |
| Windows ARM64 | 전용 VM SSH worker와 대화형 세션 UIA | 교차 빌드 PE의 정적 추출 확인. 실제 실행 미검증 |
| macOS x64 | 전용 worker SSH와 대화형 세션 AX | 교차 빌드 thin Mach-O 정적 추출 확인. 실제 실행 미검증 |
| macOS ARM64 | 전용 worker SSH와 대화형 세션 AX | 교차 빌드 thin Mach-O 정적 추출 확인. 실제 실행 미검증 |

| 축 | 구현 | 적용 범위 |
| --- | --- | --- |
| 런타임 | Python, Node.js, 명시적 빌드 후 Node.js로 실행하는 TypeScript, JVM, .NET | 사용자 프로그램과 런타임 식별값을 분리한다 |
| 콘솔 | stdout·stderr 바이트, 종료 코드와 근거, 파일·SQLite | 시간초과·취소·잘림·환경 실패를 구분한다 |
| HTTP·웹 | 준비 확인, HTTP 요청·응답, Chromium DOM·다운로드·PNG | 지정 loopback 사용. PNG는 별도 시각 근거 |
| 데스크톱 | Windows UIA, Linux AT-SPI, macOS AX | 소유 프로세스와 자식 앱만 관찰. Linux x64 가상 X11과 Windows 실제 세션에서 PySide6 조작. macOS AX 미검증 |
| 라이브러리 | DLL·SO·dylib 자식 하네스 | 함수·호출 규약·타입 지정. C→Rust SO 및 Windows DLL 하네스 실측. dylib 미검증 |
| 구조 | 네이티브, CIL, JVM 바이트코드, Python·JavaScript·TypeScript AST | 표현·프로필이 맞는 경우에 비교. Mach-O universal은 아키텍처별 thin 파일로 지정 |
| 패키지 | 기존 ZIP 구성 비교 | 파일·경로·해시 비교 |

## Linux worker 준비

Docker Linux 엔진을 시작한 뒤 저장소 루트에서 실행한다. 베이스 이미지 digest를 고정해 준비하고, 실제 대상은 네트워크를 분리한 임시 비특권 컨테이너에서 실행한다.

```powershell
New-Item -ItemType Directory -Force verification | Out-Null
docker build --platform linux/amd64 -f resources/porting/Dockerfile -t recheck-porting:linux-x64 .
$image = docker image inspect recheck-porting:linux-x64 --format '{{.Id}}'
@{workers=@{default=@{kind='docker';os='linux';arch='x64';image=$image}}} |
  ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 verification/porting-worker.json
.venv/Scripts/python.exe -m recheck porting-capabilities
.venv/Scripts/python.exe -m scripts.verify_porting --gui --require-native --runtime-matrix --native-platform linux/x64 --worker verification/porting-worker.json --output verification/porting-reproduction
```

ARM64에서는 `--platform linux/arm64`로 빌드한 실제 이미지 ID와 `arch`를 지정한다. x64에서 에뮬레이션이 필요하면 `allow_emulation`을 `true`로 명시한다. 결과의 `emulated`가 이를 보여준다. 검증기의 `--require-native`는 에뮬레이션을 제외한다.



## Windows·macOS·다른 Linux worker 연결

검증 전용 VM 또는 별도 격리 환경에 Python, 필요한 런타임, 선택 드라이버와 `recheck-porting-worker`를 준비한다. worker 계정은 게스트에 한정하고 일반 사용자 데이터와 자격증명을 두지 않는다. worker 설치는 관리자 소유 읽기 전용 경로에 둔다. 게스트 바깥으로의 공유 마운트·통신·자원 상한과 초기 이미지 복원은 게스트 운영 환경에서 관리한다. GUI는 접근성 API를 쓸 수 있는 전용 대화형 세션에서 실행한다.

고정 호스트 키를 확인해 `known_hosts`에 등록한다. 전용 키에 고정 worker 명령을 지정하고 터미널·포워딩을 제한한다. worker는 관리자가 설정한 `RECHECK_WORKER_ID`와 `RECHECK_WORKER_ISOLATION=dedicated-vm`을 사용한다. 호스트의 설정 JSON 예시는 다음과 같다.

```json
{"workers":{"default":{"kind":"ssh","os":"windows","arch":"x64","host":"recheck-worker","port":22,"worker_id":"win-x64-test"}}}
```

Windows는 Job Object로 대상 자식과 메모리 제한을 관리한다. Linux Docker는 CPU·메모리·PID 상한과 읽기 전용 이미지 및 임시 작업공간을 적용한다. 다른 SSH 게스트의 총자원과 외부 상태의 초기화는 격리 환경에서 관리한다. 작업 스트림이 닫히면 worker에서 취소한다. 전송에 실패하면 준비 또는 실행 단계, 작업 ID, 요청 해시와 확인 가능한 전송 프로세스 상태를 남긴다. 준비 단계 실패는 대상 미실행으로 기록하고, 제출한 실행의 응답이 없으면 대상 실행 여부와 프로세스 수를 미확인으로 기록한다. 응답이 사라진 실행의 자동 재실행을 막고 다음 시나리오는 별도 작업으로 요청한다. 배포 검증은 worker 코드의 SHA-256과 네이티브 아키텍처 근거를 확인한다.

## 대상과 시나리오 작성

`entry`·`files`는 명세 폴더 기준 상대 경로다. `cwd`, `args`, `env`, `runtime_version`, `language`, `metadata`로 대상과 의존성·컴파일러 정보를 기록할 수 있다. 선언값, 실제 런타임 버전, 진입 파일 SHA-256은 각각 남긴다. Java는 JAR 또는 `java_main`·`java_classpath`를 사용한다. TypeScript는 JavaScript 산출물과 빌드 도구·인자를 명시한다. `--approve-build` 또는 GUI 체크 항목으로 빌드를 허용한다. 실행에 필요한 의존성은 미리 준비한다.

`runtime_version`을 지정하면 worker에서 확인한 버전 전체와 일치해야 한다. Java 클래스패스 모드에서는 검색 순서에서 처음 발견되는 메인 클래스가 선언한 진입 파일 또는 JAR에 속하는지 확인한다. .NET은 앱이 단일 외부 프레임워크를 선언한 경우에 설치된 런타임 목록을 확인하고 `dotnet exec --fx-version`으로 요청 버전을 선택한다. SDK 버전은 빌드 도구 근거로 유지한다. 버전을 지정하지 않으면 일반 실행 경로를 사용하며 .NET의 실제 선택 버전은 미확인으로 기록한다. [Microsoft .NET CLI 참조](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet)

Worker에서 빌드한 네이티브·Java·.NET 진입 파일을 구조 분석에 지정하면 최대 8 MiB의 실행 산출물을 받아 SHA-256을 검증한 뒤 임시 입력으로 분석한다. 저장 이력에는 바이트 대신 크기·해시·검증 상태를 기록한다. 이전 worker나 크기 제한으로 전달받을 수 없으면 실행 해시와 일치하는 로컬 파일을 사용한다. 실행별 해시가 다르거나 대조할 수 없으면 구조 단계가 차단되고 동작 관찰은 유지된다. 독립적인 구조 표현은 `static.path` 또는 `static.files`로 지정한다.

`input`에 논리 인자, 표준입력, 환경을 지정한다. `mapping.reference`와 `mapping.candidate`에는 의미 보존 근거와 인자 순서·접두부·포트·locator·경로 차이를 명시할 수 있다. 준비·실행·정리 단계에는 각기 다른 ID를 사용한다. 초기 사본을 `fixtures`로 전달한다. 로케일·시간·난수 등 통제되지 않은 값은 `uncontrolled`에 기록하고 반복 횟수는 `repetitions`로 지정한다.

```powershell
python -m recheck --data-dir verification/porting-history porting-run manifest.json --worker worker.json --approve-build
python -m recheck --data-dir verification/porting-history porting-result run_<id>
python -m recheck --data-dir verification/porting-history report run_<id>
python -m recheck --data-dir verification/porting-history porting-rerun run_<id> revised-manifest.json --worker worker.json --scenario above --approve-build
```

재검사는 기존 기록의 시나리오·정책·기대 결과·매핑을 수정본의 대상에 적용하고 이전 ID를 새 결과에 연결한다. 초기 파일이나 데이터가 이전 SHA-256과 다르거나 미기록이면 실행을 차단하고 양쪽 해시를 보여준다. GUI에서 저장된 결과를 열면 그 실행의 명세와 worker 설정 경로를 불러온다. 다음 실행 대상으로 수정본을 선택하고 빌드가 필요하면 다시 허용한다. [드라이버별 단계 예시와 준비 절차](../samples/porting/README.md)

### 비교 정책

- 콘솔 기본값은 종료 코드·종료 원인·stdout/stderr 바이트를 비교한다. `legacy`는 줄바꿈·선택적 줄끝 공백 정규화, `text`는 UTF-8 정확 비교를 적용한다.
- `json`은 객체 키 순서를 무시하고 배열 순서를 유지한다. 중복 키, NaN, Infinity 및 비유한 실수는 미측정 처리한다. 정수와 실수, 누락과 null은 구분한다. `http-json`은 본문을 JSON으로 해석하고 상태·선택 헤더도 보존한다.
- `select`는 JSON Pointer 목록이며 `tolerances`는 지정 숫자 필드의 `abs`·`rel` 오차다. JSON 정수 오차는 정수의 정밀도를 유지해 계산한다. 비교 정책 적용 전후와 사라진 원시 차이를 모두 기록한다. `expected.reference`·`expected.candidate`에 독립 기대값을 넣는다. `step:quote#/status`처럼 관찰 필드도 사용할 수 있다.
- 시간초과나 관측 실패는 미측정으로 집계한다. 계약 기한은 `deadline_met: true`로 명시한다. 종료 코드가 0이 아니고 원인 근거가 부족하면 미확인으로 기록한다.
- 스크린샷은 HTML의 시각 근거로 보존한다. 기능 비교와 기대값에서는 스크린샷 단계, 해당 단계의 JSON Pointer, 전체 관찰 조회에 포함된 시각 근거를 제외한다. 화면 캡처만 등록한 경우 기능 일치율은 미측정이다.
- Worker의 관찰 응답이 4 MiB를 넘으면 상한 안에 들어오는 앞쪽 단계의 근거와 기존 종료 상태를 보존하고, 생략한 단계와 필드를 기록한다. 공유 라이브러리 비교 시나리오는 `steps`에 실제 `call`을 한 개 이상 등록한다.

### 어댑터 추가와 실패 해결

환경 연결·식별은 `porting_transport.py`, 단계 관찰은 `porting_drivers.py`, 비교 정책은 `porting_policy.py`, 구조 추출은 `porting_static.py`에서 확장한다. 정상·결함 예제와 시간초과·취소·정리 회귀를 함께 확인한다. 호스트 키·이미지·OS·아키텍처 불일치는 worker 설정을 먼저 확인한다. 단계 시간초과는 준비 경로와 선택 관찰을 확인한다. 도구가 없으면 해당 런타임·드라이버를 worker 이미지에 명시적으로 설치한 뒤 새 시도를 실행한다.



### 전용 worker 설치 명령

격리 게스트의 검토한 체크아웃에서 다음 명령으로 새 버전 경로를 준비한다. 지정한 Python에는 `httpx`와 Windows의 경우 `psutil`이 있어야 한다. 인터넷 없이 준비하려면 검토한 호환 wheel 디렉터리를 `--wheelhouse`로 지정할 수 있다.

```powershell
python scripts/prepare_porting_worker.py --destination "C:/RecheckWorker/porting-v1" --worker-id win-x64-test --python "C:/RecheckWorker/venv/Scripts/python.exe"
```

생성된 `worker-deployment.json`에 실행기 절대 경로와 소스 SHA-256이 들어 있다. SSH 서버의 전용 계정 또는 전용 키에 이 실행기를 고정하고 임의 명령과 포워딩을 허용하지 않는다. Windows의 대화형 앱은 GUI 세션에서 소유 프로세스가 시작되도록 게스트를 준비한다. [OpenSSH 서버 설정](https://man.openbsd.org/sshd_config.5) · [Windows OpenSSH 서버 설정](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh-server-configuration)

## 실험과 판정

| 실제 비교 | 정상·대체본 | 결함본 | 검증 수준 |
| --- | --- | --- | --- |
| Linux x64 C→Rust 콘솔 | 정상 11/11, 다른 알고리즘 11/11 | 8/11 | 불변 Docker 이미지, 제품 CLI |
| Windows x64 C→Rust 콘솔 | 정상 11/11, 다른 알고리즘 11/11 | 8/11 | 저장소 소유 예제, Windows Job Object로 실행. 전용 VM 전송 미검증 |
| Linux ARM64 C→Rust 콘솔 | 정상 3/3, 다른 알고리즘 3/3 | 2/3 | Docker x64 호스트의 ARM64 에뮬레이션. 경계값과 한글 입력 3개 |
| Python→Rust, Python→Node, Python→TypeScript | 각각 4/4 | 각각 3/4 | Linux x64 Docker 제품 실행 |
| Java→C#/.NET | 4/4 | 3/4 | Linux x64 Docker 제품 실행 |
| Python→Node HTTP | 1/1 | 0/1 | Linux x64 Docker. 요청 순서, HTTP 상태·본문 |
| C→Rust 공유 라이브러리 | 4/4 | 3/4 | Linux x64 Docker. 명시적 C ABI 실제 호출 |
| Python→Node 파일·SQLite | 1/1 | 0/1 | Linux x64 Docker. 생성·수정·삭제 및 SQLite 결과 |
| Chromium 웹 UI, Windows PySide6 데스크톱 UI | 각각 1/1 | 각각 0/1 | Windows 자체 예제로 실제 DOM·다운로드 및 UIA 조작 |
| Linux PySide6 데스크톱 UI | 1/1 | 0/1 | Linux x64 Docker, 가상 X11에서 실제 AT-SPI 입력·버튼·결과 관찰 |

정상 사례는 등록 기대 계약을 충족했다. 결함 사례는 원본 기대 결과 충족 여부와 별도로 새 구현의 계약 위반을 검출했다. C→Rust 결함은 네 번째 단위에 잘못된 요율을 적용한다. 콘솔 계약에는 경계값·빈 인자·음수·잘못된 형식·한글 및 공백을 포함했다. HTML과 GUI에서 같은 분모·행·원시 관찰을 확인했고, 기존 저장 결과의 문제 입력을 수정본에 적용했다. [실행 방식별 근거와 소스 해시](../benchmarks/results/porting-drivers-20261119/summary.json)

Windows 대화형 세션에서 자체 테스트 앱의 UI 스레드를 멈췄다. 전체 데스크톱을 UIA로 검색할 때는 정상 앱도 결과 읽기에서 시간초과됐다. Win32 창 목록에서 소유 PID를 확인한 뒤 해당 창만 UIA로 열도록 바꾸자, 멈춘 앱은 준비·입력·클릭 근거를 보존하고 시간초과됐으며 정상 앱의 결과 250을 수집했다. 시나리오는 미측정으로 집계했고 테스트 프로세스 여섯 개, Job 핸들 네 개, 임시 작업 폴더 두 개를 모두 정리했다. 같은 세션의 정상 대조 실행에서는 두 결과가 일치했다. [Windows UIA 멈춤 실측](../benchmarks/results/porting-drivers-20261119/summary.json)

Linux x64의 런타임 조합 8건은 GCC 12.2.0, Rust 1.92.0, Python 3.12.16, Node 24.22.3, TypeScript 5.9.3, JDK 21.0.13, .NET SDK 8.0.428 환경에서 확인했다. .NET의 초기 빌드 오류는 작업별로 쓰기 가능한 `XDG_RUNTIME_DIR`를 제공해 해결했다. [콘솔 검증 및 해시](../benchmarks/results/porting-console-20261119/summary.json) · [.NET 런타임 디렉터리 구현](https://github.com/dotnet/runtime/blob/release/8.0/src/coreclr/pal/src/sharedmemory/sharedmemory.cpp)

Linux x64와 ARM64 에뮬레이션의 Java→C# 버전 계약 실험에서는 .NET 런타임 `8.0.34`를 지정했을 때 각각 1/1 일치했다. 접두사 `8.0.3`과 SDK 버전 `8.0.428`은 각각 대상 실행 전에 차단됐으며 비교 가능 수는 0이었다. 같은 검사를 재현하려면 `samples/porting/console/fixtures.py`에서 Java→.NET 명세를 생성하고, 포팅본의 `runtime_version`을 각 값으로 지정해 `porting-run`을 실행한다. 버전, worker 코드·이미지·명세 해시는 콘솔 검증 JSON의 `runtime-version-contract`에 있다. [버전 계약 실측](../benchmarks/results/porting-console-20261119/summary.json)

Linux x64 C→Rust 라이브러리의 Ghidra 부분 관측에서 선택 함수 `parcel_quote`의 구조 유사도는 정상 68.3333%, 결함 67.5333%였다. 각 비교의 분모는 1이며 전체 관측 함수는 원본 13/17, Rust 538/588이었다. REA .NET 실험은 메서드 식별이 2/2 일치했고 `Total` CIL이 달랐다. PE·Mach-O·ELF 교차 빌드 자료, Python·JS·TS AST 및 JVM 실제 추출 근거는 구조 실험에 남겼다. [구조 실험](../benchmarks/results/porting-structure-20261119/README.md)

같은 콘솔 소스로 만든 ELF를 REA 6.9.0과 Ghidra 12.1.4에서 추가로 추출했다. 포팅 프로필에서 원본 `main`·`fee`와 Rust의 `main` 관련 함수를 선택했다. 정상과 결함은 각각 4.5173%로, 분모 12에 함수 대응 1개였다. 대체 알고리즘은 분모 4에서 대응 없이 0%였다. 세 결과의 구조 관찰은 부분 범위이며 동작 결과는 각각 11/11, 8/11, 11/11이다. [구조·동작 분모와 산출물 해시](../benchmarks/results/porting-console-20261119/summary.json)

Linux x64 Docker에서 빌드한 Java 두 구현을 동일 시나리오에서 각각 두 번 실행했다. 동작은 1/1 일치했고, `javap`의 구조 유사도는 98.3333%였다. 양쪽 모두 메서드 3/3개를 관측했다. 같은 worker에서 빌드한 C→Rust ELF도 동작 1/1 일치였으며, Ghidra 12.1.4가 관측한 구조의 부분 유사도는 4.3813%였다. 분모는 15이고 전체 함수 관측은 C 24/33개, Rust 677/746개였다. 두 실험에서 실행 산출물과 구조 입력의 SHA-256이 각각 일치했다. [Worker 빌드 산출물 실측](../benchmarks/results/porting-console-20261119/summary.json)

외부 네트워크를 분리한 Linux 컨테이너 내부에서 OpenSSH 연결로 약 5 MiB JAR를 전달해 해시 검증과 구조 분석을 완료했다. 약 9 MiB의 JAR는 전달 크기 제한으로 구조 분석이 차단됐고 동작 관측 1/1은 유지됐다. [SSH 전달 상한 실측](../benchmarks/results/porting-console-20261119/summary.json)

같은 Linux 시험 환경에서 대상과 자식이 실행된 것을 확인한 뒤 SSH 연결을 끊었다. 해당 시나리오는 미측정으로 남고, 전송 종료 상태 `-9`와 작업 ID가 저장됐다. 두 프로세스와 임시 작업 폴더는 정리됐고 다음 시나리오는 새 작업 ID로 통과했다. 별도의 Linux Docker 실험에서는 `/status`만 비교해 양쪽이 일치했어도 후보가 명시된 `amount`를 위반하면 계약 수용 상태가 실패로 남았다. [전송 장애 복구와 독립 계약 실측](../benchmarks/results/porting-console-20261119/summary.json)

### 릴리스 상태

보호된 main의 정확한 SHA와 필수 CI, Windows x64 배포 파일 해시, 동일 EXE의 실제 SSH worker 실행을 관문으로 구성했다. 모든 필수 근거가 있으면 새 태그와 draft를 만든 뒤 업로드한 자산을 내려받아 대조하고 공개한다. 현재 보호된 main의 기존 공개는 v0.1.7이다. 이번 작업의 새 태그·배포 자산·Release URL은 아직 없다. 격리 Windows x64 worker의 접속과 frozen EXE 비교 및 hosted Actions는 실제 게시 전에 확인해야 한다. [자동 릴리스 운영 안내](PORTING_RELEASE.md)

Windows 릴리스 관문은 `porting-product`와 `porting-linux-x64` 작업의 성공을 각각 확인한다. Linux ARM64 네이티브 검증 작업은 `ubuntu-24.04-arm`의 실행기·Docker 서버·이미지 아키텍처와 비에뮬레이션 결과를 확인해 별도 근거를 남기도록 구성했다. 호스팅 실행은 미검증이다. [GitHub 호스팅 실행기](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)

외부 원격을 연결하지 않은 로컬 시험 저장소에서 `0.2.0` 후보 커밋 `c85d348d14453b08e60ef48fdaaa45d9a625ec47`을 빌드했다. pytest 2,410건·Node 정규화 7건과 소스·배포본 포팅 검사, 실제 Windows GUI 시작, ZIP 구성 6,351개 및 배포 고지를 확인했다. Windows와 Linux의 독립 체크아웃에서 빌드 입력 147개가 동일했고, ZIP과 별도 EXE의 해시가 검증 기록과 일치했다. ZIP에서 푼 EXE로 Linux x64 격리 worker의 정상·결함·대체 구현을 실행했다. Windows x64를 요구하는 게시 관문은 이 결과를 거부했고 GitHub API 호출은 0건이었다. 리허설 근거는 `verification/porting-release-rehearsal-20261119/rehearsal-result.json`에 있다.

로컬 Windows x64에서 Python 3.14.8로 EXE를 빌드했다. 배포본에서 GUI·CLI·JSON·HTML, 재검사, Linux x64 Docker worker 연결을 확인했다. 상대 `--data-dir`을 지정한 배포본 CLI에서 Java 산출물을 받아 구조 분석했다. 독립 계약 실험은 배포본 GUI·CLI·HTML에서 동작 1/1 일치와 후보 계약 실패를 함께 표시했다. ZIP 전체 구성 파일의 경로·내용·크기·SHA-256을 실행한 배포 디렉터리와 대조했고 번들 라이선스도 검증했다. 로컬 체크섬은 `verification/porting-windows-uia-current/package/local-assets.json`, EXE 실행 근거는 `verification/porting-windows-uia-current/frozen/verification.json`에 있다. 실행 근거의 `working_tree_dirty`가 `true`인 것은 빌드에 로컬 작업 트리 변경이 들어갔음을 뜻한다. 소스 검사는 pytest 2,406건 통과·13건 건너뜀, Node 정규화 7건 통과였고 Ruff와 actionlint도 통과했다. 실제 Windows 대화형 데스크톱을 사용한 별도 검사는 8건 통과였다.

## 요구사항별 확인 기록

| 명세 범위 | 확인 결과와 근거 | 남은 범위 |
| --- | --- | --- |
| 1–2 목적·기준선 | 깨끗한 `5640e79`에서 분기. 변경 전 pytest 2,224 통과·3 건너뜀, 정규화 7 통과 | 초기 환경에는 Python 3.14.8이 PATH에 없어 설치된 uv 인터프리터로 가상환경 구성 |
| 3–5 환경·공통 엔진·격리 | 6개 플랫폼 경로, 불변 이미지, 헤더 검사, 제한·독립 사본·검증 작업 ID. Linux Docker에서 OOM·시간초과·출력 잘림 판정과 취소 후 컨테이너 0개. 격리 Linux 실험실의 OpenSSH 연결 중단·정리·다음 작업 복구 확인 | 전용 Windows/macOS와 실제 ARM64 하드웨어, 별도 호스트 간 장애 복구 실측 |
| 6–9 시나리오·관찰·판정 | 입력·실제 매핑·원시 출력 보존. 정상/오류 계약, 파싱 실패, 정규화 차이, 반복·상한·취소 테스트 및 실제 정상·결함 실행 | 지원 도구가 없는 세션의 UI 및 신규 플랫폼 조합 검증 |
| 10 구조 | REA/Ghidra 네이티브·CIL, Python·JS·TS·JVM 추출. 표현·범위·분모를 기록 | 함수 분할·통합은 수동 메모로 표현. 근거 추가의 검토 효율 미측정 |
| 11 제품 경로·재검사 | 실제 RECHECK Qt 화면에서 Linux Docker 비교 2/2. CLI와 저장 JSON 대조, HTML 생성, 결함 입력 재검사 1/1 | 전용 Windows VM을 연결한 제품 GUI 실행 실측 |
| 12 자체 검증 예제 | C→Rust 3변종, Python·Node·TypeScript·Java·.NET 및 C ABI·HTTP·웹·데스크톱·파일·SQLite | 여러 플랫폼 조합의 전수 검증은 별도 범위 |
| 13 검사 층위 | 소스 pytest와 Ruff, JS 정규화, 실제 대상·구조 도구·제품 엔진·RECHECK 화면·대상 화면을 각각 확인 | 현재 브랜치의 GitHub hosted Actions 실행 미확인 |
| 14–16 자료·보고 | 해시 및 재현 자료와 예제를 저장하고 README·현황·피드백·로드맵에서 연결 | 각 실행의 원시 기록은 로컬 검증 폴더에 보관. 공개 JSON에는 선별 요약을 담음 |
| 17 배포 | 정확한 소스와 ZIP·EXE를 대조하는 자동화, 로컬 후보의 실제 EXE 빌드·실행·ZIP 무결성, 플랫폼 관문 차단 확인 | 보호된 main의 해당 커밋 필수 CI, Windows x64 SSH worker의 동일 EXE 실측 및 신규 GitHub Release 미실행 |
