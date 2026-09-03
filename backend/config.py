"""Environment-backed configuration — the single place `.env` is read.

Everything that varies between physical booths lives here, so the same code
runs unchanged across a whole fleet.  New machine = new `.env`, nothing else.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

try:
    VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
except OSError:
    VERSION = "0.0.0"


def _clean(v: str) -> str:
    # python-dotenv leaves an inline comment on empty assignments (KEY=  # note)
    v = v.strip()
    if v.startswith("#"):
        return ""
    if " #" in v:
        v = v.split(" #", 1)[0].strip()
    return v


def _get(name: str, default: str = "") -> str:
    return _clean(os.getenv(name, default))


def _get_any(names: list[str], default: str = "") -> str:
    """First non-empty env var from `names` (supports renamed keys)."""
    for n in names:
        v = _clean(os.getenv(n, ""))
        if v:
            return v
    return default


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


def _bool(name: str, default: bool = False) -> bool:
    v = _get(name).lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # ── Identity of this physical booth ──────────────────────────
    booth_id: str = _get("BOOTH_ID", "booth-local")
    booth_name: str = _get("BOOTH_NAME", "InstaBOX")
    support_phone: str = _get("SUPPORT_PHONE")

    # ── Server ──────────────────────────────────────────────────
    host: str = _get("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)
    public_base_url: str = _get("PUBLIC_BASE_URL")
    admin_token: str = _get("ADMIN_TOKEN")

    # ── Pricing ─────────────────────────────────────────────────
    price_uah: int = _int("PRICE_UAH", 150)
    currency: str = _get("CURRENCY", "UAH")
    shots: int = _int("SHOTS", 4)

    # ── Acquirer (Monobank) ─────────────────────────────────────
    payment_provider: str = _get("PAYMENT_PROVIDER", "mock").lower()
    bank_token: str = _get_any(["BANK_TOKEN", "MONOBANK_TOKEN"])
    merchant_id: str = _get("MERCHANT_ID")
    monobank_api_base: str = _get("MONOBANK_API_BASE", "https://api.monobank.ua")
    invoice_ttl_sec: int = _int("INVOICE_TTL_SEC", 180)
    poll_interval_sec: float = _float("POLL_INTERVAL_SEC", 1.5)

    # ── dslrBooth trigger: hotkey | rest ────────────────────────
    dslrbooth_trigger: str = _get("DSLRBOOTH_TRIGGER", "hotkey").lower()
    dslrbooth_hotkey: str = _get("DSLRBOOTH_HOTKEY", "space")
    dslrbooth_api_url: str = _get_any(
        ["DSLRBOOTH_API_URL", "DSLBOOTH_API_URL", "DSLRBOOTH_REST_URL"],
        "http://localhost:1500/api/take_photo",
    )
    dslrbooth_api_password: str = _get("DSLRBOOTH_API_PASSWORD")

    # ── Session timing (measure once on the real booth) ─────────
    session_duration_sec: float = _float("SESSION_DURATION_SEC", 35)
    print_duration_sec: float = _float("PRINT_DURATION_SEC", 25)
    done_duration_sec: float = _float("DONE_DURATION_SEC", 6)
    trigger_retries: int = _int("TRIGGER_RETRIES", 3)
    trigger_retry_pause_sec: float = _float("TRIGGER_RETRY_PAUSE_SEC", 2.0)

    # ── Telegram alerts to the owner ────────────────────────────
    telegram_bot_token: str = _get_any(["TELEGRAM_ALERTS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"])
    telegram_chat_id: str = _get_any(["TELEGRAM_ADMIN_CHAT_ID", "TELEGRAM_CHAT_ID"])
    daily_summary_hour: int = _int("DAILY_SUMMARY_HOUR", 23)

    # ── Consumables (thermal paper) ────────────────────────────
    paper_capacity: int = _int("PAPER_CAPACITY", 700)   # prints per roll; 0 disables
    paper_warn_prints_left: int = _int("PAPER_WARN_PRINTS_LEFT", 60)

    # ── Fleet heartbeat (optional) ─────────────────────────────
    heartbeat_url: str = _get("HEARTBEAT_URL")
    heartbeat_interval_sec: int = _int("HEARTBEAT_INTERVAL_SEC", 300)

    # ── Storage (per-booth, local) ─────────────────────────────
    data_dir: str = _get("DATA_DIR", "data")
    db_path: str = _get("DB_PATH")            # explicit override; else data/{booth_id}.db

    # ── Branded video layer (optional; HTML by default) ────────
    attract_video: str = _get("ATTRACT_VIDEO", "")
    paid_video: str = _get("PAID_VIDEO", "")
    qr_left_pct: float = _float("QR_LEFT_PCT", 50.6)
    qr_top_pct: float = _float("QR_TOP_PCT", 56.46)
    qr_size_pct: float = _float("QR_SIZE_PCT", 52.5)
    qr_height_pct: float = _float("QR_HEIGHT_PCT", 50.5)
    qr_radius_pct: float = _float("QR_RADIUS_PCT", 3.6)

    # ── Dev-only switches (keep off in production) ─────────────
    fake_trigger_ok: bool = _bool("FAKE_TRIGGER_OK")
    mock_fail: bool = _bool("MOCK_FAIL")

    # ── Derived paths ─────────────────────────────────────────
    @property
    def _root(self) -> Path:
        d = Path(self.data_dir)
        return d if d.is_absolute() else BASE_DIR / d

    @property
    def db_file(self) -> Path:
        if self.db_path:
            p = Path(self.db_path)
            return p if p.is_absolute() else BASE_DIR / p
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root / f"{self.booth_id}.db"

    @property
    def log_file(self) -> Path:
        d = BASE_DIR / "logs"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{self.booth_id}.log"

    @property
    def meta_file(self) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        return self._root / f"{self.booth_id}.meta.json"

    def warnings(self) -> list[str]:
        """Non-fatal config problems worth logging loudly at startup."""
        w: list[str] = []
        if self.booth_id == "booth-local":
            w.append("BOOTH_ID is default 'booth-local' — set a unique id per booth")
        if self.payment_provider == "monobank" and not self.bank_token:
            w.append("PAYMENT_PROVIDER=monobank but BANK_TOKEN is empty")
        if self.payment_provider == "mock":
            w.append("PAYMENT_PROVIDER=mock — no real money will move")
        if not self.telegram_bot_token or not self.telegram_chat_id:
            w.append("Telegram not configured — owner gets no alerts")
        if self.fake_trigger_ok:
            w.append("FAKE_TRIGGER_OK=1 — the camera will NOT actually fire")
        return w


settings = Settings()
