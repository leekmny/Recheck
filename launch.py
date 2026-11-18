import os
import sys
from pathlib import Path


def _optimize_environment():
    """운영체제 및 QWebEngine 단일 onefile 실행 환경 최적화."""
    # 1. UTF-8 인코딩 보장
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"

    # 2. Windows 고해상도(High DPI) 텍스트 및 배율 스케일링 설정 (글자 잘림 방지: PassThrough)
    os.environ["QT_SCALE_FACTOR_ROUNDING_POLICY"] = "PassThrough"
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")

    # 3. Chromium 플래그 최적화 (단일 파일 원파일 패키지 샌드박스 크래시 방지 및 시동 속도 향상)
    chromium_flags = [
        "--no-sandbox",
        "--disable-gpu-sandbox",
        "--disable-logging",
        "--disable-gpu-shader-disk-cache",
        "--disable-dev-shm-usage",
    ]
    existing_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{existing_flags} {' '.join(chromium_flags)}".strip()

    # 4. PyInstaller onefile (_MEIPASS) 환경에서 QtWebEngineProcess 및 자원 경로 자동 감지
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

    # QtWebEngineProcess.exe 후보 경로 탐색
    candidate_processes = [
        base_dir / "PySide6" / "QtWebEngineProcess.exe",
        base_dir / "QtWebEngineProcess.exe",
        base_dir / "bin" / "QtWebEngineProcess.exe",
    ]
    for proc in candidate_processes:
        if proc.is_file():
            os.environ["QTWEBENGINEPROCESS_PATH"] = str(proc)
            break

    # WebEngine 자원 및 로케일 경로 감지
    for res in [base_dir / "PySide6" / "resources", base_dir / "resources"]:
        if (res / "qtwebengine_resources.pak").is_file():
            os.environ["QTWEBENGINE_RESOURCES_PATH"] = str(res)
            break

    for loc in [
        base_dir / "PySide6" / "translations" / "qtwebengine_locales",
        base_dir / "translations" / "qtwebengine_locales",
    ]:
        if loc.is_dir():
            os.environ["QTWEBENGINE_LOCALES_PATH"] = str(loc)
            break


if __name__ == '__main__':
    _optimize_environment()
    if '--cli' in sys.argv[1:]:
        from recheck.cli import main

        args = sys.argv[1:]
        args.remove('--cli')
        raise SystemExit(main(args))
    from recheck.gui import main

    raise SystemExit(main())
