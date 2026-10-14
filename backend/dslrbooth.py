"""dslrBooth trigger, behind one interface.

`hotkey` - emulate a keypress. Works with basic dslrBooth (no Pro).
           Requires the dslrBooth window to be focused/fullscreen on the PC.
           Windows only (pyautogui).
`rest`   - dslrBooth Pro REST API: GET /api/take_photo?mode=start&...

Swap by setting DSLRBOOTH_TRIGGER in .env — the session flow does not change.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from urllib.parse import urlparse

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
        t = (buf.value or "").lower()
        return "dslrbooth" in t or "lumabooth" in t
    except Exception as exc:
        log.debug("foreground check unavailable: %s", exc)
        return True


def _start_url(url: str) -> str:
    """The API's start command is /api/start?mode=print. Early builds of this
    project shipped /api/take_photo in .env — an endpoint LumaBooth does not
    have — so map it here instead of making every booth edit its .env."""
    u = urlparse(url)
    if u.path.rstrip("/").endswith("/take_photo"):
        fixed = u._replace(path=u.path.rstrip("/")[: -len("take_photo")] + "start").geturl()
        log.info("DSLRBOOTH_API_URL %s -> %s", url, fixed)
        return fixed
    return url


class BoothTrigger:
    name = "base"
    last_error: str = ""     # human hint from the most recent failed start()

    async def start(self) -> bool:
        """Fire a photo session. Return True if the command was accepted."""
        raise NotImplementedError

    async def healthy(self) -> bool:
        return True

    def unhealthy_reason(self) -> str:
        return "Фотобудка (dslrBooth) не відповідає"


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

    def unhealthy_reason(self) -> str:
        return (f"dslrBooth не запущено (процес {settings.dslrbooth_proc_names} не знайдено)")


class RestTrigger(BoothTrigger):
    name = "rest"

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(8.0))
        self.url = _start_url(settings.dslrbooth_api_url)

    def _params(self, extra: dict | None = None) -> dict:
        p = {"mode": settings.dslrbooth_api_mode or "print"}
        if settings.dslrbooth_api_password:
            p["password"] = settings.dslrbooth_api_password
        if extra:
            p.update(extra)
        return p

    async def start(self) -> bool:
        try:
            r = await self._client.get(self.url, params=self._params())
        except Exception as exc:
            log.error("dslrBooth REST unreachable: %s", exc)
            self.last_error = "API dslrBooth недоступний — dslrBooth запущено? API увімкнено?"
            return False
        if r.status_code == 200:
            self.last_error = ""
            return True
        log.error("dslrBooth REST %s: %s", r.status_code, r.text[:200])
        if r.status_code in (401, 403):
            self.last_error = "dslrBooth відхилив пароль — перевір DSLRBOOTH_API_PASSWORD у .env"
        else:
            self.last_error = f"dslrBooth відповів HTTP {r.status_code}"
        return False

    def _endpoint(self) -> tuple[str, int]:
        u = urlparse(self.url)
        return (u.hostname or "127.0.0.1"), (u.port or (443 if u.scheme == "https" else 80))

    async def healthy(self) -> bool:
        # A bare TCP connect, never a request to take_photo: an undocumented
        # probe mode on the START command is one firmware quirk away from
        # firing a free session every few seconds.
        host, port = self._endpoint()
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=3)
        except Exception:
            return False
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()
        return True

    def unhealthy_reason(self) -> str:
        host, port = self._endpoint()
        return (f"dslrBooth: API недоступний ({host}:{port}). "
                "Запусти dslrBooth і перевір, що API увімкнено в налаштуваннях")

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
