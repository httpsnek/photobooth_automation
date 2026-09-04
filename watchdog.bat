@echo off
rem ── InstaBOX external watchdog — restarts a wedged backend (see watchdog.py) ──
rem   Double-click to run manually, or let run.bat / the Scheduled Task start it.
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "%~dp0watchdog.py" %*
) else (
  python "%~dp0watchdog.py" %*
)
