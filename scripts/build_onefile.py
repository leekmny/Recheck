"""Recheck 단일 onefile 실행 파일 빌드 스크립트.

_internal 디렉터리 없이 단일 실행 파일(Recheck.exe)로 패키징하며,
외부 종속성 없이 단독 실행될 수 있도록 QWebEngine 및 필수 리소스를 일체형으로 통합합니다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build_onefile():
    print("=" * 60)
    print("🚀 Recheck 단일 onefile 무설치 실행 파일 빌드 시작")
    print(f"📁 프로젝트 루트: {ROOT}")
    print("=" * 60)

    dist_dir = ROOT / "dist"
    work_dir = ROOT / "build" / "onefile-work"

    dist_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    spec_file = ROOT / "Recheck.spec"
    if not spec_file.is_file():
        raise FileNotFoundError(f"스펙 파일을 찾을 수 없습니다: {spec_file}")

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--workpath",
        str(work_dir),
        "--distpath",
        str(dist_dir),
        str(spec_file),
    ]

    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["RECHECK_STRICT_IDENTITY"] = "0"  # 개발/단독 빌드 모드
    env["RECHECK_ONEFILE"] = "1"
    env["RECHECK_BUNDLE_MODE"] = "onefile"

    start_time = time.time()
    print(f"실행 명령: {' '.join(cmd)}")
    print("빌드 진행 중... (QWebEngine 및 종속 라이브러리 번들링)")

    process = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    for line in iter(process.stdout.readline, ""):
        line_s = line.strip()
        if any(keyword in line_s for keyword in ("INFO: Building", "INFO: Appending", "INFO: Checking", "WARNING:", "ERROR:")):
            print(f"  {line_s}")

    process.stdout.close()
    ret = process.wait()

    elapsed = time.time() - start_time

    if ret != 0:
        print("\n❌ PyInstaller 빌드 실패 (종료 코드:", ret, ")")
        sys.exit(ret)

    exe_path = dist_dir / "Recheck.exe"
    internal_path = dist_dir / "_internal"

    print("\n" + "=" * 60)
    print("✨ 빌드 검증 결과:")
    if internal_path.exists():
        print("⚠️ 경고: _internal 디렉터리가 감지되었습니다. (제거 대상)")
    else:
        print("✅ _internal 디렉터리 없음 (단일 standalone 파일 규격 확인됨)")

    if exe_path.is_file():
        size_mb = exe_path.stat().st_size / (1024 * 1024)
        print(f"✅ 생성된 단일 실행 파일: {exe_path}")
        print(f"📦 파일 크기: {size_mb:.2f} MB")
        print(f"⏱️ 소요 시간: {elapsed:.1f}초")
        print("🎉 빌드가 성공적으로 완료되었습니다!")
        print("이제 외부 종속성이나 추가 폴더 없이 Recheck.exe 파일 하나만으로 즉시 실행 가능합니다.")
    else:
        print(f"❌ 오류: 생성된 실행 파일을 찾을 수 없습니다: {exe_path}")
        sys.exit(1)
    print("=" * 60)


if __name__ == "__main__":
    build_onefile()
