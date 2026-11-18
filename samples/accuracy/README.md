# 고정 정답으로 검사하는 비교 샘플

## 실행 경로 구분

`scripts/evaluate_accuracy.py`의 기본값은 기존 `ManagedExecutionAdapter`입니다. 제품에 고정된 이미지가 없으면 빌드 성공 후에도 실행 준비 단계에서 차단하며 다른 이미지로 바꾸지 않습니다.

이미 설치된 Linux 실행 이미지를 **별도 독립 출력 비교 기준선**으로 평가하려면 `--execution-image`를 명시합니다. 이 경우에만 기존 `DockerAdapter`를 사용하고, 로컬 Linux amd64 Docker·immutable image ID·실행 wrapper를 먼저 검증합니다. 결과의 `execution_backend=standalone_docker_baseline`, `product_managed_execution=false`는 제품 Engine 또는 managed 이미지의 검증 결과가 아님을 뜻합니다. 기본 경로와 제품의 pin 검증은 그대로 유지합니다.

```powershell
.venv\Scripts\python scripts\evaluate_accuracy.py `
  --output verification\accuracy-고유실행ID `
  --compiler-image 설치된-컴파일러-이미지 `
  --execution-image 설치된-실행-이미지 --concurrency 3
```

기존 평가의 분모는 **71등록 입력 × 5비교 유형 = 355판정**입니다. 원본 관측을 비교 유형 사이에서 공유하고 동일 파일 비교의 다른 쪽은 다시 실행하므로 정확도 대상 실행은 426회입니다. timeout·출력 초과·자식 프로세스 회복 시험 3회는 별도로 기록해 총 실행 시도는 429회이며, 정확도 355판정에 합산하지 않습니다. 새 `benchmarks/capstone`의 24입력·4방법 실험과도 합산하지 않습니다.

`--reuse-build`는 이미 있는 출력 폴더의 빌드를 재사용합니다. 2026-11-18 실험에서는 재사용 전에 33개 ELF의 SHA-256을 전부 확인하고 `build-reuse.json`에 기록했습니다. 일반 재실행은 새 폴더에서 다시 빌드하거나 같은 수준으로 기존 산출물 identity를 확인해야 합니다.

2026-11-18의 원본 managed 경로는 실제 33개 ELF 빌드 후 pin 이미지 미설치로 차단됐습니다. 설치된 legacy 이미지를 명시한 대표 입력 시험은 5판정·6대상 실행과 회복 시험 3회를 수행했습니다. 전체 평가 결과와 원시 근거는 `benchmarks/results/accuracy-20261118-standalone-full`에 별도로 보존합니다. 이 실행에는 모델 요청이 없습니다.

### 이미지가 없는 새 팀원의 준비 경로

Python 잠금 의존성과 로컬 Linux amd64 Docker를 준비한 뒤 저장소의 공개 Dockerfile로 별도 기준선 이미지를 만들 수 있습니다. 아래는 **준비 명령**이며 이번 세션은 이미 설치된 이미지로 실측했습니다. 새 빌드의 base/apt 패키지·이미지 ID는 과거 이미지와 달라질 수 있으므로 새 실행 ID에서 oracle을 다시 확인하고 이미지·컴파일러 identity를 결과에 남깁니다.

```powershell
docker build --platform linux/amd64 -t recheck-capstone-compiler:local resources/docker/compiler
docker build --platform linux/amd64 -t recheck-capstone-execution:local resources/docker/linux
.venv\Scripts\python scripts\evaluate_accuracy.py `
  --output verification\accuracy-새실행ID `
  --compiler-image recheck-capstone-compiler:local `
  --execution-image recheck-capstone-execution:local --concurrency 3
```

이 이미지는 standalone 기준선용입니다. 제품 Managed 환경은 앱의 명시적 준비 절차와 자체 manifest/receipt 검증을 따라야 합니다. 검증을 생략하거나 임의 ID를 제품 manifest에 넣는 방식으로 준비하지 않습니다. 원시 데이터의 재집계만 확인할 때는 [공개 결과의 Python 명령](../../benchmarks/results/accuracy-20261118-standalone-full/README.md)을 사용하면 Docker·모델 실행이 필요 없습니다.

