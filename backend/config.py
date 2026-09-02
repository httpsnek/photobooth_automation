"""Environment-backed configuration. One place to read `.env`."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    try:
        return int(float(_get(name) or default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_get(name) or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    host: str = _get("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)
    public_base_url: str = _get("PUBLIC_BASE_URL")

    price_uah: int = _int("PRICE_UAH", 150)
    currency: str = _get("CURRENCY", "UAH")
    shots: int = _int("SHOTS", 4)

    # Branded video layer. Files live in frontend/static/media/.
    # Leave empty to fall back to the plain HTML screens.
    attract_video: str = _get("ATTRACT_VIDEO", "")   # looping "waiting for payment" clip
    paid_video: str = _get("PAID_VIDEO", "")         # "payment received" clip, played once
    # Where the live QR sits over the video, as % of the video box (calibrate once).
    qr_left_pct: float = _float("QR_LEFT_PCT", 50)   # horizontal centre
    qr_top_pct: float = _float("QR_TOP_PCT", 68)     # vertical centre
    qr_size_pct: float = _float("QR_SIZE_PCT", 26)   # width as % of video width

    payment_provider: str = _get("PAYMENT_PROVIDER", "mock").lower()
    monobank_token: str = _get("MONOBANK_TOKEN")
    monobank_api_base: str = _get("MONOBANK_API_BASE", "https://api.monobank.ua")
    invoice_ttl_sec: int = _int("INVOICE_TTL_SEC", 180)
    poll_interval_sec: float = _float("POLL_INTERVAL_SEC", 1.5)

    dslrbooth_trigger: str = _get("DSLRBOOTH_TRIGGER", "hotkey").lower()
    dslrbooth_hotkey: str = _get("DSLRBOOTH_HOTKEY", "space")
    dslrbooth_rest_url: str = _get("DSLRBOOTH_REST_URL", "http://localhost:1500/api/take_photo")
    dslrbooth_api_password: str = _get("DSLRBOOTH_API_PASSWORD")

    session_duration_sec: float = _float("SESSION_DURATION_SEC", 35)
    print_duration_sec: float = _float("PRINT_DURATION_SEC", 25)
    done_duration_sec: float = _float("DONE_DURATION_SEC", 6)

    telegram_bot_token: str = _get("TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = _get("TELEGRAM_CHAT_ID")
    daily_summary_hour: int = _int("DAILY_SUMMARY_HOUR", 23)

    db_path: str = _get("DB_PATH", "photobooth.db")

    # session flow tuning
    trigger_retries: int = _int("TRIGGER_RETRIES", 3)
    trigger_retry_pause_sec: float = _float("TRIGGER_RETRY_PAUSE_SEC", 2.0)

    # dev-only switches (leave empty/0 in production)
    fake_trigger_ok: bool = _get("FAKE_TRIGGER_OK", "0") in {"1", "true", "yes"}
    mock_fail: bool = _get("MOCK_FAIL", "0") in {"1", "true", "yes"}

    @property
    def db_file(self) -> Path:
        p = Path(self.db_path)
        return p if p.is_absolute() else BASE_DIR / p


settings = Settings()
