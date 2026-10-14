@echo off
chcp 65001 >nul
title InstaBOX - vstanovlennia
cd /d "%~dp0"

rem ================================================================
rem  Встановлення / оновлення точки InstaBOX в 1 клік.
rem  Можна запускати скільки завгодно разів: кожен запуск застосовує
rem  поточний .env і перезапускає все з нуля.
rem ================================================================

rem ---- права адміністратора; перезапуск через UAC ----
rem  шлях передаємо через змінну середовища: апостроф у назві папки
rem  (C:\Users\Мар'яна\...) зламав би рядок у лапках PowerShell
net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo.
  echo   Потрібні права адміністратора - підтвердьте запит Windows...
  set "SELF=%~f0"
  powershell -NoProfile -Command "Start-Process -FilePath $env:SELF -Verb RunAs"
  exit /b
)
cd /d "%~dp0"

echo ============================================================
echo   InstaBOX  -  встановлення точки
echo ============================================================
echo.

rem ---- крок 1: файл налаштувань .env ----
if exist ".env" goto have_env
if exist "InstaBOX.env.txt" (
  copy /y "InstaBOX.env.txt" ".env" >nul
  echo Створено .env з файлу InstaBOX.env.txt
  goto have_env
)
if exist ".env.txt" (
  copy /y ".env.txt" ".env" >nul
  echo Створено .env з файлу .env.txt
  goto have_env
)
if not exist ".env.example" (
  echo [ПОМИЛКА] у папці немає .env.example - архів розпаковано не повністю.
  echo.
  pause
  exit /b 1
)
copy /y ".env.example" ".env" >nul
echo Створено новий .env з шаблону.

:have_env
echo.
echo ------------------------------------------------------------
echo   Зараз відкриється файл налаштувань .env у Блокноті.
echo.
echo   Заповніть рядки з позначкою  ^<^<^< ЗАПОВНИТИ  :
echo     BANK_TOKEN              - токен еквайрингу Monobank
echo     DSLRBOOTH_API_PASSWORD  - пароль API з налаштувань dslrBooth
echo     SUPPORT_TELEGRAM        - Telegram підтримки, напр. @instabox
echo     PAPER_CAPACITY          - скільки відбитків дає рулон
echo.
echo   Дуже бажано також: TELEGRAM_ALERTS_BOT_TOKEN і TELEGRAM_ADMIN_CHAT_ID
echo.
echo   Потім: Ctrl+S, закрити Блокнот - встановлення продовжиться.
echo ------------------------------------------------------------
echo.
pause
start "" /wait notepad.exe "%~dp0.env"

rem ---- крок 2: зупинити попередній запуск ----
rem  інакше старий бекенд зі старими налаштуваннями лишився б працювати,
rem  а правки в .env не застосувались би
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -m backend.envtool stop >nul 2>&1
)

rem ---- крок 3: встановлення та самоперевірка ----
echo.
echo Встановлення: Python, залежності, автозапуск, брандмауер, живлення...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if not "%errorlevel%"=="0" (
  echo.
  echo [ПОМИЛКА] Встановлення зупинилось - причина написана вище червоним.
  echo Виправте її і запустіть цей файл ще раз.
  echo.
  pause
  exit /b 1
)

echo.
echo ============================================================
echo   Подивіться на самоперевірку вище.
echo     [  OK ]  - все добре
echo     [ WARN ] - не критично
echo     [ FAIL ] - треба виправити: відкрийте .env, виправте,
echo                і запустіть СТАРТ_ТУТ.bat ще раз
echo ============================================================
echo.
pause

rem ---- крок 4: запуск, так само як після перезавантаження ПК ----
set "BID="
for /f "usebackq delims=" %%i in (`".venv\Scripts\python.exe" -m backend.envtool booth-id`) do set "BID=%%i"
schtasks /Run /TN "InstaBOX-%BID%" >nul 2>&1
if not "%errorlevel%"=="0" start "InstaBOX" /min "%~dp0run.bat"
schtasks /Run /TN "InstaBOX-watchdog-%BID%" >nul 2>&1
rem  через explorer.exe - щоб Chrome стартував як звичайний користувач, а не
rem  адміністратор: після перезавантаження він має писати у свій профіль
explorer.exe "%~dp0start_kiosk.bat"

echo.
echo   Кіоск запущено. Це вікно можна закрити - точка працює далі
echo   і сама підніметься після перезавантаження ПК.
echo.
echo   Перевірка перемикання вікон - адреса "Window test" вище.
echo.
pause
