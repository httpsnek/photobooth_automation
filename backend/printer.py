"""Windows printer-queue health. No-op (returns '') on non-Windows or when
disabled — so the same code runs on the dev machine.

`printer_problem()` returns a short human reason to stop selling sessions, or ''
when the printer is fine / can't be checked.
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys

from .config import settings

log = logging.getLogger("printer")

# PrinterStatus / job states that mean "don't take money for a print right now"
_BAD_STATUS = {
    "offline", "error", "paperjam", "paperout", "outofpaper", "paperproblem",
    "nopaper", "paused", "dooropen", "notavailable", "serverunknown",
    "userintervention", "outofmemory",
}


async def printer_problem() -> str:
    if not settings.printer_check_enabled or sys.platform != "win32":
        return ""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_check), timeout=12)
    except Exception as exc:
        log.debug("printer check failed: %s", exc)
        return ""  # never block on our own inability to check


def _ps(script: str) -> str:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, timeout=10,
    ).stdout.strip()


def _check() -> str:
    name = settings.printer_name.strip()
    sel = f"-Name '{name}'" if name else ""
    default_hint = "" if name else (
        "if (-not $p) { $p = Get-CimInstance Win32_Printer -Filter 'Default=True' | "
        "Select-Object @{n='Name';e={$_.Name}}, "
        "@{n='PrinterStatus';e={$_.PrinterStatus}} }"
    )
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"$p = Get-Printer {sel} | Select-Object Name,PrinterStatus;"
        f"{default_hint}"
        "$p = $p | Select-Object -First 1;"
        "if (-not $p) { '{}' ; exit }"
        "$jobs = @(Get-PrintJob -PrinterName $p.Name | "
        "  Where-Object { $_.JobStatus -match 'Error|Paused|Blocked|Offline|PaperOut' });"
        "[pscustomobject]@{ name=$p.Name; status=[string]$p.PrinterStatus; "
        "  stuck=$jobs.Count } | ConvertTo-Json -Compress"
    )
    raw = _ps(script)
    if not raw:
        return ""
    try:
        d = json.loads(raw)
    except Exception:
        return ""
    status = str(d.get("status", "")).lower().replace(" ", "").replace("_", "")
    if status in _BAD_STATUS:
        return f"Принтер: {d.get('status')}"
    if int(d.get("stuck", 0) or 0) > 0:
        return "Принтер: завдання друку зависло"
    return ""
