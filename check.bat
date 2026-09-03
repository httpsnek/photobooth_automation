@echo off
rem Pre-flight check for this booth. Send the output to support if something fails.
cd /d "%~dp0"
".venv\Scripts\python.exe" -m backend.selfcheck
pause
