# 기존 정확도 하네스 실제 재실행

2026-11-18에 기존 `scripts/evaluate_accuracy.py`의 literal oracle 평가를 실제 실행했습니다. **71등록 입력 × 5비교 유형 = 355판정 전부 측정**, 예정된 차이 19개 검출, 오탐·누락·oracle 오류·미측정은 0건입니다. 실제 정확도 대상 실행은 **426회**, 별도 회복 시험은 **3회**, 합계 **429회**입니다. 모델 요청은 0회입니다.

이 결과는 `--execution-image`를 명시한 **standalone Docker 출력 기준선**입니다. 제품 Engine이나 managed 이미지 검증이 성공했다는 결과가 아닙니다. `execution_backend=standalone_docker_baseline`, `product_managed_execution=false`를 원시 요약과 CSV에 기록했습니다.

| 실행 | 분모 | 측정 | 일치 / 불일치 | 실제 정확도 대상 실행 | 별도 회복 시험 |
|---|---:|---:|---:|---:|---:|
| 원본 managed 경로 | 355 | 0 | 미측정 | 0 | 0 |
| standalone 대표 입력 | 5 | 5 | 4 / 1 | 6 | 3 |
| standalone 기존 전체 평가 | 355 | 355 | 336 / 19 | 426 | 3 |

대표 입력과 전체 평가는 다른 실행 ID입니다. 대표 시험의 6+3회를 전체 평가의 429회에 합산하지 않습니다. 원본 managed 경로는 실제 33개 ELF 빌드에 성공한 뒤 pin 이미지 부재로 차단됐습니다. 그 후 기본 managed 동작을 유지하면서 명시적 standalone 옵션을 추가했습니다. 대표 시험의 oracle·격리 실행·회복 처리를 먼저 확인하고 전체 평가를 실행했습니다.

전체 평가의 TP=19, TN=336, FP=0, FN=0입니다. precision과 이 등록 불일치에 대한 탐지율은 각각 19/19입니다. 이는 6개 소형 프로그램과 변형·등록 입력에 한정된 결과이며, 355개 독립 프로그램의 정확도나 모든 입력의 동등성을 뜻하지 않습니다.

| 프로그램 | 등록 입력 | 판정 수 | literal oracle의 불일치 |
|---|---:|---:|---:|
| arithmetic | 12 | 60 | 1 |
| branch | 12 | 60 | 1 |
| loop | 12 | 60 | 8 |
| strings | 11 | 55 | 2 |
| statistics | 12 | 60 | 2 |
| io | 12 | 60 | 5 |
| 합계 | 71 | 355 | 19 |

5비교 유형은 동일 파일의 별도 실행·O2·대체 알고리즘·의도한 변경·심볼 제거입니다. 원본 관측은 5비교 사이에서 재사용하고 동일 파일의 다른 쪽은 별도로 실행하므로 71×6=426회입니다. 회복 시험은 timeout·출력 초과·자식 프로세스 각 1회입니다. 세 회복 사례 모두 `container_removed=true`, cleanup 오류 0을 확인했으며 정확도 355판정의 분모에 포함하지 않았습니다. 426개 관측의 저장 오류와 adapter snapshot 불일치도 0입니다.

컴파일러는 실제 확인한 GCC 13.3.0의 `recheck-validation3-compiler:20261117` 이미지입니다. 최초 빌드한 33개 ELF를 SHA-256으로 모두 검증한 뒤 대표·전체 실행에 재사용했습니다. 이 중 30개는 정확도용 6프로그램×5빌드이며, 나머지는 회복 시험 1개와 정적 부하용 C++ 2개입니다. MinGW C/C++가 없어 PE 빌드는 0개이며 PE 실행과 정적 분석 품질·AI 설명 정확도는 미측정입니다.

실행 이미지는 실제 설치된 Linux amd64 ID `sha256:7e46ed9ae67f0ddcdd401d3d8129f1b5e1ae201e7517f8e644e40b154270151b`입니다. wrapper의 실행 가능 여부를 먼저 확인했고, 모든 대상 관측의 이미지 ID가 일치했습니다. 제품 pin을 변경하거나 다른 이미지를 제품의 검증 이미지로 가장하지 않았습니다. 코드·바이너리 SHA는 실행 전후 일치했습니다. 전체 method wall time과 peak memory는 독립 계측하지 않았습니다.

## 새 24입력 실험과의 구분

