@echo off
rem Pull the latest code + deps on a booth. Safe to run while it's live —
rem the Scheduled Task restarts the backend on the next crash/reboot,
rem or run:  schtasks /End /TN InstaBOX-<id>  &  schtasks /Run /TN InstaBOX-<id>
cd /d "%~dp0"
git pull --ff-only || (echo git pull failed & pause & exit /b 1)
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
echo Updated to:
type VERSION
echo Restart the InstaBOX scheduled task to apply.
pause
