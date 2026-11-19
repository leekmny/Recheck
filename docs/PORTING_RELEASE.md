# 포팅 검증 및 자동 배포

Windows x64를 실행 배포 대상으로 삼습니다. 포팅 제품 검사는 PR과 `main`에서 저장소가 제공하는 정상·결함 예제를 실제로 비교하고 CLI 조회, JSON·HTML 보고서, GUI 입력과 결과를 확인합니다. 자동 배포는 보호된 `main`에 새 릴리스 버전 요청을 병합하면 시작합니다. [포팅 제품 검사](https://github.com/leekmny/Recheck/actions/workflows/porting-ci.yml), [자동 배포](https://github.com/leekmny/Recheck/actions/workflows/porting-release.yml)

## 릴리스 요청과 버전

PR에서 `recheck/__init__.py`, `pyproject.toml`, `resources/version_info.txt`의 표시·패키지·Windows 버전을 맞춥니다. Windows 고정 버전은 `MAJOR, MINOR, PATCH, 0`을 사용합니다. `.github/porting-release.json`에 같은 버전과 확인할 항목 ID를 적습니다. 예를 들어 다음은 현재 버전이 `0.1.7`일 때의 요청 형식입니다.

```json
{
  "version": "0.1.8",
  "verification": "github-hosted-windows-fixtures",
  "changes": [
    "console-porting",
    "result-reporting",
    "scenario-rerun"
  ]
}
```

`changes`는 아래 ID를 하나 이상 포함하는 목록입니다. 중복이나 등록되지 않은 문구가 있으면 요청이 거부됩니다. 태그를 만들기 전에 각 항목의 `recheck-verification.json` 증거가 성공인지 확인하고, 릴리스 노트에는 정해진 검증 범위와 증거 경로를 넣습니다.

| ID | 검증 범위 | 증거 경로 |
| --- | --- | --- |
| `console-porting` | Windows x64에서 저장소의 정상·결함 콘솔 포팅 예제 비교 | `source.normal`, `source.defect`, `isolated_worker.native` |
| `result-reporting` | 소스와 Windows x64 배포본에서 CLI 결과 조회와 JSON·HTML 보고서 확인 | `source.cli`, `source.json`, `source.html`, `frozen.cli`, `frozen.json`, `frozen.html` |
| `scenario-rerun` | 소스에서 결함 시나리오를 수정본으로 재검사하고 이전 결과 보존 | `source.rerun` |

호환되는 오류 수정은 PATCH, 호환되는 기능 추가는 MINOR, 안정 공개 계약의 호환성 단절은 MAJOR를 올립니다. 기존 공개 정책에 따라 `v0.MINOR.PATCH`는 GitHub 사전 공개로 설정합니다. 최초 안정 공개는 해당 지원 계약을 검증하고 합의한 뒤 `v1.0.0`부터 진행합니다. 안정 공개는 같은 필수 검사를 통과해야 합니다. 태그는 `v` 다음에 버전을 붙입니다. 기존 태그보다 큰 숫자 버전만 발행합니다.

보호된 `main`에 요청 파일이 있고 해당 버전 태그가 없으면 그 push의 정확한 커밋을 검증합니다. 미발행 요청의 검증이 실패하면 수정 사항을 `main`에 병합한 뒤 같은 버전으로 다시 검증합니다. 요청이 없는 커밋은 제품 검사만 실행합니다.

## Windows 검증 방식과 사전 실행

`verification`에 `github-hosted-windows-fixtures`를 지정하면 빌드 작업과 별도의 GitHub 호스팅 `windows-2025` x64 VM에서 저장소에 포함된 C·Rust 정상·결함·대체 예제를 실제로 컴파일하고 실행합니다. x64 MinGW GCC와 Rust 1.96.0의 Windows GNU 대상을 사용합니다. VM에는 SSH 비밀이나 릴리스 쓰기 권한을 부여하지 않습니다. 검증 기록의 `isolated_worker_mode`에 설정한 방식이 표시됩니다. [GitHub 호스팅 Windows 2025 실행기](https://github.com/actions/runner-images/blob/main/images/windows/Windows2025-Readme.md)

같은 저장소에서 `main`으로 여는 PR은 이 방식의 릴리스 후보를 게시 없이 검증합니다. PR의 병합 미리보기 커밋과 대상 브랜치 커밋을 사용하며, 보호된 `main`에 병합되면 실제 push 커밋으로 빌드와 검증을 다시 실행합니다. PR의 검증 결과는 게시에 사용할 수 없습니다. 실패한 호스팅 실행을 다시 시도할 때는 **Re-run all jobs**로 빌드와 검증을 같은 실행 시도에서 다시 수행합니다. [GitHub Actions의 pull_request 이벤트](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#pull_request)

`verification`을 생략하거나 `ssh`로 지정하면 전용 Windows x64 SSH 작업자를 사용합니다. 전용 작업자에 필요한 환경과 자격 증명은 아래 운영 설정을 따릅니다.

## 필수 검증

1. Windows x64 호스팅 실행기에서 잠긴 의존성으로 Ruff, 전체 pytest, 기존 Node 정규화 검사를 실행합니다. 검사 전후 소스 식별자를 비교하고 정확한 Git 커밋을 패키지에 포함합니다.
2. 신뢰된 정상·결함 예제를 소스에서 비교하고 CLI, JSON·HTML 보고서, GUI를 확인합니다.
3. Python 3.14.8 x64와 기존 PyInstaller 경로로 공개용 앱 패키지를 빌드합니다. frozen CLI가 비교 실행 기록과 보고서를 읽는지 확인하고, Windows에서 실제로 `Recheck.exe` GUI를 실행해 기록과 스크린샷을 확인합니다.
4. 실행 전 staging과 최종 인벤토리를 비교합니다. 전체 ZIP 인벤토리와 Windows EXE, 필수 파일, 라이선스·재배포 고지·대응 소스 인벤토리를 대조합니다. Windows 검증 작업은 같은 실행에서 빌드한 자산을 변경 불가능한 아티팩트 ID로 내려받고, ZIP과 풀어낸 EXE의 SHA-256을 확인한 뒤 예제와 패키지 CLI를 실행합니다.
5. 게시 전 ZIP 및 EXE의 SHA-256과 ZIP 내부의 소스 해시 목록을 검증 기록과 해당 커밋의 새 체크아웃에 대조합니다. 호스팅 방식에서는 같은 Actions 실행 및 실행 시도의 호스팅 검증 작업 성공과 빌드·검증 아티팩트 ID도 확인합니다. 대상 커밋은 보호된 `main`의 현재 head여야 하며, 같은 커밋의 `Unit tests`, `Security analysis`, `Porting product checks` push 워크플로와 필수 하위 작업이 모두 성공해야 합니다. `Porting product checks`의 `porting-product`와 `porting-linux-x64`는 각각 성공해야 합니다.
6. 정확한 커밋에 새 태그를 만들고 draft Release에 자산을 올립니다. 각 자산의 이름, 크기, 서버 SHA-256, 다운로드한 실제 바이트의 SHA-256을 대조합니다. 태그와 필수 CI를 다시 확인한 후 게시하며, 게시된 태그와 자산을 다시 조회해 결과를 작업 요약에 남깁니다.

## 저장소 운영 설정

보호된 `main`에 PR 검토와 필수 검사를 적용합니다. `tests`, `CodeQL (python)`, `CodeQL (javascript-typescript)`, `dependencies`, `porting-product`, `porting-linux-x64`를 필수 검사로 지정합니다. 빌드와 Windows 검증 작업에는 저장소 읽기 권한을 부여합니다. 보호된 `main`의 push에서 실행하는 게시 작업만 `contents: write`와 `actions: read` 권한을 사용합니다.

### 전용 SSH 작업자를 선택한 경우

격리된 Windows x64 게스트에 SSH 작업자를 구성합니다. 게스트의 전용 계정은 고정 `recheck-porting-worker` 명령만 실행하며, 호스트 공유 경로나 다른 자격 증명에 접근할 수 없는 환경이어야 합니다. 게스트에서 `RECHECK_WORKER_ISOLATION=dedicated-vm`과 구성의 `worker_id`와 동일한 `RECHECK_WORKER_ID`를 설정합니다. 검증 예제에는 x64 GCC·MinGW와 Rust `x86_64-pc-windows-gnu` 대상이 필요합니다. 후보와 같은 worker 소스 배포본을 사용하며, 관문에서 모듈 해시와 네이티브 아키텍처를 확인합니다. 저장소의 `porting-release-worker` 환경에서 배포 브랜치를 보호된 `main`으로 제한합니다. [GitHub 배포 환경 설정](https://docs.github.com/en/actions/how-tos/deploy/configure-and-manage-deployments/manage-environments)

환경 비밀에 `RECHECK_PORTING_WORKER_JSON`(Windows x64 SSH 작업자 구성), `RECHECK_PORTING_WORKER_SSH_KEY`(해당 게스트 전용 Ed25519 키), `RECHECK_PORTING_WORKER_KNOWN_HOSTS`(별도 경로에서 확인한 호스트 공개 키)를 지정합니다. Windows 호스팅 실행기가 게스트의 SSH 엔드포인트에 도달할 수 있는지 확인합니다. 구성이 완료되면 저장소 Actions 변수 `RECHECK_PORTING_WORKER_READY`를 `true`로 설정합니다. 격리 작업자 증거 또는 적용된 비밀이 없으면 게시 관문에서 실패합니다.

```json
{
  "workers": {
    "default": {
      "kind": "ssh",
      "host": "operator@worker.example",
      "port": 22,
      "worker_id": "windows-x64-release",
      "os": "windows",
      "arch": "x64"
    }
  }
}
```

해당 버전의 태그가 있으면 후보에서 제외합니다. 태그 없는 draft가 있으면 게시 단계에서 충돌로 중단합니다. 실패한 실행의 Actions 요약과 draft 상태를 확인해 별도 운영 판단을 내립니다. 자동화는 기존 태그·draft·릴리스 자산을 삭제하거나 덮어쓰지 않습니다.

## 배포 자산 및 한계

| 파일 | 내용 |
| --- | --- |
| `Recheck-Windows-x64.zip` | `Recheck.exe`, 필수 DLL과 리소스, 사용 안내, 라이선스, 재배포·대응 소스 자료 |
| `Recheck.exe` | ZIP 내부의 동일 파일. 실행할 때는 ZIP 전체를 풀어 사용 |
| `recheck-verification.json` | 버전, 커밋 SHA, 배포 범위, 빌드·검증 결과, ZIP·EXE의 크기와 SHA-256, CI 링크 |
| `SHA256SUMS.txt` | ZIP, EXE, 검증 JSON의 SHA-256 |

Linux·macOS·Windows ARM64, 다른 VM 구성, AI 해석, 오프라인 Docker 이미지는 이 릴리스의 지원 검증 범위 밖입니다. 플랫폼을 추가하려면 해당 플랫폼 배포본의 별도 빌드와 실제 실행, CLI·보고서 검사, 필수 파일과 해시 확인을 게시 전 필수 작업으로 연결해야 합니다.