| 항목 | 이 기존 평가 | 새 capstone 평가 |
|---|---|---|
| 분모 | 71입력 × 5변형 = 355판정 | 6유형·7쌍·24 pair-input |
| 방법 | 기존 실행·출력 비교 하네스 | simple, source-assisted LLM, execution replay, 실제 Engine 시도 |
| 모델 요청 | 0 | 정식 2회; 누출 v1 2회 별도 제외 |
| 근거 | 고정 literal oracle, 426개 대상 관측 | 별도 고정 명세·관측·모델 원문 |
| 제품 경로 | managed 원본은 차단, 이 결과는 standalone | 실제 Engine도 환경 부족으로 차단·판정유보 |

두 평가를 합산하지 않습니다. 새 source-assisted LLM은 기존 A/B/C benchmark의 Ghidra 정보만 받는 A와도 다른 조건입니다. 원래 A/B/C는 할인 샘플·원시 snapshot·정적 환경이 없어 이번 checkout에서 재현하지 못했습니다. [실제 가용성 점검](../legacy-abc-20261118-availability/README.md)에 help·prepare-only 호출과 누락 파일 오류를 남겼습니다.

## 실행 명령과 근거

전체 평가에서 실제 사용한 명령은 다음과 같습니다. 재사용한 빌드의 identity 검증은 `provenance.json`에 있습니다.

```powershell
.venv\Scripts\python scripts\evaluate_accuracy.py `
  --output verification\accuracy-20261118-standalone-full `
  --reuse-build --execution-image recheck-analysis-linux:1 --concurrency 3
```

다시 빌드해 독립 실행할 때는 새 출력 ID와 현재 PC에 설치된 이미지를 사용합니다.

```powershell
.venv\Scripts\python scripts\evaluate_accuracy.py `
  --output verification\accuracy-새실행ID `
  --compiler-image recheck-validation3-compiler:20261117 `
  --execution-image recheck-analysis-linux:1 --concurrency 3
```

공개 데이터만으로 정답·출력 바이트·집계를 다시 확인하는 명령입니다.

```powershell
@'
import base64, csv, json
from pathlib import Path
p = Path('benchmarks/results/accuracy-20261118-standalone-full')
rows = list(csv.DictReader((p/'accuracy-results.csv').open(encoding='utf-8-sig')))
oracle = json.loads((p/'frozen-oracle.json').read_text(encoding='utf-8'))['samples']
raw = json.loads((p/'target-observations.json').read_text(encoding='utf-8'))
tp = tn = 0
for row in rows:
    sample, number, variant = row['sample'], int(row['case']), row['variant']
    case = oracle[sample][number]
    expected_left = (case.get('stdout',''), case.get('stderr',''), case.get('exit_code',0))
    expected_right = (case.get('mutant_stdout',case.get('stdout','')) if variant == 'mutant'
                      else case.get('stdout',''), case.get('stderr',''), case.get('exit_code',0))
    def observed(name):
        value = raw[f'observations/{sample}-{number:02d}/{name}/returned-observation.json']['returned']
        assert value['status'] == 'success' and value['infrastructure_ok']
        return tuple(base64.b64decode(value[k+'_b64']).decode('utf-8') for k in ('stdout','stderr')) + (value['exit_code'],)
    left, right = observed('base'), observed(variant)
    assert left == expected_left and right == expected_right
    assert (left == right) == (row['actual_match'] == 'True')
    tp += expected_left != expected_right
    tn += expected_left == expected_right
assert (len(rows), len(raw), tp, tn) == (355, 426, 19, 336)
print('355 verified, TP=19 TN=336 FP=0 FN=0, target observations=426')
'@ | .venv\Scripts\python -
```

공개물은 호스트 process/env·개인 경로를 제거한 projection입니다. 426개 대상의 stdout/stderr Base64·입력·종료 코드와 원래 oracle·CSV·해시는 보존했습니다. 전체 adapter·컨테이너·빌드 원시 기록은 로컬 `verification/accuracy-20261118-standalone-full`에 있습니다.

- [원시 집계의 공개 사본](accuracy-summary.json)
- [355행 비교 결과](accuracy-results.csv)
- [426개 대상 관측과 snapshot 영수증](target-observations.json)
- [실행 전 고정한 oracle](frozen-oracle.json)
- [회복 시험 3건](recovery-results.json)
- [source·이미지·명령·빌드 재사용 provenance](provenance.json)
- [바이너리 identity](binary-identities.json)
- [실제 실행 당시 하네스](harness-at-run.py)
- [원본 managed 경로의 실제 차단](../accuracy-20261118-managed-original/blocked-summary.json)
- [새 24입력 실험](../capstone-20261118-v2/README.md)
