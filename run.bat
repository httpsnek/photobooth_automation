@echo off
rem ── InstaBOX kiosk launcher — keeps the backend up, restarts on crash ──
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [InstaBOX] .venv not found. Run  powershell -ExecutionPolicy Bypass -File setup.ps1
  pause
  exit /b 1
)

for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
  if /i "%%A"=="PORT" set "PORT=%%B"
)
if not defined PORT set "PORT=8000"

:loop
echo [InstaBOX] starting backend on port %PORT%  (%date% %time%)
".venv\Scripts\python.exe" -m uvicorn backend.app:app --host 0.0.0.0 --port %PORT%
echo [InstaBOX] backend exited (code %errorlevel%). Restarting in 5s...  (%date% %time%)
timeout /t 5 /nobreak >nul
goto loop
