@echo off
rem -- InstaBOX kiosk launcher - keeps the backend up, restarts on crash --
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [InstaBOX] .venv not found. Run  powershell -ExecutionPolicy Bypass -File setup.ps1
  pause
  exit /b 1
)

set "PORT="
if exist ".env" (
  for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
    if /i "%%A"=="PORT" set "PORT=%%B"
  )
)
if not defined PORT set "PORT=8000"
rem  "PORT=8000  # note" would otherwise reach uvicorn as extra arguments
for /f "tokens=1 delims=# " %%P in ("%PORT%") do set "PORT=%%P"

rem -- external watchdog: restarts a wedged backend so this loop can recover it.
rem    Safe to start every time - a pidfile lock keeps it to one instance.
if exist "%~dp0watchdog.py" (
  start "InstaBOX-watchdog" /min "%~dp0watchdog.bat"
)

:loop
echo [InstaBOX] starting backend on port %PORT%  (%date% %time%)
rem  --timeout-graceful-shutdown: an open kiosk keeps an endless SSE stream, and
rem  without a limit uvicorn waits for it forever on Ctrl+C (port + lock stay taken)
".venv\Scripts\python.exe" -m uvicorn backend.app:app --host 0.0.0.0 --port %PORT% --timeout-graceful-shutdown 3
rem  17 = another backend for this booth already holds the lock (the scheduled
rem  task is running and someone started this file again). Looping here would
rem  just retry forever, so step aside.
if "%errorlevel%"=="17" goto already
echo [InstaBOX] backend exited (code %errorlevel%). Restarting in 5s...  (%date% %time%)
timeout /t 5 /nobreak >nul
goto loop

:already
echo [InstaBOX] The backend is already running in another window.
echo [InstaBOX] Nothing to do here - this window will close.
timeout /t 8 /nobreak >nul
exit /b 0
