@echo off
chcp 65001 >nul
title InstaBOX - rozgortannya tochky
cd /d "%~dp0"

rem ================================================================
rem  Запуск нової будки в 1 клік.
rem  Розпакуйте архів і запустіть цей файл ВІД ІМЕНІ АДМІНІСТРАТОРА
rem  (правою кнопкою -> "Запуск від імені адміністратора",
rem   або подвійним кліком -> підтвердьте запит UAC).
rem ================================================================

rem ---- перевірка прав адміністратора + авто-перезапуск через UAC ----
net session >nul 2>&1
if not "%errorlevel%"=="0" (
  echo.
  echo   Потрібні права адміністратора - підтвердьте запит Windows...
  echo.
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)
cd /d "%~dp0"

echo ============================================================
echo   InstaBOX  -  розгортання точки
echo ============================================================
echo.

rem ---- крок 1: .env ----
if not exist ".env" (
  if not exist ".env.example" (
    echo [ПОМИЛКА] у папці немає ні .env, ні .env.example
    echo Архів розпаковано не повністю.
    echo.
    pause
    exit /b 1
  )
  copy /y ".env.example" ".env" >nul
  echo Створено .env з шаблону .env.example
) else (
  echo Знайдено наявний .env
)

echo.
echo ------------------------------------------------------------
echo   Зараз відкриється .env у Блокноті.
echo.
echo   Заповніть / перевірте як мінімум:
echo     BOOTH_ID       - унікальний id точки, напр. kiosk_mall_01
echo     BOOTH_NAME     - назва точки
echo     PRICE_UAH      - ціна фотосесії
echo     PRINTER_NAME   - точна назва принтера Windows, порожньо = типовий
echo     SUPPORT_PHONE  - телефон підтримки
echo     BANK_TOKEN, TELEGRAM_ALERTS_BOT_TOKEN, TELEGRAM_ADMIN_CHAT_ID
echo.
echo   Після правок: збережіть файл Ctrl+S і закрийте Блокнот.
echo ------------------------------------------------------------
echo.
pause
start "" /wait notepad.exe "%~dp0.env"

rem ---- крок 2: налаштування системи ----
echo.
echo Налаштування системи: брандмауер, електроживлення, автозапуск...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
if not "%errorlevel%"=="0" (
  echo.
  echo [ПОМИЛКА] setup.ps1 завершився з помилкою, код %errorlevel%.
  echo Усуньте причину і запустіть цей файл ще раз.
  echo.
  pause
  exit /b 1
)

rem ---- крок 3: запуск ----
echo.
echo ============================================================
echo   Систему налаштовано. Що ще треба зробити ВРУЧНУ:
echo     - автоматичний вхід у Windows: netplwiz
echo     - автозапуск dslrBooth у повний екран: shell:startup
echo     - клавіша-тригер у dslrBooth = DSLRBOOTH_HOTKEY у .env
echo     - статичний IP цього ПК на роутері
echo     - Fully Kiosk на планшеті: http://IP-ПК:8000/kiosk
echo.
echo   Повний чек-лист: DEPLOY_CHECKLIST.md
echo ============================================================
echo.
echo Запускаю кіоск. Це вікно можна згорнути; закриття = зупинка.
echo Після перезавантаження ПК кіоск підніметься сам.
echo.
pause
call "%~dp0run.bat"
