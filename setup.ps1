<#
  InstaBOX kiosk - one-shot installer for a booth PC (Windows).

  Normally started by START_TUT.bat (the Cyrillic-named file). By hand:
      powershell -ExecutionPolicy Bypass -File setup.ps1     (as administrator)

  Safe to run again at any time: every step replaces what it made before.

  It will:
    1. find a real Python 3.10+
    2. create .venv and install dependencies
    3. prepare .env (and generate the admin / event tokens)
    4. register Scheduled Tasks: backend + watchdog at logon, kiosk browser
    5. open the port on the firewall for the owner's phone
    6. disable sleep / USB suspend, laptop lid = do nothing
    7. run the self-check and print the addresses needed next
#>

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

function Section($t) { Write-Host "`n=== $t ===" -ForegroundColor Cyan }
function Fail($msg) {
  Write-Host ""
  Write-Host $msg -ForegroundColor Red
  Write-Host ""
  exit 1
}
# Windows PowerShell 5.1 turns a native command's redirected stderr into
# error records, and under "Stop" the first one aborts the whole script -
# e.g. on a first install, where "task not found" goes to stderr. Run native
# tools through this so a chatty stderr never kills the installer.
function Native([scriptblock]$block) {
  $old = $ErrorActionPreference
  $ErrorActionPreference = "Continue"
  try { & $block } finally { $ErrorActionPreference = $old }
}

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
          ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) {
  Fail "Run this as administrator (right-click START_TUT.bat or setup.bat -> Run as administrator)."
}

# -- 1. Python ----------------------------------------------------
Section "Checking Python"

$pyExe = $null
foreach ($name in @("py", "python", "python3")) {
  $cmd = Get-Command $name -ErrorAction SilentlyContinue
  if (-not $cmd) { continue }
  # WindowsApps holds both the Microsoft Store stub (prints a hint, exit 9009)
  # and the real aliases of python.org's Python install manager - so run it
  # and judge by the answer instead of by the path. Empty stdin: a runtime
  # prompt must not leave the installer waiting on invisible input.
  $v = Native { $null | & $cmd.Source -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null }
  if ($LASTEXITCODE -ne 0 -or -not $v) { continue }
  $parts = "$v".Trim().Split(".")
  if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) { $pyExe = $cmd.Source; break }
  Write-Host "Skipping Python $v at $($cmd.Source) - need 3.10 or newer." -ForegroundColor Yellow
}
if (-not $pyExe) {
  Fail ("Python 3.10+ not found.`n" +
        "Install it from https://www.python.org/downloads/ and TICK 'Add python.exe to PATH',`n" +
        "then run this installer again.")
}
Write-Host "Using Python $v at $pyExe"

# -- 2. venv + deps -----------------------------------------------
Section "Python environment"
$venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
  Native { & $pyExe -m venv .venv }
  if (-not (Test-Path $venvPy)) { Fail "Could not create .venv with $pyExe." }
}
Write-Host "Installing dependencies (needs internet, ~1-2 min)..."
Native { & $venvPy -m pip install -r requirements.txt --disable-pip-version-check --quiet }
if ($LASTEXITCODE -ne 0) {
  Fail ("Dependency install FAILED (pip exit $LASTEXITCODE).`n" +
        "Check the internet connection and run again. If the internet is fine and Python $v`n" +
        "is very new, install Python 3.12 (Windows installer 64-bit) from python.org instead.")
}
Write-Host "Dependencies installed."

# -- 3. .env ------------------------------------------------------
Section "Booth configuration (.env)"
$envPath = Join-Path $PSScriptRoot ".env"
$utf8 = New-Object System.Text.UTF8Encoding $false

function Set-EnvValue($key, $value) {
  $text = [IO.File]::ReadAllText($envPath, [Text.Encoding]::UTF8)
  $line = "$key=$value"
  $pattern = "(?m)^[ \t]*" + [regex]::Escape($key) + "[ \t]*=[^\r\n]*"
  if ([regex]::IsMatch($text, $pattern)) {
    $text = [regex]::Replace($text, $pattern, [System.Text.RegularExpressions.MatchEvaluator]{ param($m) $line })
  } else {
    $text = $text.TrimEnd() + "`r`n$line`r`n"
  }
  [IO.File]::WriteAllText($envPath, $text, $utf8)
}

if (-not (Test-Path $envPath)) {
  # the file we ship with real values, dropped in without renaming
  $shipped = @("InstaBOX.env.txt", ".env.txt") | Where-Object { Test-Path (Join-Path $PSScriptRoot $_) } | Select-Object -First 1
  if ($shipped) {
    Copy-Item (Join-Path $PSScriptRoot $shipped) $envPath
    Write-Host "Created .env from $shipped."
  } else {
    Copy-Item (Join-Path $PSScriptRoot ".env.example") $envPath
    Write-Host "Created .env from .env.example - answer a few questions (Enter = keep default):"
    function Ask($key, $prompt, $default) {
      if ($default) { $p = "$prompt [$default]" } else { $p = $prompt }
      $v = Read-Host $p
      if (-not $v) { $v = $default }
      if ($v) { Set-EnvValue $key $v.Trim() }
    }
    Ask "BANK_TOKEN"                "Monobank acquiring X-Token"              ""
    Ask "DSLRBOOTH_API_PASSWORD"    "dslrBooth API password"                  ""
    Ask "SUPPORT_TELEGRAM"          "Support Telegram (@username)"            ""
    Ask "TELEGRAM_ALERTS_BOT_TOKEN" "Telegram bot token (blank to skip)"      ""
    Ask "TELEGRAM_ADMIN_CHAT_ID"    "Telegram chat id (blank to skip)"        ""
    Ask "PAPER_CAPACITY"            "Prints per paper roll"                   "400"
    Ask "PRICE_UAH"                 "Price per session, UAH"                  "150"
  }
} else {
  Write-Host ".env found - keeping your values."
}

