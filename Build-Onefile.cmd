@echo off
setlocal
chcp 65001 > nul
echo [Recheck] 단일 OneFile 빌드를 시작합니다...
python scripts\build_onefile.py
if errorlevel 1 (
    echo [ERROR] 빌드 중 오류가 발생했습니다.
    pause
    exit /b 1
)
pause
