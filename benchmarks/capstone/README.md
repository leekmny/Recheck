# 무해한 6유형 반례 실험

`scripts/run_capstone_benchmark.py`는 기존 프로젝트 소유 C 소스와 이 폴더의 고정 출력 프로그램을 사용합니다. 외부 비공개 할인 샘플은 필요하지 않습니다. 대상은 네트워크 없는 읽기 전용 컨테이너에서 실행합니다.

평가 단위는 **바이너리 쌍 × 등록 입력**입니다. 6유형, 7쌍, 24입력을 한 번씩 비교합니다. 실행 전에 literal stdout/stderr/exit code 정답을 고정합니다. 일치 17개, 불일치 5개, 관측 완료 불가 2개(timeout·미지원 형식)입니다. 등록 입력 밖의 동등성 증명이나 모집단 정확도를 뜻하지 않습니다.

| 유형 | 쌍 | 입력 수 | 고정 정답 |
|---|---|---:|---|
| 동일 바이너리 | p1 | 4 | 일치 4 |
| 동일 소스 O0/O2 | p2 | 4 | 일치 4 |
| 같은 명세, 서로 다른 알고리즘 | p3 | 6 | 일치 6 |
| 유사 구조, 경계 결함 | p4 | 4 | 일치 3, 50에서 불일치 1 |
| 명백히 다른 동작 | p5 | 4 | 불일치 4 |
| 관측 완료 불가 | p6·p7 | 2 | timeout 1, 미지원 형식 1 → 판정유보 |

O0/O2 및 별도 알고리즘의 정답은 등록한 유한 입력 범위와 정의된 C 동작에 한정됩니다. 독립구현은 별도로 작성한 알고리즘이라는 뜻이며 독립 저자·독립 검증을 뜻하지 않습니다. 프롬프트·문턱값 성능 조정은 없고, 별도 held-out 일반화 평가는 하지 않습니다.

| 방법 | 제공 정보 | 실행 방식 |
|---|---|---|
| simple execution | 바이너리·입력, 출력 바이트 직접 비교 | 기존 `DockerAdapter` 격리 실행 |
| LLM static source | 원 C 소스·빌드 플래그·입력 | 로컬 Ollama 예측, 동적 출력 없음 |
| LLM execution replay | 같은 소스·입력 + simple의 실제 관측 | 고정 근거 replay |
| RECHECK | 바이너리·입력 | 실제 `Engine.analyze(analysis_mode='both', ai_mode='off')` |

LLM의 원 소스 접근은 RECHECK보다 유리한 정보 조건입니다. 동일한 디컴파일 입력으로 공정 비교했다고 주장하지 않습니다. replay는 자율 도구 에이전트가 아니며 simple 관측을 재사용하므로 독립 실행 반복이 아닙니다. RECHECK의 결정적 경로와 LLM 예측을 구분합니다. 이 실행만으로 구조 분석의 추가 탐지 효과를 주장할 수 없습니다.

모델 입력에서는 모든 C 주석을 일률적으로 제거하고 문자열/문자 리터럴을 보존합니다. 변환 명칭과 변환한 소스의 SHA-256을 모델 메시지에 기록합니다. 가족 분류·의도·정답·설명은 제공하지 않고 s1..s6/b1..b8 중립 ID만 사용합니다.

이미지·모델은 미리 설치되어 있어야 합니다. 실제 환경에서 확인한 태그를 지정하세요. 모델 다운로드·이미지 재작성·전역 설정 변경은 하지 않습니다.

