<#
  InstaBOX kiosk - one-shot installer for a new booth PC (Windows).

  Run from an elevated PowerShell in the project folder:
      powershell -ExecutionPolicy Bypass -File setup.ps1

  It will:
    1. check Python / port
    2. create .venv and install dependencies
    3. create .env from .env.example and ask for this booth's basics
    4. register a Scheduled Task that starts the kiosk at logon and
       restarts it if it ever dies
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }

# -- 1. environment checks ---------------------------------------
Section "Checking environment"

$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
  Write-Host "Python not found. Install Python 3.10+ from https://python.org (tick 'Add to PATH')." -ForegroundColor Red
  exit 1
}
$ver = & $py.Source --version
Write-Host "Found $ver at $($py.Source)"

$port = 8000
if (Test-Path ".env") {
  $m = Select-String -Path ".env" -Pattern '^\s*PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue
  if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) { Write-Host "Warning: port $port is already in use (pid $($busy.OwningProcess))." -ForegroundColor Yellow }

$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -notlike "169.*" -and $_.IPAddress -ne "127.0.0.1" } |
       Select-Object -First 1).IPAddress

# -- 2. venv + deps ----------------------------------------------
Section "Python environment"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  & $py.Source -m venv .venv
}
& ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
Write-Host "Dependencies installed."

# -- 3. .env -----------------------------------------------------
Section "Booth configuration (.env)"
if (-not (Test-Path ".env")) {
  Copy-Item ".env.example" ".env"
  Write-Host "Created .env from .env.example."

  function Ask($key, $prompt, $default) {
    if ($default) { $p = "$prompt [$default]" } else { $p = $prompt }
    $v = Read-Host $p
    if (-not $v) { $v = $default }
    if ($v -ne $null) {
      $esc = [regex]::Escape($key)
      (Get-Content ".env") -replace "^\s*$esc\s*=.*", "$key=$v" | Set-Content ".env"
    }
  }

  Ask "BOOTH_ID"                  "Booth id (unique, e.g. booth-002)"          "booth-001"
  Ask "BOOTH_NAME"               "Booth name / location"                      "InstaBOX"
  Ask "SUPPORT_PHONE"            "Support phone, blank if none"               ""
  Ask "SUPPORT_TELEGRAM"        "Support Telegram @handle or t.me link"      ""
  Ask "PRICE_UAH"               "Price per session, UAH"                     "150"
  Ask "PAYMENT_PROVIDER"        "Payment provider (mock | monobank)"         "mock"
  Ask "BANK_TOKEN"              "Monobank acquiring X-Token (blank for now)" ""
  Ask "TELEGRAM_ALERTS_BOT_TOKEN" "Telegram bot token (blank to skip)"       ""
  Ask "TELEGRAM_ADMIN_CHAT_ID"  "Telegram admin chat id (blank to skip)"     ""
  Ask "DSLRBOOTH_HOTKEY"        "dslrBooth trigger key"                      "space"
  Ask "ADMIN_TOKEN"             "Admin token for /admin (blank to skip)"     ([guid]::NewGuid().ToString('N').Substring(0,12))
} else {
  Write-Host ".env already exists - leaving it untouched."
}

# nag about anything important that's still blank
$envText = Get-Content ".env" -Raw
if (($envText -notmatch '(?m)^\s*SUPPORT_PHONE\s*=\s*\S') -and ($envText -notmatch '(?m)^\s*SUPPORT_TELEGRAM\s*=\s*\S')) {
  Write-Host "Warning: no SUPPORT_PHONE / SUPPORT_TELEGRAM - stranded customers have nowhere to turn on the error screen." -ForegroundColor Yellow
}
if ($envText -match '(?m)^\s*PAYMENT_PROVIDER\s*=\s*monobank' -and $envText -notmatch '(?m)^\s*BANK_TOKEN\s*=\s*\S') {
  Write-Host "Warning: PAYMENT_PROVIDER=monobank but BANK_TOKEN is empty." -ForegroundColor Yellow
}

