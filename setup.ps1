<#
  InstaBOX kiosk — one-shot installer for a new booth PC (Windows).

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

# ── 1. environment checks ───────────────────────────────────────
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

# ── 2. venv + deps ──────────────────────────────────────────────
Section "Python environment"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
  & $py.Source -m venv .venv
}
& ".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
& ".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet
Write-Host "Dependencies installed."

# ── 3. .env ─────────────────────────────────────────────────────
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
  Ask "SUPPORT_PHONE"            "Support phone (shown on error screens)"     ""
  Ask "PRICE_UAH"               "Price per session, UAH"                     "150"
  Ask "PAYMENT_PROVIDER"        "Payment provider (mock | monobank)"         "mock"
  Ask "BANK_TOKEN"              "Monobank acquiring X-Token (blank for now)" ""
  Ask "TELEGRAM_ALERTS_BOT_TOKEN" "Telegram bot token (blank to skip)"       ""
  Ask "TELEGRAM_ADMIN_CHAT_ID"  "Telegram admin chat id (blank to skip)"     ""
  Ask "DSLRBOOTH_HOTKEY"        "dslrBooth trigger key"                      "space"
  Ask "ADMIN_TOKEN"             "Admin token for /admin (blank to skip)"     ([guid]::NewGuid().ToString('N').Substring(0,12))
} else {
  Write-Host ".env already exists — leaving it untouched."
}

# ── 4. autostart via Scheduled Task ─────────────────────────────
Section "Autostart"
$boothId = (Select-String -Path ".env" -Pattern '^\s*BOOTH_ID\s*=\s*(.+)$').Matches[0].Groups[1].Value.Trim()
$taskName = "InstaBOX-$boothId"
$runBat = Join-Path $PSScriptRoot "run.bat"

schtasks /Query /TN $taskName >$null 2>&1
if ($LASTEXITCODE -eq 0) { schtasks /Delete /TN $taskName /F >$null }

$action  = New-ScheduledTaskAction -Execute $runBat -WorkingDirectory $PSScriptRoot
$trigger = New-ScheduledTaskTrigger -AtLogOn
$set     = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
             -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
             -DisallowHardTerminate:$false -StartWhenAvailable
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $set `
  -RunLevel Highest -Force | Out-Null
Write-Host "Scheduled task '$taskName' registered (starts at logon, auto-restarts)."

# ── done ───────────────────────────────────────────────────────
Section "Done"
Write-Host "Start now:   schtasks /Run /TN $taskName"
Write-Host "Kiosk URL:   http://localhost:$port/kiosk"
if ($ip) { Write-Host "For the tablet (Fully Kiosk):  http://$ip`:$port/kiosk" -ForegroundColor Green }
Write-Host "Owner status page:  http://localhost:$port/status"
Write-Host ""
Write-Host "Next: set the PC to auto-login, put dslrBooth in fullscreen autostart," -ForegroundColor Yellow
Write-Host "      give the PC a static DHCP lease, and point Fully Kiosk at the URL above." -ForegroundColor Yellow
