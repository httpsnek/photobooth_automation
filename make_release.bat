@echo off
chcp 65001 >nul
title InstaBOX - make release
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "%~dp0make_release.py"
) else (
  python "%~dp0make_release.py"
)
echo.
pause
