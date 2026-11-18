# Recheck 비교 실험

기존 71입력 정확도 하네스를 먼저 재실행한 결과는 [355판정 실제 평가](results/accuracy-20261118-standalone-full/README.md)에 있습니다. 원본 managed 경로의 차단과 명시적 standalone 출력 기준선을 구분하고, 대상 실행 426회와 별도 회복 시험 3회를 보존합니다.

공개 샘플만으로 실행한 새 실험은 [무해한 6유형 반례 프로토콜](capstone/README.md)과 [2026-11-18 실제 결과](results/capstone-20261118-v2/README.md)를 확인하세요. 아래의 이전 3방법 실험과 분모·정보 조건이 다릅니다. 새 실험은 LLM 두 조건의 실제 로컬 응답과 RECHECK의 실제 환경 차단까지 보존합니다.

이 하네스는 별도로 준비한 할인 계산기 샘플과 이 폴더의 작은 C 콘솔 프로그램을 비교합니다. 할인 샘플의 실행 파일은 공개 소스에 포함하지 않습니다. `--discount-sample`에 `tests.json`과 `original`·`reimplementation` 실행 파일, 대응하는 두 `.c` 소스를 준비한 폴더를 지정하세요. 실행 파일은 그 폴더 또는 `bin/linux-x64`, 소스는 그 폴더 또는 `src`에 있어야 합니다. 여기의 C 소스는 평가용으로 직접 작성했으며 외부 프로그램을 복제하지 않고 파일·네트워크 접근을 수행하지 않습니다.

* `equivalent_structure`: 구간별 곱셈/분기와 배열·반복 합산으로 같은 배송 요금을 계산합니다.
* `similar_structure`: 유사한 함수 구조에서 요금 구간의 경계 포함 조건을 다르게 구현합니다.

`scripts/run_benchmark.py`는 새 실행 디렉터리에 소스 해시, 빌드 명령·컴파일러 버전·이미지 ID, 실행 파일 해시, 기대 출력과 종료 코드, 등록 테스트를 기록합니다. 기존 할인 샘플은 다시 빌드하거나 덮어쓰지 않습니다. 새 두 쌍은 기존 프로젝트 소유 컴파일러 이미지 `recheck-comparison-build:1`에서 네트워크 없이 빌드합니다.

실험 A는 Ghidra에서 추출한 함수 정보와 입력만 받습니다. 기존 할인 테스트의 설명에 정답이 포함되어 있으므로 **모든 테스트의 description/source 및 기대 정답을 제거**합니다. 원본 C 소스도 제공하지 않습니다. 예측 JSON을 먼저 디스크에 저장한 뒤 B 실행을 시작합니다. 실험 B는 같은 Docker 실행 어댑터의 출력·오류·종료 코드를 직접 비교하고, C는 GUI/CLI와 같은 `Engine.analyze`를 호출합니다. 각 단계의 원문·시간·오류는 실행 폴더에 보존합니다.

```powershell
.venv\Scripts\python scripts\run_benchmark.py --output verification\benchmark-고유실행ID --ghidra '설치경로' --discount-sample '준비한 할인 샘플 폴더'
```

`--output`은 새 폴더여야 합니다. `--prepare-only`로 샘플 작성·빌드 기록만 생성하고, `--resume`으로 같은 실험의 아직 끝나지 않은 단계만 계속할 수 있습니다. 완료된 예측을 수정하거나 과거 결과를 새 결과로 표시하지 않습니다. Ghidra 재사용은 바이너리·추출기 해시, Ghidra/Java 환경, 설정이 동일하고 완료된 출력만 허용합니다.

표본은 3쌍·각 1회입니다. 불일치 검출 수와 함수 후보 연결을 구분합니다. 실행 시간을 수집하지만 프로세스별 최대 메모리는 측정하지 않으므로 미측정으로 표시합니다. 인과관계/조건 식별의 정확성을 자동으로 확정하지 않으며 사람 검토가 필요한 항목은 미검증으로 기록합니다. 이 결과만으로 보편적 성능 우위를 주장할 수 없습니다.

## 수정 전후 평가

초기 실측은 로컬 `verification/benchmark-20261113-115534`에 보존했으며 이 원시 자료는 공개 소스에 포함하지 않습니다. A는 실제 불일치 2개 중 1개를 예측했고 정상 18개 중 오탐 1개, 전체 20개 중 판정불가 2개였습니다. B와 초기 C의 결정적 비교는 모두 불일치 2개를 검출하고 정상 18개를 일치로 판정했습니다. 초기 C의 LLM 구조화 해석은 3쌍 모두 검증에 실패했으며 각 3회 원문을 남겼습니다.

아래 재평가 명령은 위 로컬 원시 자료를 보관한 환경에서만 사용할 수 있습니다. 새 clone에 그 폴더가 없다면 먼저 자신의 초기 평가를 실행하고 해당 폴더를 `--source`로 지정하세요. 최초의 정답 없는 A 예측과 B 관찰을 해시가 있는 기존 근거로 참조하고, 같은 실행 파일·입력·이미지·모델 설정으로 C를 새 실행 ID에서 다시 실행합니다. 초기 실패도 표에 함께 표시합니다.

```powershell
$revisionStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
.venv\Scripts\python scripts\run_benchmark_revision.py --source verification\benchmark-20261113-115534 --output "verification\benchmark-20261113-115534\revisions\prompt-1.3-$revisionStamp"
```

새 평가 폴더의 `results.json`과 `report.html`을 확인하세요. C의 실행 데이터는 최초 평가 폴더의 `recheck-data`에 새 `run_id`로 저장하며, 같은 환경·해시·추출 설정으로 검증된 정적 캐시를 공유합니다. 실패한 해석 응답에서도 `/api/ps`로 확인된 로컬 실행 메타데이터를 보존하므로 모델 실행 성공과 해석 검증 성공을 구분할 수 있습니다.
