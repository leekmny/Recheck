# Windows에서 Recheck 처음 실행하기

## 배포 ZIP으로 실행

1. [Windows 배포 ZIP](https://github.com/leekmny/Recheck/releases/download/v0.1.0/Recheck-0.1.0-Windows-x64.zip)을 받습니다. [릴리즈 설명](https://github.com/leekmny/Recheck/releases/tag/v0.1.0)도 확인하세요.
2. ZIP의 압축을 모두 풉니다.
3. `Recheck` 폴더의 **`Recheck.exe`**를 실행합니다. 같은 위치의 `_internal` 폴더도 함께 유지하세요.

앱 실행용 Python은 ZIP에 포함되어 있습니다. Git과 별도의 Python 설치는 소스 개발에만 사용합니다. 다른 PC의 GitHub 로그인이나 AI 연결은 이 PC로 옮겨지지 않으므로 AI 계정은 앱에서 별도로 연결하세요.

## 필요한 도구 설치

시작 화면의 **도구 설치·관리**를 엽니다. **도구 확인**은 설치 위치와 버전만 확인합니다. PATH에 없는 설치도 알려진 설치 경로와 Windows 등록 정보에서 찾습니다.

| 도구 | 필요한 경우 | 준비 방법 |
| --- | --- | --- |
| Docker Desktop | 격리된 분석 환경, Linux 프로그램 실행 비교 | 선택한 도구 설치 → Docker 실행 → Linux 컨테이너 준비 |
| Git | 소스 복제·개발 | 선택 설치. 소스 ZIP과 EXE 실행에는 불필요 |
| Python 3.11 이상 | 소스 실행·개발 | 선택 설치. EXE에는 실행용 Python 포함 |
| Java JDK·Ghidra | 호스트에 직접 설치한 Ghidra를 사용하는 경우 | 선택 설치/공식 다운로드. Docker 분석 환경에는 따로 준비됨 |
| Ollama | 호스트에서 로컬 AI를 사용하는 경우 | 선택 설치. ChatGPT 등 외부 AI 사용에는 불필요 |

**선택한 도구 설치**는 공식 WinGet 패키지를 요청합니다. 설치 관리자가 권한 승인이나 사용 조건 확인을 요구할 수 있습니다. WinGet이 없다면 **설치 관리자 받기** 또는 도구의 **공식 다운로드 안내**를 사용하세요. 재시작이 필요하다는 결과는 설치 완료·사용 가능과 구분하여 표시합니다. Docker 설치만으로 WSL·가상화·엔진 실행이 완료되는 것은 아닙니다. [Docker의 Windows 설치 조건](https://docs.docker.com/desktop/setup/install/windows-install/)을 확인하세요.

## 분석 환경 처음 준비

Docker Desktop을 실행하고 Linux 컨테이너를 사용할 수 있을 때 **분석 도구 내려받아 준비**를 누릅니다. 앱에 포함된 Docker 빌드 자료로 이 PC의 분석 환경을 구성하므로 호스트 Git·Python·Node·Java를 따로 설치할 필요가 없습니다. 처음에는 인터넷, 수 GB의 디스크 공간과 시간이 필요합니다. 실패·중단 시 준비 완료로 표시하지 않으며 다시 시도할 수 있습니다.

기본 준비는 구조 분석용 REA·Ghidra와 프로그램 실행 환경입니다. 로컬 AI도 원하면 **로컬 AI 실행 환경도 준비**를 선택하세요. AI 모델은 별도의 선택으로 내려받으며 배포 ZIP에 모델 가중치는 포함하지 않습니다. 설치·환경 만들기는 명시적으로 선택한 작업에서만 시작합니다.

현재 실행 비교는 Linux x86-64 ELF 콘솔 프로그램을 지원합니다. Windows PE·DLL·.NET 파일의 구조 비교와 실제 Windows 실행 검증은 별개입니다. 지원 범위는 [지원·배포 안내](DISTRIBUTION_AND_RECOVERY.md)를 따릅니다. 새 PC의 깨끗한 설치 검증은 실제 결과를 받기 전 완료로 간주하지 않습니다.

## 소스 ZIP으로 시작한 경우

GitHub의 `Source code` ZIP에는 빌드된 EXE가 없습니다. 바로 앱을 사용하려면 위 Windows 배포 ZIP을 선택하세요. 소스를 실행하거나 수정하려면 압축을 푼 프로젝트의 **`Run-Recheck.cmd`**를 실행합니다. 준비 과정은 기존 Python을 찾아 프로젝트 전용 `.venv`와 잠금 버전의 의존성을 준비합니다. Python이 없다면 설치를 안내합니다. 설치와 인터넷 연결이 제한된 PC에서는 필요한 관리자와 협의해 공식 설치를 진행하세요.

소스 실행에서 오류가 나면 창의 설치 결과·재시작 안내를 확인하고 다시 실행합니다. 소스 ZIP을 실행하는 데 Git은 필수가 아니며 PATH 전체를 덮어쓸 필요도 없습니다.
