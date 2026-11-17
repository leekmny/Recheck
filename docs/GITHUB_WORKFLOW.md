# GitHub에서 Recheck 이어서 개발하기

앱 소스의 기준 버전은 0.1.0입니다. 공개 저장소는 검토한 소스 스냅샷에서 새 이력으로 시작했으며, 새 EXE ZIP은 별도 검증 후 Releases에서 제공합니다. 실제 실행·AI 인증·분석 데이터는 각 작업 PC에서 관리합니다. 이전 개발 기록·태그·실행 파일은 별도의 비공개 저장소에 보존합니다. [공개 이력 구성](PUBLIC_HISTORY.md)을 먼저 읽으세요.

## 저장소와 공동 작업자

소스·테스트·선별 문서는 공개하고 쓰기 권한은 관리자와 초대한 Collaborator에게만 부여합니다. 함께 수정할 사람은 GitHub 저장소의 **Settings → Collaborators**에서 초대합니다. 초대된 사람은 자신의 GitHub 계정으로 저장소를 복제하고, 각자 Python 환경과 외부 분석 도구를 준비합니다.

GitHub의 로그인과 Recheck의 AI 로그인은 별개입니다. API 키·OAuth 토큰·로컬 보안 저장소를 공유하지 않고, 각 작업자가 앱에서 자신의 AI 연결을 설정합니다. Windows 사용자에게 결합된 보호 자료를 다른 PC로 복사해 인증을 이전하지 않습니다. 연결 절차는 [AI 연결 안내](AI_CONNECTIONS.md)를 따릅니다.

## 처음 복제한 PC

앱만 사용하려면 README의 Windows 배포 ZIP을 받아 Recheck.exe를 실행합니다. Git/Python PATH는 필요하지 않습니다. 소스 ZIP은 `Run-Recheck.cmd`로 시작할 수 있습니다. [첫 실행과 도구 설치](FIRST_RUN.md)를 먼저 참고하세요.

GitHub의 **Code**에서 저장소 주소를 복사해 Git 또는 GitHub Desktop으로 복제합니다. 아래 명령은 복제된 Recheck 폴더에서 실행합니다.

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements-lock.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python launch.py
```

프로젝트 메타데이터의 Python 요구 버전은 3.11 이상이며 현재 Windows 배포는 Python 3.14.8로 빌드합니다. 비교 기능에는 Docker와 선택한 AI 연결이 필요합니다. Ghidra·Java·REA는 앱의 분석 환경 준비에서 구성하거나 직접 설치 방식으로 연결할 수 있습니다.

## Releases와 Packages 운영 선택

| 항목 | 용도 | Recheck 적용 |
| --- | --- | --- |
| Releases | 태그별 배포 설명과 다운로드 자산 | Windows EXE ZIP·고지·대응 소스 자료 배포 |
| Packages | npm·NuGet 등 의존성 레지스트리와 Docker/OCI 이미지 | 현재 미사용. 검토한 Docker 이미지 배포가 필요해지면 GHCR 별도 검토 |

`No packages published`는 Packages에 게시한 항목이 없다는 안내이며 Releases의 ZIP과 독립적입니다. 사용자는 ZIP을 풀고 EXE를 실행하므로 이번 배포는 Releases를 사용합니다. Packages를 채우기 위해 같은 ZIP을 올리지 않습니다. Docker 이미지는 당장 공개하지 않고 각 PC에서 앱의 명시적 준비 작업으로 구성합니다. 향후 GHCR을 사용하려면 이미지의 재배포 자료·고정 digest·패키지 공개 설정과 권한을 별도 검증합니다.

프로토타입은 prerelease이므로 README는 `/releases/latest`가 아닌 정확한 태그와 ZIP 링크를 사용합니다. 공식 근거: [Releases](https://docs.github.com/en/repositories/releasing-projects-on-github/about-releases), [Packages](https://docs.github.com/en/packages/learn-github-packages/introduction-to-github-packages), [Container registry](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry), [Latest release 규칙](https://docs.github.com/en/rest/releases/releases#get-the-latest-release).

## 수정부터 push까지

작업을 시작할 때 변경 내용이 없는 상태에서 최신 소스를 받습니다. 수정할 내용별로 브랜치를 만들면 검토와 되돌리기가 쉽습니다.

```powershell
git pull --ff-only
git switch -c fix/example-change
```

필요한 파일을 수정하고 검사합니다. 테스트 성공은 실제 계정 로그인·모든 모델의 응답·Windows EXE 실행 확인과 구분합니다. GUI를 바꿨다면 소스로 실행해 해당 화면을 확인하고, 외부 도구나 AI 경로를 바꿨다면 해당 경로를 따로 확인합니다.

```powershell
.venv\Scripts\python -m pytest -q
git status --short
git diff
```

확인한 파일만 골라 commit합니다. 다음 예제의 파일명과 메시지는 실제 변경 내용으로 바꿉니다.

```powershell
git add README.md docs/GITHUB_WORKFLOW.md
git diff --cached
git commit -m "docs: update development guide"
git push -u origin HEAD
```

GitHub에서 변경 내용을 Pull Request로 열고 검사 결과와 확인한 동작을 적습니다. 다른 사람이 함께 작업한다면 검토 후 기본 브랜치로 병합합니다. 다음 작업은 최신 기본 브랜치에서 새 브랜치를 만듭니다.

## 소스와 로컬 자료 구분

| 저장 위치 | 내용 |
| --- | --- |
| Git 저장소 | Python 소스·리소스·테스트·고정 의존성·선별한 안내 문서·빌드 정의 |
| 각 PC | `.venv/`, `build/`, `dist/`, 캐시, `verification/`, 실제 분석 입력·결과·원시 QA 자료 |
| Recheck 로컬 보안 저장소 | 각 사용자의 API 키·OAuth 토큰·보호된 인증 자료 |
| 공개 GitHub Releases | 향후 재배포 자료 검토·빌드·실행 검증을 마친 새 Windows ZIP. 현재 공개 실행 자산 없음 |

기본 앱 데이터는 `%LOCALAPPDATA%\Recheck`에 저장합니다. `RECHECK_DATA_DIR`로 별도 위치를 지정할 수도 있습니다. 이 데이터를 소스 저장소로 옮겨 공유하지 않습니다. 문제 재현용 자료가 필요하면 인증 정보와 개인 자료를 제거한 작은 예제를 별도로 만듭니다.

`.gitignore`는 추적할 필요 없는 파일을 제외하는 설정입니다. 이미 commit된 파일을 지우거나 인증 정보를 비밀로 바꾸는 기능은 없으므로 `git diff --cached`로 실제 업로드할 내용을 확인합니다.

이 공개 저장소의 과거 태그·릴리즈는 이전 비공개 저장소에서 가져오지 않습니다. 원래 개발 이력이 있는 clone은 공개 저장소에 그대로 push하지 말고 별도의 비공개 보관 원격을 사용하세요. 공개 저장소를 새로 clone하고 필요한 소스 변경만 검토해 반영합니다. `git push --mirror`나 과거 refs·태그의 일괄 push로 두 이력을 합치지 않습니다. 새 공개 커밋의 author·committer에는 GitHub에서 제공하는 자신의 noreply 주소를 설정하고 확인합니다.

## 배포본 만들기

clone·commit·push는 실행 파일을 다시 만들지 않습니다. 자동 테스트 결과만으로 Windows 배포본의 실행을 확인했다고 볼 수도 없습니다.

현재 빌드 진입점은 다음과 같습니다.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build.ps1
```