# -- 4. autostart via Scheduled Task -----------------------------
Section "Autostart"
$boothId = (Select-String -Path ".env" -Pattern '^\s*BOOTH_ID\s*=\s*(.+)$').Matches[0].Groups[1].Value.Trim()
$taskName = "InstaBOX-$boothId"
$runBat = Join-Path $PSScriptRoot "run.bat"

schtasks /Query /TN $taskName >$null 2>&1
if ($LASTEXITCODE -eq 0) { schtasks /Delete /TN $taskName /F >$null }

$set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
         -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
         -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$trigger = New-ScheduledTaskTrigger -AtLogOn

$action = New-ScheduledTaskAction -Execute $runBat -WorkingDirectory $PSScriptRoot
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $set `
  -RunLevel Highest -Force | Out-Null
Write-Host "Scheduled task '$taskName' registered (starts at logon, auto-restarts)."

# external watchdog - restarts the backend if it wedges (run.bat also starts it;
# a pidfile lock keeps it single). Own task so it survives even if run.bat is killed.
$wdName = "InstaBOX-watchdog-$boothId"
$wdPy   = Join-Path $PSScriptRoot "watchdog.py"
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if ((Test-Path $wdPy) -and (Test-Path $venvPy)) {
  schtasks /Query /TN $wdName >$null 2>&1
  if ($LASTEXITCODE -eq 0) { schtasks /Delete /TN $wdName /F >$null }
  $wdAction = New-ScheduledTaskAction -Execute $venvPy -Argument "`"$wdPy`"" `
                -WorkingDirectory $PSScriptRoot
  Register-ScheduledTask -TaskName $wdName -Action $wdAction -Trigger $trigger -Settings $set `
    -RunLevel Highest -Force | Out-Null
  Write-Host "Scheduled task '$wdName' registered (external process watchdog)."
}

# -- 5. firewall - let the tablet reach the backend -------------
Section "Firewall"
$fwName = "InstaBOX-$port"
netsh advfirewall firewall delete rule "name=$fwName" >$null 2>&1
netsh advfirewall firewall add rule "name=$fwName" dir=in action=allow protocol=TCP "localport=$port" profile=any | Out-Null
Write-Host "Inbound TCP $port allowed as rule '$fwName' (so the tablet can open the kiosk)."

# -- 6. power - a kiosk PC must never sleep or suspend the camera -
Section "Power settings"
try {
  powercfg /change standby-timeout-ac 0
  powercfg /change standby-timeout-dc 0
  powercfg /change hibernate-timeout-ac 0
  powercfg /change hibernate-timeout-dc 0
  powercfg /change monitor-timeout-ac 0
  powercfg /change monitor-timeout-dc 0
  # USB selective suspend OFF (both power schemes) - keeps the camera stable
  powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
  powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
  powercfg /setactive SCHEME_CURRENT
  Write-Host "Sleep / hibernate / monitor-off / USB-selective-suspend disabled."
} catch {
  Write-Host "Could not change power settings automatically - do it in Control Panel > Power Options." -ForegroundColor Yellow
}

# -- done -------------------------------------------------------
Section "Done"
Write-Host "Start now:   schtasks /Run /TN $taskName"
if ((Test-Path $wdPy) -and (Test-Path $venvPy)) { Write-Host "             schtasks /Run /TN $wdName" }
Write-Host "Kiosk URL:   http://localhost:$port/kiosk"
if ($ip) { Write-Host "For the tablet (Fully Kiosk):  http://$ip`:$port/kiosk" -ForegroundColor Green }
Write-Host "Owner status page:  http://localhost:$port/status"
Write-Host ""
Write-Host "Still MANUAL (see DEPLOY.md):" -ForegroundColor Yellow
Write-Host "  * Windows auto-login (netplwiz) - required, task runs -AtLogOn" -ForegroundColor Yellow
Write-Host "  * dslrBooth: fullscreen autostart (shell:startup), trigger key, print-only" -ForegroundColor Yellow
Write-Host "  * pause Windows Update active hours / defer restarts" -ForegroundColor Yellow
Write-Host "  * static DHCP lease for this PC on the router" -ForegroundColor Yellow
Write-Host "  * Fully Kiosk on the tablet -> the URL above" -ForegroundColor Yellow
