#!/usr/bin/env python3
"""InstaBOX external watchdog — a dead-man's switch for the backend.

`run.bat` already restarts the backend when the process *exits*. This catches
the cases where it *doesn't*:
  - the HTTP server stops answering /health, or
  - the FSM wedges in a work state (SHOOTING / PRINTING / PAID / …) and stops
    advancing (the internal recovery in controller.py only fires while the
    asyncio loop is still alive).

When that happens it kills the backend process; `run.bat`'s loop then brings a
fresh one up within ~5 s. `OUT_OF_SERVICE` is a legitimate long hold (no
internet / no paper / dslrBooth down) and is never treated as "stuck".

Kill strategy: find the `uvicorn backend.app` process by command line and kill
it + its children (never this watchdog). Falls back to
`taskkill /F /IM python.exe` on Windows if that finds nothing, or always when
`WATCHDOG_TASKKILL=1`.

Run:      python watchdog.py        (or watchdog.bat)
Tunables: WATCHDOG_* keys in .env (see .env.example) or edit the defaults below.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)


def env(key: str, default: str = "") -> str:
    p = ROOT / ".env"
    if not p.exists():
        return default
    for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() != key:
            continue
        v = v.strip()
        if " #" in v:
            v = v.split(" #", 1)[0].strip()
        return "" if v.startswith("#") else v
    return default


def env_int(key: str, default: int) -> int:
    try:
        return int(float(env(key) or default))
    except ValueError:
        return default


PORT          = env_int("PORT", 8000)
TTL           = env_int("INVOICE_TTL_SEC", 180)
BOOTH_WATCH   = env_int("BOOTH_WATCHDOG_SEC", 90)
BOOTH_ID      = env("BOOTH_ID", "booth")

INTERVAL      = env_int("WATCHDOG_INTERVAL_SEC", 45)
HTTP_TIMEOUT  = env_int("WATCHDOG_TIMEOUT_SEC", 5)
FAIL_LIMIT    = env_int("WATCHDOG_FAIL_LIMIT", 2)
# AWAITING_PAYMENT legitimately lives ~INVOICE_TTL_SEC; SHOOTING/PRINTING up to
# BOOTH_WATCHDOG_SEC. Never flag those before the FSM's own limits.
STUCK_SEC     = max(env_int("WATCHDOG_STUCK_SEC", 180), TTL + 120, BOOTH_WATCH + 120)
COOLDOWN_SEC  = env_int("WATCHDOG_COOLDOWN_SEC", 90)
REBOOT_AFTER  = env_int("WATCHDOG_REBOOT_AFTER_KILLS", 5)     # 0 = never reboot the PC
REBOOT_WINDOW = env_int("WATCHDOG_REBOOT_WINDOW_MIN", 30)
FORCE_TASKKILL = env("WATCHDOG_TASKKILL", "0").lower() in {"1", "true", "yes", "on"}

TG_TOKEN = env("TELEGRAM_ALERTS_BOT_TOKEN") or env("TELEGRAM_BOT_TOKEN")
TG_CHAT  = env("TELEGRAM_ADMIN_CHAT_ID") or env("TELEGRAM_CHAT_ID")

URL = f"http://127.0.0.1:{PORT}/health"
OK_TO_HOLD = {"OUT_OF_SERVICE"}
LOG_FILE = ROOT / "logs" / "watchdog.log"
LOCK_FILE = ROOT / "logs" / "watchdog.lock"


def log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 1_000_000:
            tail = LOG_FILE.read_text(encoding="utf-8", errors="ignore").splitlines()[-400:]
            LOG_FILE.write_text("\n".join(tail) + "\n", encoding="utf-8")
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        data = urllib.parse.urlencode({
            "chat_id": TG_CHAT, "parse_mode": "HTML",
            "text": f"🐕 <b>{BOOTH_ID}</b> · watchdog\n{text}",
        }).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data, timeout=8
        ).read()
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
        return psutil.pid_exists(pid)
    except Exception:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def single_instance() -> bool:
    try:
        if LOCK_FILE.exists():
            old = int((LOCK_FILE.read_text().strip() or "0"))
            if old and old != os.getpid() and _pid_alive(old):
                return False
    except Exception:
        pass
    try:
        LOCK_FILE.parent.mkdir(exist_ok=True)
        LOCK_FILE.write_text(str(os.getpid()))
    except Exception:
        pass
    return True


def kill_backend(reason: str) -> None:
    log(f"KILL — {reason}")
    notify(f"Перезапуск бекенду: {reason}")
    killed = 0
    _PY = ("python", "python.exe", "python3", "pythonw.exe", "python3.exe")
    try:
        import psutil
        me = os.getpid()
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            if p.info["pid"] == me:
                continue
            if (p.info.get("name") or "").lower() not in _PY:
                continue                      # only real python processes, not shells
            cl = " ".join(p.info.get("cmdline") or []).lower()
            if "uvicorn" in cl and "backend.app" in cl and "watchdog" not in cl:
                for c in p.children(recursive=True):
                    try:
                        c.kill()
                    except Exception:
                        pass
                try:
                    p.kill()
                    killed += 1
                except Exception:
                    pass
        log(f"surgical kill: {killed} backend process(es)")
    except Exception as exc:
        log(f"psutil kill unavailable: {exc}")

    if sys.platform == "win32" and (killed == 0 or FORCE_TASKKILL):
        log("taskkill /F /IM python.exe")
        try:
            subprocess.run(["taskkill", "/F", "/IM", "python.exe"],
                           capture_output=True, timeout=15)
        except Exception as exc:
            log(f"taskkill failed: {exc}")

    _register_kill()


_kill_times: list[float] = []


def _register_kill() -> None:
    now = time.time()
    _kill_times.append(now)
    while _kill_times and (now - _kill_times[0]) > REBOOT_WINDOW * 60:
        _kill_times.pop(0)
    if REBOOT_AFTER > 0 and len(_kill_times) >= REBOOT_AFTER:
        log(f"REBOOT — {len(_kill_times)} kills in {REBOOT_WINDOW} min, nothing else worked")
        notify(f"⚠️ {len(_kill_times)} перезапусків за {REBOOT_WINDOW} хв — "
               f"перезавантажую комп'ютер.")
        time.sleep(3)
        if sys.platform == "win32":
            try:
                subprocess.run(["shutdown", "/r", "/t", "20", "/c",
                                "InstaBOX watchdog: recovering"], capture_output=True)
            except Exception:
                pass
        _kill_times.clear()


def probe() -> tuple[str, str] | None:
    """(state, session_id) on a healthy 200, else None."""
    try:
        with urllib.request.urlopen(URL, timeout=HTTP_TIMEOUT) as r:
            if r.status != 200:
                return None
            d = json.loads(r.read().decode())
        return str(d.get("state", "")), str(d.get("session_id", ""))
    except Exception:
        return None


def main() -> None:
    if not single_instance():
        print("watchdog already running — exiting")
        return
    log(f"start · {URL} · every {INTERVAL}s · timeout {HTTP_TIMEOUT}s · "
        f"fail>={FAIL_LIMIT} · stuck>{STUCK_SEC}s · "
        f"reboot after {REBOOT_AFTER}/{REBOOT_WINDOW}min")

    fails = 0
    last_sid: str | None = None
    last_change = time.time()

    while True:
        time.sleep(INTERVAL)
        res = probe()

        if res is None:
            fails += 1
            log(f"no answer from /health ({fails}/{FAIL_LIMIT})")
            if fails >= FAIL_LIMIT:
                kill_backend(f"не відповідає {fails} рази поспіль")
                fails, last_sid, last_change = 0, None, time.time()
                time.sleep(COOLDOWN_SEC)     # let run.bat bring it back
            continue

        fails = 0
        state, sid = res

        if sid != last_sid:                 # FSM advanced — healthy
            last_sid, last_change = sid, time.time()
            continue
        if state in OK_TO_HOLD:             # OUT_OF_SERVICE is a valid wait
            continue

        held = int(time.time() - last_change)
        if held > STUCK_SEC:
            kill_backend(f"завис у стані {state} на {held}с (сесія не змінюється)")
            last_sid, last_change = None, time.time()
            time.sleep(COOLDOWN_SEC)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            if LOCK_FILE.exists() and LOCK_FILE.read_text().strip() == str(os.getpid()):
                LOCK_FILE.unlink()
        except Exception:
            pass
