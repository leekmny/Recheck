# 2026-11-18 실제 측정 결과

6유형·7쌍·24개의 **바이너리 쌍 × 등록 입력**을 평가했습니다. literal oracle은 일치 17개, 불일치 5개, 완료된 관측을 얻을 수 없는 사례 2개(timeout·미지원 형식)입니다. 모든 비율은 이 작은 고정 사례에만 적용됩니다.

| 방법 | TP | TN | FP | FN | 동작 22개 중 판정유보 | 관측 불가 2개 처리 |
|---|---:|---:|---:|---:|---:|---|
| simple execution | 5 | 17 | 0 | 0 | 0 | 유보 2 |
| LLM static source | 5 | 12 | 5 | 0 | 0 | 둘 다 일치로 오확정 |
| LLM execution replay | 5 | 17 | 0 | 0 | 0 | 둘 다 일치로 오확정 |
| RECHECK 실제 Engine | 0 | 0 | 0 | 0 | 22 | 유보 2 |

simple은 실제 대상 실행 시도 46회와 미지원 파일 2건의 실행 거부를 보존했습니다. 22개 동작 입력에서 양쪽 출력·오류·종료 코드가 literal oracle에 모두 맞았습니다. 같은 바이너리 비교도 양쪽을 별도로 실행했습니다. timeout 양쪽 2회는 일치로 세지 않았습니다. v2는 v1의 바이너리·소스·입력·이미지 SHA를 검증한 뒤 이 관측만 재사용했으며 새 대상 실행으로 세지 않습니다.

RECHECK는 새 프로세스에서 실제 `Engine.analyze(both, AI off)`를 7회 호출했습니다. 정적 도구와 pin된 실행 이미지가 없어 모두 static/dynamic blocked였고 대상 실행은 0회입니다. RECHECK의 FP/FN이 0이라는 수치는 성공률을 뜻하지 않습니다. 동작 22개 전부 판정유보이며, 실제 불일치 5개 중 탐지는 0/5입니다. 결정한 불일치 예가 없어서 precision과 decided recall은 null입니다. 구조 점수·추가 탐지 효과는 측정하지 못했습니다. Java 21은 있지만 direct Ghidra가 없고 managed-static 이미지 ID는 미등록, 제품 pin 실행 이미지도 미설치입니다. legacy 이미지를 제품의 검증 이미지로 가장하지 않았습니다.

LLM은 기설치 로컬 `gemma4-12b-uncensored-official:latest`를 각 조건 1회 호출했고 응답과 `/api/ps`의 로컬 실행 확인을 보존했습니다. 고정 설정은 seed=17, temperature=0, num_ctx=16384, num_predict=3072, timeout=120초입니다.

| 방법 | 실제 요청 | 시간 | prompt/output tokens |
|---|---:|---:|---:|
| LLM static source | 1 | 27.44초 | 3678 / 1590 |
| LLM execution replay | 1 | 19.93초 | 6928 / 1229 |
| RECHECK 환경 확인·분석 시도 합계 | AI 0 | 41.74초 | 해당 없음 |

static-source의 FP는 p3-c1·p3-c3·p3-c5·p4-c1·p4-c3입니다. 일부는 reason에서 일치라고 계산하면서 decision을 mismatch로 내어 서로 모순됩니다. 채점은 요청한 JSON `decision`을 그대로 사용했고 자연어를 보고 정답으로 고쳐주지 않았습니다. 두 LLM 모두 p6-c1·p7-c1에서 실패 상태의 동일함을 일치로 잘못 확정했습니다.

LLM은 원 C 소스를 받았지만 RECHECK는 바이너리만 받았습니다. replay는 simple 관측을 제공하는 고정 근거 조건이며 자율 도구 에이전트가 아닙니다. 이 정보 차이와 공유 관측 때문에 동일 도구 예산의 공정한 에이전트 성능 비교나 독립 반복으로 해석하면 안 됩니다. 반복은 1회, 성능 기반 조정은 없고 별도 held-out 검증도 없습니다. simple 전체 method wall time과 모든 방법의 peak memory는 미측정입니다. 보존된 simple 대상 duration의 합계는 20.7042초로 전체 method wall time과 다릅니다.

최초 v1 모델 조건은 source comment의 의도 힌트 때문에 정식 비교에서 제외했습니다. v2는 모든 C 주석을 동일하게 제거하고 문자열 리터럴을 보존했으며 변환 SHA를 기록했습니다. 샘플·정답·입력·바이너리·문턱값은 바꾸지 않았습니다. 이 누출 수정은 모델 성능에 맞춘 조정이 아닙니다.

전체 실제 모델 요청은 v1의 제외된 2회와 v2의 정식 2회로 총 4회입니다. 모델의 cold/warm 상태를 통제하거나 추론 캐시 적중률을 측정하지 않았습니다. 시간 차이에서 캐시 효과나 정보 제공의 인과 효과를 분리할 수 없습니다. 제품 정적 추출은 차단되어 Ghidra 캐시의 효율도 측정하지 못했습니다.

검증한 제품 source hash는 실행 전후 같았습니다. 실험 후 reuse guard와 미응답 집계를 강화했으며, 실제 관측은 모두 완전한 identity를 갖고 유효한 모델 응답도 24개씩이라 기존 실측 수치는 바뀌지 않았습니다. 당시 실행한 runner 원문은 `harness-at-run.py`, 강화한 guard로 모든 실제 근거와 지표를 재검증한 기록은 `postrun-validation.json`입니다.

이 폴더의 JSON은 개인 경로·프로세스/환경 출력 등을 제거한 공개 projection입니다. 대상 출력 Base64, 모델 메시지/원문 응답, 정답, 해시는 보존합니다. 전체 adapter/Engine 원시 기록은 로컬 `verification/capstone-20261118-v1` 및 `verification/capstone-20261118-v2`에 있습니다.

- [상세 결과·모델 원문](results.json)
- [고정 입력·literal oracle·명세](protocol.json)
- [공개 대상 관측](observations.json)
- [source/runner 식별](provenance.json)
- [실험 후 재검증](postrun-validation.json)
- [실행 방법·정보 접근 차이](../../capstone/README.md)