Native { & $venvPy -m backend.envtool ensure-tokens }
$boothId = (Native { & $venvPy -m backend.envtool booth-id }) | Select-Object -Last 1
if (-not $boothId) { $boothId = "booth-local" }
$boothId = "$boothId".Trim()
$portOut = (Native { & $venvPy -c "from backend.config import settings; print(settings.port)" }) | Select-Object -Last 1
$port = 8000
if ("$portOut".Trim() -match '^\d+$') { $port = [int]"$portOut".Trim() }
Write-Host "Booth id: $boothId   port: $port"

# -- 4. autostart -------------------------------------------------
Section "Autostart"
$runBat = Join-Path $PSScriptRoot "run.bat"
$set = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
         -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
         -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$trigger = New-ScheduledTaskTrigger -AtLogOn

# -Force replaces an existing task of the same name, so re-running is safe
$taskName = "InstaBOX-$boothId"
$action = New-ScheduledTaskAction -Execute $runBat -WorkingDirectory $PSScriptRoot
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $set `
  -RunLevel Highest -Force | Out-Null
Write-Host "Task '$taskName' registered (backend, starts at logon, auto-restarts)."

$wdName = "InstaBOX-watchdog-$boothId"
$wdPy   = Join-Path $PSScriptRoot "watchdog.py"
if (Test-Path $wdPy) {
  $wdAction = New-ScheduledTaskAction -Execute $venvPy -Argument "`"$wdPy`"" -WorkingDirectory $PSScriptRoot
  Register-ScheduledTask -TaskName $wdName -Action $wdAction -Trigger $trigger -Settings $set `
    -RunLevel Highest -Force | Out-Null
  Write-Host "Task '$wdName' registered (external watchdog)."
}

$kioskBat = Join-Path $PSScriptRoot "start_kiosk.bat"
if (Test-Path $kioskBat) {
  try {
    $startup = [Environment]::GetFolderPath("Startup")
    $lnk = Join-Path $startup "InstaBOX Kiosk.lnk"
    $ws = New-Object -ComObject WScript.Shell
    $sc = $ws.CreateShortcut($lnk)
    $sc.TargetPath = $kioskBat
    $sc.WorkingDirectory = $PSScriptRoot
    $sc.WindowStyle = 7
    $sc.Save()
    Write-Host "Kiosk browser starts at logon ($lnk)."
  } catch {
    Write-Host "Could not create the kiosk shortcut - put start_kiosk.bat into shell:startup by hand." -ForegroundColor Yellow
  }
}

# -- 5. firewall --------------------------------------------------
Section "Firewall"
$fwName = "InstaBOX-$port"
try {
  Get-NetFirewallRule -DisplayName $fwName -ErrorAction SilentlyContinue | Remove-NetFirewallRule -ErrorAction SilentlyContinue
  New-NetFirewallRule -DisplayName $fwName -Direction Inbound -Protocol TCP -LocalPort $port `
    -Action Allow -Profile Any | Out-Null
  Write-Host "Inbound TCP $port allowed (owner's phone can open /status)."
} catch {
  Write-Host "Could not add the firewall rule - only the booth PC itself will reach the kiosk." -ForegroundColor Yellow
}

# -- 6. power -----------------------------------------------------
Section "Power settings"
Native {
  powercfg /change standby-timeout-ac 0
  powercfg /change standby-timeout-dc 0
  powercfg /change hibernate-timeout-ac 0
  powercfg /change hibernate-timeout-dc 0
  powercfg /change monitor-timeout-ac 0
  powercfg /change monitor-timeout-dc 0
  # USB selective suspend OFF - keeps the camera connected
  powercfg /setacvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
  powercfg /setdcvalueindex SCHEME_CURRENT 2a737441-1930-4402-8d77-b2bebba308a3 48e6b7a6-50f5-4782-a5d4-53bb8f07e226 0
  # closing the laptop lid = do nothing: the touch monitor becomes the only screen
  powercfg /setacvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
  powercfg /setdcvalueindex SCHEME_CURRENT 4f971e89-eebd-4455-a8de-9e59040e7347 5ca83367-6e45-459f-a27b-476b1d01c936 0
  powercfg /setactive SCHEME_CURRENT
}
Write-Host "Sleep, hibernate, monitor-off and USB suspend disabled; lid close = do nothing."

# -- 7. self-check + what next ------------------------------------
Section "Self-check"
Native { & $venvPy -m backend.selfcheck }

Section "Addresses"
$ip = (Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
       Where-Object { $_.IPAddress -notlike "169.*" -and $_.IPAddress -ne "127.0.0.1" } |
       Select-Object -First 1).IPAddress
Native { & $venvPy -m backend.envtool summary $ip }

Write-Host ""
Write-Host "Still MANUAL:" -ForegroundColor Yellow
Write-Host "  * Windows auto-login (netplwiz) - without it nothing starts after a reboot" -ForegroundColor Yellow
Write-Host "  * dslrBooth: API on, Triggers URL (see above), Live View on, Retake ~5s," -ForegroundColor Yellow
Write-Host "    sharing screens off, 'always on top' OFF, dslrBooth in shell:startup" -ForegroundColor Yellow
