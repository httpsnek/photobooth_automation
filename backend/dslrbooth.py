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

import httpx

from .config import settings

log = logging.getLogger("dslrbooth")


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
        # No feedback channel without Pro — assume ok.
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
