# 포팅 검증 예제

택배 요금은 무게가 0–100일 때 기본 요금 100에 단위당 15를 더합니다. 각 예제는 두 구현에 같은 논리 입력을 적용합니다. 원본과 포팅본 모두 같은 계약으로 검증하므로 `--defect`를 사용하면 해당 관찰에서 동작 불일치와 포팅본의 계약 위반을 확인할 수 있습니다.

| 예제 | 원본과 포팅본 | 정상·결함 관찰 | 워커 요구사항 |
| --- | --- | --- | --- |
| `http` | Python HTTP 서버 / Node.js HTTP 서버 | 무게 100의 요금 1600 / 1599. 음수·빈 문자열·잘못된 형식은 422 | Python과 Node.js, 지정된 루프백 포트 |
| `web` | 폼과 버튼으로 구성한 두 독립 웹 페이지 | 접근성 locator가 다른 두 화면에 무게 10을 입력해 요금 250 / 249 확인. 다운로드 데이터와 시각 근거 수집 | Python Playwright 1.48 이상과 Chromium, 지정된 루프백 포트 |
| `desktop` | PySide6 수식 기반 창 / PySide6 요금표 기반 창 | 프로세스에 속한 컨트롤에 무게 10을 입력해 요금 250 / 249 확인 | 전용 대화형 데스크톱과 PySide6, Windows UIA용 `pywinauto`, Linux AT-SPI용 PyGObject 및 AT-SPI 2, macOS AX용 `pyobjc-framework-ApplicationServices` 및 손쉬운 사용 권한 |
| `library` | C 공유 라이브러리 / Rust `cdylib` | C ABI로 길이 2인 출력 버퍼에 리틀엔디언 요금 250 / 249 기록. 상한·음수·짧은 버퍼도 검사 | 워커 OS에 맞는 C 컴파일러와 Rust 컴파일러, 격리 위치에서의 명시적 빌드 승인 |
| `state` | Python `sqlite3` / Node.js `node:sqlite` | 파일 생성·수정·삭제 후 SQLite 행의 요금 250 / 249 확인. 두 실행 모두 같은 초기 파일을 받음 | Python과 `node:sqlite`를 제공하는 Node.js |

대상 소스와 초기 상태는 명세의 위치를 기준으로 읽습니다. 예제 폴더에 새 경로로 명세를 생성하고, 같은 OS와 아키텍처의 워커를 연결합니다. 아래 예제는 기본 워커 이름 `default`를 사용합니다.

```powershell
python samples/porting/make_scenario.py library --os linux --arch x64 --output samples/porting/library/normal.json
python samples/porting/make_scenario.py library --os linux --arch x64 --defect --output samples/porting/library/defect.json
python -m recheck porting-run samples/porting/library/normal.json --worker ./worker.json --approve-build
python -m recheck porting-run samples/porting/library/defect.json --worker ./worker.json --approve-build
```

`http`, `web`, `desktop`, `state`도 같은 스크립트로 명세를 만들 수 있습니다. `http`와 `web`의 `--port`는 워커에서 비어 있는 루프백 포트를 지정합니다. 생성한 `.json`에는 양쪽에 적용되는 경로 및 locator 매핑, 관찰 단계, 기대값이 들어 있습니다.

## 시나리오 작성

각 단계에 시나리오 안에서 고유한 `id`와 표의 `action`을 지정합니다. `timeout_seconds`로 해당 단계의 기한을 줄일 수 있습니다. 시나리오 전체에는 `timeout`이 적용됩니다. 대상별 경로와 접근성 locator는 `mapping.reference.steps`, `mapping.candidate.steps`에서 단계 ID별로 지정하고 `reason`에 동등한 입력을 적용하는 근거를 적습니다.

