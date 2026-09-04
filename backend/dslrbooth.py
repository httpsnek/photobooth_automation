"""dslrBooth trigger, behind one interface.

`hotkey` - emulate a keypress. Works with basic dslrBooth (no Pro).
           Requires the dslrBooth window to be focused/fullscreen on the PC.
           Windows only (pyautogui).
`rest`   - dslrBooth Pro REST API: GET /api/take_photo?mode=start&...

Swap by setting DSLRBOOTH_TRIGGER in .env — the session flow does not change.
"""
from __future__ import annotations

import asyncio
import logging
import sys

import httpx

from .config import settings

log = logging.getLogger("dslrbooth")


def _proc_names() -> set[str]:
    return {n.strip().lower() for n in settings.dslrbooth_proc_names.split(",") if n.strip()}


def _booth_process_running() -> bool:
    """True if a dslrBooth process is running. Best-effort, cross-platform."""
    wanted = _proc_names()
    if not wanted:
        return True
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() in wanted:
                return True
        return False
    except Exception:
        pass
    # psutil missing → OS fallbacks
    try:
        import subprocess
        if sys.platform == "win32":
            out = subprocess.run(
                ["tasklist", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=6,
            ).stdout.lower()
        else:
            out = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                capture_output=True, text=True, timeout=6,
            ).stdout.lower()
        return any(name.split(".")[0] in out for name in wanted)
    except Exception as exc:  # can't check → don't brick the booth
        log.debug("process check unavailable: %s", exc)
        return True


def _booth_window_foreground() -> bool:
    """Windows only: True if the active window looks like dslrBooth."""
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = u32.GetForegroundWindow()
        length = u32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        u32.GetWindowTextW(hwnd, buf, length + 1)
        return "dslrbooth" in (buf.value or "").lower()
    except Exception as exc:
        log.debug("foreground check unavailable: %s", exc)
        return True


class BoothTrigger:
    name = "base"

    async def start(self) -> bool:
        """Fire a photo session. Return True if the command was accepted."""
        raise NotImplementedError

    async def healthy(self) -> bool:
        return True


class HotkeyTrigger(BoothTrigger):
    name = "hotkey"

    async def start(self) -> bool:
        try:
            import pyautogui  # imported lazily; Windows-only dependency
        except Exception as exc:  # pragma: no cover - dev machines without it
            log.error("pyautogui unavailable: %s", exc)
            return False
        try:
            await asyncio.to_thread(pyautogui.press, settings.dslrbooth_hotkey)
            return True
        except Exception as exc:
            log.error("hotkey press failed: %s", exc)
            return False

    async def healthy(self) -> bool:
        # Basic dslrBooth has no feedback channel. A blind keypress that lands
        # nowhere = paid customer, no photos, no refund — so the least we can
        # do is confirm the app is actually running before selling a session.
        if not await asyncio.to_thread(_booth_process_running):
            log.warning("dslrBooth process not found — booth unhealthy")
            return False
        if settings.dslrbooth_require_foreground:
            if not await asyncio.to_thread(_booth_window_foreground):
                log.warning("dslrBooth window not in the foreground — booth unhealthy")
                return False
        return True


class RestTrigger(BoothTrigger):
    name = "rest"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0))

    def _params(self, extra: dict | None = None) -> dict:
        p = {"mode": "start"}
        if settings.dslrbooth_api_password:
            p["password"] = settings.dslrbooth_api_password
        if extra:
            p.update(extra)
        return p

    async def start(self) -> bool:
        try:
            r = await self._client.get(settings.dslrbooth_api_url, params=self._params())
            ok = r.status_code == 200
            if not ok:
                log.error("dslrBooth REST %s: %s", r.status_code, r.text[:200])
            return ok
        except Exception as exc:
            log.error("dslrBooth REST unreachable: %s", exc)
            return False

    async def healthy(self) -> bool:
        try:
            r = await self._client.get(settings.dslrbooth_api_url, params={"mode": "status"})
            return r.status_code < 500
        except Exception:
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


class FakeTrigger(BoothTrigger):
    """Dev only: pretends the booth fired. Enabled by FAKE_TRIGGER_OK=1."""

    name = "fake"

    async def start(self) -> bool:
        log.warning("FakeTrigger: pretending dslrBooth started a session")
        return True


def build_trigger() -> BoothTrigger:
    if settings.fake_trigger_ok:
        return FakeTrigger()
    if settings.dslrbooth_trigger == "rest":
        return RestTrigger()
    return HotkeyTrigger()