새 팀원의 별도 기준선 이미지 준비 명령은 [기존 평가 안내](../../samples/accuracy/README.md#이미지가-없는-새-팀원의-준비-경로)에 있습니다. 새로 빌드한 이미지의 결과는 새 실행 ID로 검증합니다. 기존에 설치·승인된 로컬 모델이 없으면 `--model`을 생략하여 모델 조건을 미실행으로 남기고 나머지를 실행할 수 있습니다. 이 준비 문서는 이번 세션에서 새 이미지를 빌드하거나 모델을 설치했다는 뜻은 아닙니다.

```powershell
.venv\Scripts\python scripts\run_capstone_benchmark.py `
  --output verification\capstone-고유ID `
  --public-output benchmarks\results\capstone-고유ID `
  --compiler-image 설치된-컴파일러-이미지 `
  --execution-image 설치된-실행-이미지 `
  --model 설치된-로컬-Ollama-모델 --llm-timeout 120
```

모델 예측은 static/replay 각각 최대 1회, 총 2회이며 검증 실패 재요청은 없습니다. 반복은 1회, temperature=0, seed=17, num_ctx=16384, num_predict=3072입니다. 모델 부재·오류·응답 검증 실패는 원인과 전체 입력의 판정유보로 남깁니다. 판정유보와 FP/FN을 별도로 세고, 불일치 중 판정유보를 포함한 누락도 표시합니다. precision·decided recall·전체 불일치 대비 탐지율·판정유보율은 각각 분자/분모를 기록하고 분모 0이면 값은 null입니다.

`frozen-protocol.json`, SHA-256, 빌드 영수증, 바이너리 식별, 실제 adapter 관측, Engine 결과, 모델 messages/responses, `results.json`을 보존합니다. 공개 사본은 호스트 프로세스·환경·개인 경로를 제거한 **projection**이며 전체 원시 adapter 파일의 복제본이 아닙니다. 대상 stdout/stderr Base64와 모델 원문을 보존합니다. 출력 폴더는 덮어쓰지 않습니다.

simple은 설치된 legacy 이미지의 immutable ID를 직접 사용합니다. RECHECK는 제품의 managed 이미지 검증을 그대로 거칩니다. legacy 실행만 가능하고 제품의 pin 이미지나 정적 도구가 없으면 RECHECK를 차단/판정유보로 보고합니다. 다른 이미지 ID를 검증된 제품 이미지로 가장하지 않습니다.

`--reuse-from 기존실행폴더`는 동일한 frozen protocol·샘플 소스 SHA·바이너리 SHA·등록 입력을 검증하고 simple 관측만 재사용합니다. 두 모델과 Engine은 새 프로세스에서 다시 실행합니다. 재사용은 새 대상 실행으로 세지 않습니다. 최초 관측의 전체 method wall time을 계측하지 못했다면 null이며 대상 duration 합계만 제공하고, peak memory는 미측정으로 남깁니다.

공개된 원문 예측과 정답으로 지표를 다시 집계하려면 저장소 루트의 PowerShell에서 다음을 실행합니다. 모델·Docker 호출이나 파일 변경은 하지 않습니다. 보존한 당시 수치는 덮어쓰지 않고, 이후 추가한 누락예측 계수까지 포함한 `postrun-validation.json`과 대조합니다.

```powershell
@'
import json
from pathlib import Path
from scripts.run_capstone_benchmark import score
folder = Path('benchmarks/results/capstone-20261118-v2')
protocol = json.loads((folder / 'protocol.json').read_text(encoding='utf-8'))
results = json.loads((folder / 'results.json').read_text(encoding='utf-8'))
validation = json.loads((folder / 'postrun-validation.json').read_text(encoding='utf-8'))
cases = [case for pair in protocol['pairs'] for case in pair['cases']]
for name, method in results['methods'].items():
    metrics = score(cases, method['predictions'])
    assert metrics == validation['current_metrics'][name], name
    assert all(metrics[key] == value for key, value in method['metrics'].items()), name
    print(name, json.dumps(metrics, ensure_ascii=False))
'@ | .venv\Scripts\python -
```

2026-11-18 v1의 새 C 소스 주석에 의도 힌트가 포함되어 두 LLM 조건을 exploratory/invalid_blinding으로 제외했습니다. v1 원문은 보존합니다. v2는 주석의 일률 제거라는 누출 수정이며 정답·샘플·입력·문턱값은 바꾸지 않았습니다. 원시 모델 응답을 보고 정확도가 좋아지는 프롬프트를 찾은 실험이 아닙니다.
