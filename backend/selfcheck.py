"""Pre-flight check for a booth. Run:  python -m backend.selfcheck

Verifies the .env, the acquirer token, dslrBooth, Telegram and disk — so a
new install (or a support call) gets a clear pass/fail per subsystem.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

from .config import VERSION, settings

OK, WARN, FAIL = "  OK ", " WARN", " FAIL"


def line(status: str, name: str, detail: str = "") -> tuple[str, str]:
    print(f"[{status}] {name}" + (f"  — {detail}" if detail else ""))
    return status, name


async def check_acquirer() -> None:
    if settings.payment_provider == "mock":
        line(WARN, "acquirer", "PAYMENT_PROVIDER=mock — no real payments")
        return
    if not settings.bank_token:
        line(FAIL, "acquirer", "BANK_TOKEN is empty")
        return
    try:
        async with httpx.AsyncClient(
            base_url=settings.monobank_api_base,
            headers={"X-Token": settings.bank_token}, timeout=10.0,
        ) as c:
            r = await c.get("/api/merchant/details")
            if r.status_code == 200:
                d = r.json()
                line(OK, "acquirer", f"{d.get('merchantName', '?')}")
            elif r.status_code in (401, 403):
                line(FAIL, "acquirer", "token rejected")
            else:
                line(WARN, "acquirer", f"HTTP {r.status_code}: {r.text[:120]}")
    except Exception as exc:
        line(FAIL, "acquirer", f"unreachable: {exc}")


async def check_dslrbooth() -> None:
    if settings.dslrbooth_events_enabled:
        tok = "with token" if settings.dslrbooth_event_token else "NO TOKEN (unauthenticated)"
        line(OK if settings.dslrbooth_event_token else WARN, "dslrbooth events",
             f"on · put /dslrbooth/event {tok} as a Trigger URL in dslrBooth Pro")
    else:
        line(WARN, "dslrbooth events", "off — screens run on the SESSION_DURATION_SEC timers")

    if settings.fake_trigger_ok:
        line(WARN, "dslrbooth", "FAKE_TRIGGER_OK=1 — camera will not fire")
        return
    if settings.dslrbooth_trigger == "rest":
        try:
            async with httpx.AsyncClient(timeout=6.0) as c:
                r = await c.get(settings.dslrbooth_api_url, params={"mode": "status"})
            line(OK if r.status_code < 500 else WARN, "dslrbooth",
                 f"REST HTTP {r.status_code}")
        except Exception as exc:
            line(FAIL, "dslrbooth", f"REST unreachable: {exc}")
    else:
        try:
            import pyautogui  # noqa: F401
            line(OK, "dslrbooth", f"hotkey '{settings.dslrbooth_hotkey}' (pyautogui ok)")
        except Exception:
            if sys.platform != "win32":
                line(WARN, "dslrbooth",
                     "pyautogui not installed — expected off-Windows; it is a "
                     "Windows-only dep and installs on the booth PC")
            else:
                line(FAIL, "dslrbooth",
                     "pyautogui missing — run: .venv\\Scripts\\pip install pyautogui")


async def check_telegram() -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        line(WARN, "telegram", "not configured — owner gets no alerts")
        return
    try:
        async with httpx.AsyncClient(timeout=8.0) as c:
            r = await c.get(
                f"https://api.telegram.org/bot{settings.telegram_bot_token}/getMe"
            )
        if r.status_code == 200 and r.json().get("ok"):
            line(OK, "telegram", "@" + r.json()["result"]["username"])
        else:
            line(FAIL, "telegram", f"HTTP {r.status_code}")
    except Exception as exc:
        line(FAIL, "telegram", f"unreachable: {exc}")


def check_disk() -> None:
    try:
        settings.db_file.parent.mkdir(parents=True, exist_ok=True)
        p = settings.db_file.parent / ".write-test"
        p.write_text("x"); p.unlink()
        line(OK, "disk", str(settings.db_file.parent))
    except Exception as exc:
        line(FAIL, "disk", str(exc))


async def check_printer() -> None:
    from . import printer
    if not settings.printer_check_enabled:
        line(WARN, "printer", "PRINTER_CHECK_ENABLED=0 — no paper-jam / offline detection")
        return
    if sys.platform != "win32":
        line(WARN, "printer", "not Windows — printer status check is a no-op here")
        return
    problem = await printer.printer_problem()
    if problem:
        line(FAIL, "printer", problem)
    else:
        line(OK, "printer", settings.printer_name or "default printer — status normal")


async def check_pending_refunds() -> None:
    from .state import SessionStore
    try:
        store = SessionStore()
        await store.init()
        n = await store.pending_count()
        line(WARN if n else OK, "refund queue",
             f"{n} pending" if n else "empty")
    except Exception as exc:
        line(WARN, "refund queue", f"could not read: {exc}")


def check_watchdog() -> None:
    from .config import BASE_DIR
    if not (BASE_DIR / "watchdog.py").exists():
        line(WARN, "watchdog", "watchdog.py missing — no external process guard")
        return
    if sys.platform == "win32":
        import subprocess
        try:
            out = subprocess.run(["schtasks", "/Query", "/TN",
                                  f"InstaBOX-watchdog-{settings.booth_id}"],
                                 capture_output=True, text=True, timeout=8)
            if out.returncode == 0:
                line(OK, "watchdog", "scheduled task registered")
            else:
                line(WARN, "watchdog", "no scheduled task — re-run setup.ps1 (run.bat still starts it)")
        except Exception as exc:
            line(WARN, "watchdog", f"could not query task: {exc}")
    else:
        line(OK, "watchdog", "present (Windows task check skipped)")


async def main() -> int:
    print(f"InstaBOX selfcheck  ·  v{VERSION}  ·  booth {settings.booth_id} ({settings.booth_name})\n")
    for w in settings.warnings():
        line(WARN, "config", w)
    check_disk()
    await check_acquirer()
    await check_dslrbooth()
    await check_printer()
    await check_pending_refunds()
    check_watchdog()
    await check_telegram()
    print("\nDone. FAIL = fix before going live, WARN = intentional or later.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
