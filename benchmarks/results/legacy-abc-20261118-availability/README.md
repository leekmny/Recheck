# 기존 A/B/C 실험 재현 가능성 점검

2026-11-18에 원래 `scripts/run_benchmark.py --help`와 `scripts/run_benchmark_revision.py --help`를 실제 실행했고 둘 다 종료 코드 0을 확인했습니다. 이 기록은 과거 성능 수치를 다시 측정한 결과가 아닙니다. 모델 요청은 0회입니다.

원래 benchmark에 설치된 compiler 태그를 명시해 `--prepare-only`도 실제 호출했습니다. `build_samples`의 컨테이너에서 `/out` 디렉터리를 만들 권한이 없어 먼저 실패했습니다. 해당 이미지가 다른 bind-mount 빌드에서 컴파일에 성공했다는 사실만으로 이 오래된 wrapper 계약까지 호환된다고 주장하지 않습니다. 실제 실패 원문은 `command-receipts.json`에 있습니다.

별도로 원래 `run_benchmark.read_json`에 소스에 정의된 실제 기본 할인 샘플 경로의 `tests.json`을 전달했습니다. **FileNotFoundError**가 발생했습니다. 기본 샘플 루트와 이전 `verification/benchmark-20261113-115534` 원시 snapshot도 이 checkout에는 없습니다. 오류 경로는 shell의 문자 인코딩으로 추측하지 않고 설치된 Python 소스의 Unicode 상수에서 해석했습니다.

할인 샘플의 원래 계약은 다음과 같습니다.

- 샘플 루트에 `tests.json`이 있어야 합니다.
- `original`과 `reimplementation` 실행 파일은 루트 또는 `bin/linux-x64`에 있어야 합니다.
- 대응 `original.c`, `reimplementation.c`는 루트 또는 `src`에 있어야 합니다. 준비 단계에서 source provenance SHA를 기록합니다.
- 할인 정답은 원본 `>=100`·재구현 `>100` 경계, 정수 입력 범위 `[0,1000000]`, 등록된 입력 계약에 따릅니다.
- A는 원 C 소스 대신 실제 Ghidra 추출 함수 정보와 중립 입력만 받으며, A 응답 저장 후 B/C를 실행합니다.

현재 direct Ghidra가 없고 managed-static 이미지 ID도 등록되지 않아 정적 추출의 추가 선행 조건도 충족하지 못합니다. 원래 자료를 복구하거나 이 계약을 만족하는 별도 승인 자료와 정적 환경을 준비하기 전에는 과거 3쌍·20입력 수치를 현 checkout에서 재현했다고 할 수 없습니다. 누락된 할인 샘플을 새 예제로 대체하고 같은 실험으로 부르지 않았습니다.

새 `capstone-20261118-v2`의 source-assisted LLM 조건은 원 C 소스를 받습니다. **기존 A의 Ghidra 정보 조건과 다른 실험**이며 두 결과를 같은 계열로 합산하지 않습니다. 기존 71입력 정확도 하네스의 독립 Docker 기준선도 이 A/B/C 실험과 별도입니다.

- [실제 CLI 호출 영수증](command-receipts.json)
- [원래 입력 계약과 자료 가용성](availability.json)
- [원래 JSON reader의 누락 자료 오류](sample-validation.json)
- [새 24입력 실험](../capstone-20261118-v2/README.md)