전체 pytest와 Node 검사를 통과하고 검사 전후 입력 파일이 같을 때 소스 식별자를 갱신합니다. 빌드는 새 staging에 생성하며 현재 사용자 EXE는 교체하지 않습니다. 기본 공개 빌드는 Recheck.exe·실행용 Python/Qt·고지·대응 소스와 선별 문서를 준비하고 Docker TAR는 제외합니다. 실제 Windows EXE와 frozen CLI 검사 후 정식 `dist/Recheck`와 외부 사용자 폴더를 동일 파일로 반영합니다. 실패·중단 시 이전 폴더를 복원하며 ZIP과 전체 inventory도 대조합니다. 정확한 명령·복구 절차·지원 한계는 [배포·복구 안내](DISTRIBUTION_AND_RECOVERY.md)를 따르세요.

## PR·릴리스 설명에 남길 내용

- 사용자가 겪던 문제와 재현 조건, 수정 전후 실제 동작
- 변경한 기능·파일·설정과 변경 이유, 기존 데이터 보존 방식
- 소스 단위 검사, 실제 도구, frozen EXE, VM을 구분한 검증 결과
- 정확도 분모·실패·오탐·누락·AI 설명 검토와 미측정 항목
- EXE/ZIP 해시·크기, 소스 commit·identity, 이미지 ID, 버전
- 설치 전제조건, 지원하지 않는 형식, 실패 시 복구 방법

원격 저장소가 정확히 `leekmny/Recheck`인지와 현재 visibility를 확인합니다. 공개 전환은 별도 사용자 요청에 따른 관리 작업이며 일반 배포 명령이 공개 범위·Collaborator 권한을 변경하지 않습니다. 관리 스크립트는 공개·비공개 모두에서 정확한 저장소 identity와 일관된 visibility를 요구합니다. 개인 계정·키·토큰·보안 저장소·실제 분석 이력·VM·모델은 올리지 않습니다. QA 원본 대신 필요한 설계와 집계·재현 방법만 검토 가능한 문서로 공유합니다.

검증한 변경은 작업 브랜치와 PR로 남기고 정확한 head SHA의 CI 성공을 확인합니다. 병합 시에도 검토한 SHA가 그대로인지 확인합니다. 새 버전 태그와 draft prerelease를 사용하고 이전 태그·첨부 파일을 덮어쓰지 않습니다. [공개 정책](PUBLICATION_SCOPE.md)에 따라 재배포 고지·소스 제공 자료와 실제 실행 검증을 갖춘 새 버전만 서버 ZIP 크기·SHA-256을 로컬과 대조한 뒤 게시합니다. 명령에는 `--branch <검토한 브랜치>`와 `--tag <새 태그>`를 명시하세요. 최종 릴리스 페이지·commit·첨부 자산·CI·본문 링크를 다시 읽어 확인합니다. Source code ZIP은 Windows 실행 배포 ZIP과 다릅니다.