C 프로그램 6개는 제한된 값을 입력받으며 등록 사례에서 부호 있는 정수의 오버플로를 피한다. `oracle.json`에는 실행·AI 검토 전에 고정한 UTF-8 표준출력·표준오류·종료 코드가 있다. `MUTANT=1`은 문서화한 의미 변경 하나, `ALTERNATE=1`은 같은 결과를 내는 다른 구현을 선택한다. 평가기는 O0·O2·대체 구현·변경 구현·심볼 제거 변형을 빌드한다. 동일 파일 비교는 O0 바이너리를 다시 실행한다.

소스 루트에서 실행한다.

```powershell
.venv\Scripts\python scripts\evaluate_accuracy.py --output verification\recheck-next-20261116\accuracy
```

ELF 컴파일에는 로컬 Docker 컴파일러 이미지를 사용하고, 대상 실행에는 Recheck의 `ManagedExecutionAdapter`와 패키지에 지정된 불변 Linux 실행 이미지를 사용한다. PE x64는 설치된 MinGW가 있을 때 빌드한다. PE 빌드 성공은 Windows 실행 지원을 뜻하지 않는다. 필요한 도구·이미지가 없으면 차단 사유로 기록한다.

PE C·C++에는 각각 `x86_64-w64-mingw32-gcc`·`x86_64-w64-mingw32-g++`가 필요하다. 언어별 컴파일러가 없으면 사용할 수 없다고 기록하며 호스트 `g++`로 대신 빌드하지 않는다. 생성한 PE는 네이티브 x64 콘솔 헤더 검사를 통과해야 식별 정보를 기록한다.

원본 관찰은 사례마다 한 번 수집해 비교 유형 사이에서 재사용하고 확인 자료에 이를 명시한다. 동일 파일 비교의 오른쪽 대상은 따로 실행한다. 모든 등록 사례는 평가표에 남기되 차단·중단 사례는 측정 분모에서 제외한다. 이 하네스는 실제 어댑터·비교 함수를 검사하며 GUI나 전체 엔진 흐름까지 검증했다고 표시하지 않는다.

변경 구현은 다음과 같다. arithmetic은 입력 0에서만 1을 더한다. branch는 `<=100`을 `<100`으로 바꾼다. loop는 마지막 값까지 더하던 반복에서 마지막 값을 제외한다. strings는 빈 입력을 1바이트로 센다. statistics는 가장 작은 두 값이 반복될 때 합계에 1을 더한다. io는 인자로 받은 증분을 적용하지 않는다. UTF-8 문자열 길이는 유니코드 문자 수가 아닌 바이트 수다.

`recovery.c`는 시간 제한·출력 제한·멈춘 자식 프로세스의 제한된 컨테이너 정리만 시험한다. 시스템 설정이나 사용자 파일은 변경하지 않는다.

`many_functions.cpp`는 디컴파일 상한을 시험하기 위해 인라인하지 않는 C++ 템플릿 인스턴스 129개를 제공한다. 유효한 제한 정수 입력에는 8256을 더한 값을 출력한다. 잘못된 입력은 표준오류에 `invalid integer`를 쓰고 종료 코드 2를 반환한다. 하네스는 기본·심볼 제거 ELF·PE를 정적분석 부하 샘플로 빌드한다. 이 추가 파일은 위 6개 프로그램의 동적 정확도 분모에 포함하지 않는다.

정답표에는 잘못된 입력·UTF-8 바이트 사례를 포함한 입력 71개가 등록돼 있다. 비교 유형 5개에 걸친 판정은 355쌍이며 변경 구현의 기대 차이는 정확히 19개다. 기준 관찰은 5개 비교에서 재사용하고 동일 파일 비교의 오른쪽 대상은 새로 실행한다. 전체 평가는 대상 실행을 총 426회 수행한다.
