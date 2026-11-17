# ZIP·패키지 구성 파일 비교

Recheck 0.1.1은 두 ZIP의 **구성 파일 경로·종류·비압축 크기·내용 SHA-256**을 비교합니다. APK·JAR·MSIX·IPA 등도 유효한 ZIP 컨테이너라면 같은 범위에서 검사할 수 있습니다. 확장자만으로 올바른 앱 패키지라고 판정하지 않습니다.

## 화면에서 비교하기

1. **프로그램 비교**에서 **ZIP·패키지 구성 파일 비교**를 선택합니다.
2. 원본 패키지와 변경된 패키지를 고릅니다.
3. **패키지 구성 비교하기**를 누릅니다. 이 모드에는 AI 연결·Docker·실행 입력이 필요하지 않습니다.
4. 결과 창에서 추가·삭제·변경·유지 항목을 확인하고 JSON 또는 HTML로 내보냅니다.

파일을 디스크에 추출하거나 실행하지 않습니다. 중첩 ZIP도 다시 풀지 않고 하나의 바이트 내용으로 비교합니다. 압축 방식·타임스탬프·권한 자체를 차이 지표로 사용하지 않으며 인증서·서명 유효성도 검사하지 않습니다. 내용 해시가 같다는 결과는 프로그램 동작이나 코드 의미가 같다는 판정이 아닙니다.

## CLI에서 비교하기

배포 EXE를 풀어 둔 폴더에서 실행합니다.

```powershell
.\Recheck.exe --cli --json-output package-result.json compare-package --original before.zip --reimplementation after.zip
```

소스에서는 같은 명령을 Python 진입점으로 실행할 수 있습니다.

```powershell
.venv\Scripts\python -m recheck --json-output package-result.json compare-package --original before.zip --reimplementation after.zip
```

`status: success`는 두 전체 목록의 수집·비교 완료를 의미하며 차이가 있어도 성공입니다. 실제 구성 일치 여부는 `comparison.inventory_equal`, 차이 목록은 `added`, `removed`, `changed`, `unchanged`에 있습니다. 실패·부분 수집·취소 결과에는 비교 완료나 일치를 표시하지 않습니다.

## 현재 지원과 상한

| 항목 | 기본 제한 |
| --- | --- |
| ZIP 압축 방식 | STORED 또는 DEFLATE |
| 각 원본 ZIP 파일 크기 | 512 MiB 이하 |
| 각 ZIP의 전체 비압축 내용 합계 | 512 MiB 이하 |
| 개별 구성 파일 비압축 크기 | 128 MiB 이하 |
| 구성 항목 수 | 10,000개 이하 |
| 중앙 디렉터리 크기 | 16 MiB 이하 |
| 경로 이름 | UTF-8 기준 4096바이트 이하 |
| 비압축/압축 크기 비율 | 최대200 |

현재 ZIP64·분할 ZIP·암호화 항목·심볼릭 링크와 특수 항목·위험한 경로·중복/모호한 경로는 지원하지 않습니다. 손상·읽기 중 파일 변경·한도 초과도 명시적 오류나 부분 수집으로 남기며 일치로 계산하지 않습니다. 더 작은 일반 ZIP으로 만들거나 지원되는 입력을 선택하세요.

이번 기능은 패키지 구조 확장의 첫 단계입니다. APK의 클래스·매니페스트 의미 비교, JAR의 Java 메서드 비교, MSIX/IPA의 플랫폼 검증, Electron/JavaScript 모듈 비교와 실제 실행은 별도 구현·검증이 필요합니다. Python ZIP 모듈 자체의 지원 범위보다 Recheck의 입력 제한을 좁게 적용합니다. 형식 설명은 [Python zipfile 공식 문서](https://docs.python.org/3/library/zipfile.html)를 참고하세요.
