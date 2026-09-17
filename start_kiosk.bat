@echo off
chcp 65001 >nul
title InstaBOX kiosk browser
cd /d "%~dp0"

rem  Вікно кіоску на сенсорному моніторі.
rem  Chrome тримає екран увесь час, крім самої зйомки: на час зйомки бекенд
rem  піднімає поверх нього вікно dslrBooth (live view / відлік / Retake),
rem  а після друку повертає Chrome назад.

set "PORT=8000"
if exist "%~dp0.env" (
  for /f "usebackq tokens=1,* delims==" %%a in ("%~dp0.env") do (
    if /i "%%a"=="PORT" set "PORT=%%b"
  )
)
rem  зрізати можливий інлайн-коментар і пробіли
for /f "tokens=1 delims=# " %%p in ("%PORT%") do set "PORT=%%p"

set "URL=http://localhost:%PORT%/kiosk"

set "CHROME="
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe" set "CHROME=%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%LocalAppData%\Google\Chrome\Application\chrome.exe" set "CHROME=%LocalAppData%\Google\Chrome\Application\chrome.exe"
if not defined CHROME if exist "%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe" set "CHROME=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"

if not defined CHROME (
  echo.
  echo   Chrome не знайдено. Встанови Google Chrome і запусти цей файл ще раз.
  echo.
  pause
  exit /b 1
)

rem  окремий профіль: киоск не воює за вікно зі звичайним Chrome власника
start "" "%CHROME%" --kiosk --app="%URL%" --user-data-dir="%~dp0data\chrome-kiosk" --noerrdialogs --disable-infobars --disable-session-crashed-bubble --disable-pinch --overscroll-history-navigation=0 --check-for-update-interval=31536000
