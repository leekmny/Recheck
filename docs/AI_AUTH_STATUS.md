# Recheck 다중 AI 인증 구현과 검증 상태

**Recheck 0.1.3, 2026-11-16 기준**입니다. 기존 ChatGPT 사용자 로그인과 GPT 추론에 이어 Gemini API 키의 실제 응답·비교 설명을 확인했습니다. 새 사용자는 Gemini 무료 연결로 시작하고 GPT는 추천 선택으로 제공합니다. 기존 사용자의 저장된 선택은 유지합니다.

| 연결 | 실제 확인 범위 |
| --- | --- |
| ChatGPT | 공식 앱 자체 OAuth 로그인·추론 권한·모델 목록·기본 응답·JSON·도구 호출과 왕복·보호된 검사 결과 저장 확인. 실제 할인 예제의 비교·GPT 설명·추가 분석·보고서 확인. |
| Ollama | 이전 배포본에서 gemma4:e4b와 Linux 예제 3종 비교 실측. 이번 변경에서는 기존 경로 회귀 검사. |
| OpenAI API 키 | 구현·모의 HTTP 회귀 검사. 실제 사용자 API 키 추론은 미검증. |
| Claude API | 구현·모의 HTTP 회귀 검사. 실제 키 추론은 미검증. Claude.ai 구독 로그인 미지원. |
| Gemini 3.5 Flash-Lite API 키 | 사용자 제공 키로 모델 목록·기본 응답·JSON·도구 호출 왕복·보호된 검사 결과 저장 확인. ELF 고정3입력의 비교 결과 설명 확인: 2일치·1차이, 입력100에서 원본900/변경본1000 차이를 근거와 대조 |
| Gemini 4 Hydrogen API 키 | 작은 기본 응답과 JSON·도구 왕복 확인. 최초 긴 nonce 기능검사는 출력256토큰에서 길이 초과로 실패하여 증거 저장·기본 선택을 적용하지 않음. 별도 후속 검증 필요 |
| Gemini Google Desktop OAuth | 구현·회귀 검사. 실제 Google OAuth 계정 추론 미검증 |
| OpenAI 호환 서버 | 구현·모의 HTTP 회귀 검사. 실제 개별 서버와 모델은 미검증. |

OAuth는 공식 동적 앱 등록·PKCE·loopback callback·JWT/JWKS와 nonce 검증을 사용합니다. 브라우저 승인은 사용자가 완료했고, 이후 앱 자체 계정 저장과 실제 추론을 확인했습니다. 다른 앱의 로그인 자료를 가져오지 않았습니다.

Gemini 설명의 첫 응답은 Markdown 코드 울타리를 포함해 JSON 검증에서 거절됐습니다. 허용된 업무 요청 예산 안에서 수정 응답을 받아 앱의 설명 스키마·근거 참조·실측 지표 불변 검사를 통과했습니다. 최초 실패와 수정 후 성공을 구분하며 모든 프로그램·입력·모델의 정확도로 일반화하지 않습니다. 깨끗한 다른 PC 설치나 Windows PE 실행 검증도 아닙니다.

무료 등급은 사용자가 프로젝트 화면에서 확인했습니다. API200·모델 목록·응답 성공만으로 무료 결제 상태나 남은 할당량을 독립 증명한 것은 아닙니다. 앱은 유료 프로젝트·모델로 자동 전환하지 않습니다. [Gemini 무료 연결 안내](GEMINI_FREE_SETUP.md)를 따르세요.

Gemini 스트림의 시작 이벤트에 이미 포함된 첫 텍스트가 후속 조립에서 사라지던 결함도 수정했습니다. JSON 앞부분·초기 응답·거절 상태를 보존하며 불완전 응답을 성공으로 바꾸지 않습니다.

실제 서비스 응답으로 두 가지 호환 문제를 재현했습니다. ChatGPT 모델 목록의 `models/slug/visibility` 형식을 읽도록 수정했고, 완료된 Responses 응답의 `incomplete_details: null`을 정상 처리합니다. 숨김 모델·잘못된 목록·불완전 응답은 정상 성공으로 처리하지 않습니다.

설정에는 보안 자료 참조만 저장합니다. Windows 현재 사용자 DPAPI 보호와 프로필·origin·인증 버전 결합을 유지합니다. 도구 경로 또는 PC 자원 설정 변경은 무관한 GPT 검증 자료를 지우지 않습니다. AI 프로필·모델·인증 결합이 달라지면 재사용하지 않습니다.

모델 목록 확인과 응답 생성 검사는 다릅니다. 기능 확인은 별도 최대 4회 요청이며, 비교 업무는 해석과 추가 분석 전체에서 최대 요청 예산을 공유합니다. 자동 HTTP 재전송은 없습니다. 연결 성공이 모든 분석의 성공 또는 AI 설명의 사실성을 보장하지는 않습니다.

공식 자료: [앱 로그인 계약](https://developers.openai.com/siwc/token-sharing-open-source/sign-in), [ChatGPT 모델·추론 계약](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference), [GPT-6.1 Sol](https://developers.openai.com/api/docs/models/gpt-6.1-sol).

Gemini 공식 자료: [Interactions](https://aistudio.google.com/docs/interactions-overview), [구조화 출력](https://aistudio.google.com/docs/structured-output), [함수 호출](https://aistudio.google.com/docs/function-calling), [무료 모델 가격·데이터 조건](https://aistudio.google.com/docs/pricing), [프로젝트별 한도](https://aistudio.google.com/docs/rate-limits).

연결 순서는 [AI 연결 안내](AI_CONNECTIONS.md), 실제 검증 범위와 남은 조건은 같은 폴더의 검증범위.txt 또는 프로젝트의 UX_UPGRADE_20261115.md를 참고하세요.