| 방식 | `action` | 필드와 관찰 |
| --- | --- | --- |
| HTTP·웹 준비 | `ready` | `path`, 선택 `status`, `method` (`GET` 기본값 또는 `HEAD`). `status`를 생략하면 2xx로 준비를 판정합니다. |
| HTTP 요청 | `http` | `path`, `method`, `query` 객체 또는 이름·값 쌍의 배열, `headers`, `observe_headers`. 본문은 `body`(UTF-8), `body_b64`, `json` 중 하나로 지정합니다. 상태 코드, 선택한 헤더 값 배열, 원시 본문 `body_b64`가 남습니다. |
| 웹 페이지·조작 | `navigate`, `fill`, `click`, `wait`, `download` | `navigate`는 `path`, `fill`은 `locator`와 `value`를 받습니다. `click`과 `download`는 하나의 컨트롤을 지정합니다. `wait`는 `state` (`visible`, `hidden`, `attached`, `detached`)와 선택 `text`를 확인합니다. 다운로드는 제안 파일명, 크기, SHA-256과 내용으로 관찰합니다. |
| 웹 읽기 | `read`, `screenshot` | `read`는 `locator`와 `property` (`text`, `value`, `visible`, `checked`, `enabled`, `attribute`)를 받습니다. `attribute`에는 속성 `name`도 지정합니다. `screenshot`은 현재 페이지의 PNG를 시각 근거로 수집합니다. |
| 데스크톱 준비·조작 | `ready`, `fill`, `click`, `wait`, `read` | `locator`는 `window`, `name`, `id`, `role`에서 필요한 값을 고른 객체입니다. `fill`에는 `value`를 지정합니다. `ready`와 `wait`는 `state` (`visible`, `hidden`, `enabled`, `exists`, `missing`)와 선택 `text`를 확인합니다. `read`는 `property` (`text`, `name`, `value`, `visible`, `enabled`)를 읽습니다. |
| 라이브러리 | `call` | `abi`, `symbol`, `arg_types`, `return_type`, `args`를 모두 지정합니다. ABI는 `cdecl` 또는 Windows의 `stdcall`입니다. 정수는 `i8`부터 `i64`, `u8`부터 `u64`까지의 8·16·32·64비트 타입을 사용합니다. `f32`, `f64`를 지원하며 인자에는 `cstring`, `buffer`, 반환에는 `void`를 쓸 수 있습니다. |
| 파일·디렉터리 | `files` | `paths`에 워커 실행 디렉터리 기준 상대경로 배열을 지정합니다. 지정한 항목의 존재 여부, 디렉터리 자식 경로, 파일 크기·SHA-256·원시 바이트를 남깁니다. |
| SQLite | `sqlite` | `path`, `table`, `columns`, 선택 `order_by`를 지정합니다. 파일 존재 여부와 일반 테이블에서 선택한 열·행을 관찰합니다. BLOB은 Base64로 기록합니다. |

웹 locator는 CSS 문자열이나 `{"css": "#result"}`, `{"label": "Weight"}`, `{"test_id": "price"}`, `{"role": "button", "name": "Quote"}` 같은 객체를 받습니다. 웹 `ready`에 locator를 지정하면 현재 페이지에서 요소를 확인합니다. 데스크톱 `role` 값은 Windows UIA·Linux AT-SPI·macOS AX가 보고하는 이름을 사용합니다.

라이브러리 호출의 버퍼 인자는 `{"size": 2}`처럼 바이트 수를 지정하고, 입력이 필요한 경우 `initial_b64`를 추가합니다. 함수가 버퍼 길이 인자를 요구하면 `arg_types`와 `args`에 함께 명시합니다. `buffers`의 키는 0부터 시작하는 인자 위치입니다. 각 호출은 새 하네스 프로세스에서 수행되며 함수 반환값, `errno`, Windows `GetLastError`, 지정 버퍼, 하네스 원시 출력과 종료 코드를 기록합니다.

```json
{
  "id": "call",
  "action": "call",
  "abi": "cdecl",
  "symbol": "parcel_quote",
  "arg_types": ["i32", "buffer", "u32"],
  "return_type": "i32",
  "args": [10, {"size": 2}, 2]
}
```

`prepare`에는 실행 전 파일·SQLite 스냅샷을, `steps`에는 대상 시작 이후의 동작과 관찰을 넣습니다. 콘솔은 종료 후 스냅샷을 `steps`에 둡니다. 서비스와 GUI는 조작을 마친 뒤 워커가 프로세스를 종료하고, 성공한 실행의 `cleanup`에서 파일과 SQLite를 한 번 더 읽을 수 있습니다. 파일 경로는 작업 폴더 안에서 해석되고 링크와 재분석 지점은 거부됩니다. SQLite 조회는 읽기 전용입니다. `expected.reference`와 `expected.candidate`에 `step:price`처럼 단계 ID를 사용해 각 구현의 계약을 정합니다. HTTP 응답 객체 전체를 기대값으로 비교하거나 `policy`의 `select`에 `/status`, `/headers/content-type`처럼 JSON Pointer를 지정할 수 있습니다.

