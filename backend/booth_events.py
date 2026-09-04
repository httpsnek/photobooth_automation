"""Normalises dslrBooth Pro "Trigger" callbacks into controller events.

dslrBooth Pro (Windows) → Settings › General › Triggers → a URL it calls with
HTTP GET on every session event:

    /dslrbooth/event?event_type=session_start&param1=PrintAndGIF
    /dslrbooth/event?event_type=countdown_start&param1=3
    /dslrbooth/event?event_type=capture_start
    /dslrbooth/event?event_type=file_download&param1=20240202_1620_1.jpg
    /dslrbooth/event?event_type=processing_start
    /dslrbooth/event?event_type=printing&param1=…&param2=2
    /dslrbooth/event?event_type=session_end

For a multi-photo (Print) session the per-photo block
(countdown_start → countdown… → capture_start → file_download) repeats once
per shot, then processing_start → printing → session_end.

Docs: https://support.lumasoft.co/en/articles/12831651-triggers-webhooks-and-api
"""
from __future__ import annotations

import logging

log = logging.getLogger("booth-events")

# dslrBooth event_type  ->  (our kind, keeps-the-watchdog-happy)
#   kinds consumed by Controller.on_booth_event:
#     shoot_begin | shot_countdown | shot_capture | shot_saved
#     processing | printing | session_done | booth_error
_MAP = {
    "session_start":    "shoot_begin",
    "countdown_start":  "shot_countdown",
    "capture_start":    "shot_capture",
    "capture":          "shot_capture",
    "file_download":    "shot_saved",
    "processing_start": "processing",
    "printing":         "printing",
    "print":            "printing",
    "session_end":      "session_done",
    # defensive — not in every dslrBooth build, but free to support
    "error":            "booth_error",
    "print_error":      "booth_error",
}

# event_types we knowingly ignore (still refresh the watchdog upstream)
_IGNORED = {"countdown", "sharing_screen", "file_upload", "download_screen",
            "email_screen", "sms_screen", "print_screen"}


class BoothEventRouter:
    def __init__(self, controller) -> None:
        self._controller = controller

    async def handle(self, event_type: str, params: dict[str, str]) -> None:
        event_type = (event_type or "").strip().lower()
        if not event_type:
            log.warning("booth event with no event_type: %s", params)
            return

        kind = _MAP.get(event_type)
        if kind is None:
            if event_type not in _IGNORED:
                log.info("unmapped booth event %r %s", event_type, params)
            # still let the controller bump its watchdog
            await self._controller.on_booth_event("heartbeat", {"event": event_type})
            return

        data: dict = {}
        if kind == "shot_countdown":
            data["seconds"] = _num(params.get("param1"))
        elif kind == "shot_saved":
            data["file"] = params.get("param1", "")
        elif kind == "printing":
            data["copies"] = int(_num(params.get("param2")) or 1)
        elif kind == "booth_error":
            data["detail"] = params.get("param1", "") or event_type

        log.info("booth event %s -> %s %s", event_type, kind, data or "")
        await self._controller.on_booth_event(kind, data)


def _num(v: str | None) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0