## GUI 워커 준비

웹 자동화 패키지와 브라우저는 대상 작업을 받기 전에 동일한 워커 Python 환경에서 설치합니다. Linux에서는 Chromium을 실행할 운영체제 라이브러리도 워커 이미지에 미리 준비합니다. [Playwright 설치 안내](https://playwright.dev/python/docs/intro)

```powershell
python -m pip install 'playwright>=1.48'
python -m playwright install chromium
python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); browser=p.chromium.launch(); print(browser.version); browser.close(); p.stop()"
```

데스크톱 워커와 대상 앱을 같은 로그인 데스크톱에서 실행하고 접근성 계층에 컨트롤이 표시되는지 확인합니다. 다음 검사는 패키지나 접근성 세션의 준비 여부를 보여줍니다. 실제 입력과 결과 확인은 예제 시나리오를 실행해 검증합니다.

| 플랫폼 | 준비와 확인 |
| --- | --- |
| Windows | 전용 로그인 세션의 워커 Python에 `python -m pip install pywinauto`를 실행합니다. `python -c "from pywinauto import Application; Application(backend='uia')"`로 UIA 초기화를 확인합니다. 서비스의 Session 0에서 실행되는 일반 SSH 진입점에는 로그인 데스크톱으로 연결하는 배치 구성이 별도로 필요합니다. [Windows 서비스와 대화형 세션](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services) |
| Linux | 디스플레이와 같은 세션의 D-Bus·AT-SPI 2 서비스, 워커 Python에서 사용할 수 있는 PyGObject·Atspi typelib을 준비합니다. `python -c "import gi; gi.require_version('Atspi','2.0'); from gi.repository import Atspi; Atspi.init(); print(Atspi.get_desktop_count())"`로 세션을 확인합니다. Qt 대상의 접근성 트리를 활성화하려면 대상 `env`의 `QT_LINUX_ACCESSIBILITY_ALWAYS_ON`을 `1`로 지정할 수 있습니다. [Qt AT-SPI 안내](https://doc.qt.io/qt-6/qaccessible.html) |
| macOS | 로그인 세션의 워커 Python에 `python -m pip install pyobjc-framework-ApplicationServices`를 실행합니다. 손쉬운 사용에서 워커 프로세스에 권한을 부여하고 `python -c "import ApplicationServices as ax; print(ax.AXIsProcessTrusted())"`로 확인합니다. [PyObjC ApplicationServices](https://pyobjc.readthedocs.io/en/latest/apinotes/ApplicationServices.html) |

데스크톱 대상은 시작한 프로세스와 그 자식에 속한 컨트롤로 탐색 범위를 제한합니다. 컨트롤을 하나로 좁히도록 명세의 `window`, `name`, `id`, `role`을 조정합니다. 버튼은 플랫폼의 접근성 호출 기능을 통해 실행합니다. 같은 이름의 컨트롤이 여러 개 있거나 접근성 동작을 제공하지 않으면 해당 단계가 실패합니다.

Linux x64의 실측 환경은 전용 Docker 이미지에서 `dbus-run-session`과 `xvfb-run`으로 시작한 가상 X11 디스플레이입니다. 이미지에는 PyGObject/AT-SPI, Xvfb·Xauth·D-Bus, PySide6와 XCB 라이브러리가 포함됩니다. 워커 실행 스크립트는 데몬 로그를 stderr로 보내 응답용 stdout을 보존합니다. 저장소 루트에서 아래 명령을 실행하면 같은 설정으로 워커를 빌드하고 예제를 검증할 수 있습니다. [Dockerfile](desktop/Dockerfile) · [실행 스크립트](desktop/worker-python.sh)

```powershell
docker build --platform linux/amd64 --file samples/porting/desktop/Dockerfile --tag recheck-porting-desktop:linux-x64 .
$DesktopImage = docker image inspect recheck-porting-desktop:linux-x64 --format '{{.Id}}'
$DesktopWorker = @{ kind = 'docker'; os = 'linux'; arch = 'x64'; image = $DesktopImage }
@{ workers = @{ default = $DesktopWorker } } | ConvertTo-Json -Depth 4 | Set-Content -Encoding utf8 ./worker-desktop.json
python samples/porting/make_scenario.py desktop --os linux --output samples/porting/desktop/xvfb-normal.json
python samples/porting/make_scenario.py desktop --os linux --defect --output samples/porting/desktop/xvfb-defect.json
python -m recheck porting-run samples/porting/desktop/xvfb-normal.json --worker ./worker-desktop.json
python -m recheck porting-run samples/porting/desktop/xvfb-defect.json --worker ./worker-desktop.json
```

## 격리와 결과 확인

제품에서 지정한 워커에 대상 및 보조 파일을 전송하고 실행마다 임시 작업 폴더를 새로 만듭니다. `base_url`에는 `http://127.0.0.1:47625`처럼 루프백 IP와 포트를 지정하고, 요청에는 해당 서비스의 상대 경로를 적습니다. HTTP 드라이버는 리디렉션을 자동으로 따르지 않습니다. 웹 드라이버는 일회용 Chromium 컨텍스트를 만들고 지정한 서비스 원점으로 요청을 제한합니다. Docker의 네트워크 및 자원 제한과 전용 가상 머신의 운영 정책이 작업 전체의 격리를 담당합니다.

실행 실패나 관찰 기한 초과 전에 완료한 단계는 저장된 결과에서 확인할 수 있습니다. 전체 관찰 한도를 넘기면 출력 잘림 상태가 기록됩니다. 결과 이력은 `python -m recheck porting-result RUN_ID`로 조회하고 HTML은 `python -m recheck report RUN_ID`로 생성합니다. 웹 HTML 보고서의 **시각 관찰**에는 두 PNG와 바이트 비교가 들어갑니다. 화면 레이아웃이 달라도 가격·다운로드 등 시나리오가 지정한 동작 관찰이 같으면 동작 일치로 판정합니다.

## 실제 실행 기록

| 환경과 방식 | 정상본 | 결함본과 검출 지점 |
| --- | --- | --- |
| Linux x64 Docker 제품 워커 · C→Rust 라이브러리 | 4/4 일치 · `run_6b1bb978df4e4bdfb8064113fedf360a` | 3/4 일치 · `valid_quote`의 출력 버퍼 · `run_bec69bf1020d48968314e21a256d3d21` |
| Linux x64 Docker 제품 워커 · HTTP | 1/1 일치 · `run_dfcd06360af849469a1bf8034726f07e` | 0/1 일치 · 여섯 요청 중 `upper_bound`의 요금 1600/1599 · `run_5625673ee1574336a4afee1dc3ef848e` |
| Linux x64 Docker 제품 워커 · 파일·SQLite | 1/1 일치 · `run_69f16f193a2b4292898e3ce074112f88` | 0/1 일치 · `after_db`의 요금 250/249 · `run_b55ad995e58d498ea483e793875b7b61` |
| Windows x64 제품 엔진의 저장소 예제 경로 · Chromium | 1/1 일치 · `run_bf98e9e3ac6a4f1e85e425d165db502c` | 0/1 일치 · `price`가 250/249 · `run_6758649727a04cb6aea807bb0d0d08ec` |
| Windows x64 제품 엔진의 저장소 예제 경로 · UIA | 1/1 일치 · `run_e6a06c9a6f0240eba01725bc9ba9f674` | 0/1 일치 · `price`가 250/249 · `run_f3d81a21353e44c3b94f208f1c63ba77` |
| Linux x64 Docker 제품 워커 · 가상 X11 및 AT-SPI | 1/1 일치 · `run_34a2cf3dccab44978255ed3d8f45bb29` | 0/1 일치 · `price`가 250/249 · `run_1810311b3eb8452ea4a4e42e2d84784c` |

정상본은 모든 필수 기대값을 충족했습니다. 각 결함본은 표의 지점에서 불일치와 후보 구현의 기대값 위반을 기록했습니다. 정상 웹 실행의 HTML을 Chromium에서 열어 두 이미지가 렌더링되고 PNG 차이가 별도로 표시되는 것을 확인했습니다. Windows 웹·데스크톱 실행은 저장소에 포함된 대상만 실행하는 테스트 경로로 실측했습니다. Windows 원격 GUI 워커 연결, 가상 X11 환경 밖의 Linux 데스크톱 세션, macOS AX의 실제 앱 조작은 미검증입니다. 대상 파일 및 산출물 해시, 실측 집계, 도구 버전, 정상 웹 결과의 PNG 해시는 [실측 근거 JSON](../../benchmarks/results/porting-drivers-20261119/summary.json)에 있습니다. [업데이트 기록](../../docs/PORTING_VALIDATION_UPDATE.md)
