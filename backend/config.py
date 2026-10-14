"""Environment-backed configuration — the single place `.env` is read.

Everything that varies between physical booths lives here, so the same code
runs unchanged across a whole fleet.  New machine = new `.env`, nothing else.
"""
from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

BASE_DIR = Path(__file__).resolve().parent.parent

# The client fills .env by hand in Notepad, so the file arrives in whatever
# state Windows leaves it. Each candidate covers a real trap:
#   .env.txt          Explorer hides extensions; "rename to .env" gives this
#   InstaBOX.env.txt  the file we ship, dropped in without renaming at all
_ENV_CANDIDATES = (".env", ".env.txt", "InstaBOX.env.txt")


def _read_env_text(path: Path) -> str:
    raw = path.read_bytes()
    # utf-8-sig strips the BOM older Notepad adds; cp1251 is what Notepad
    # writes when saved as "ANSI" with Cyrillic in BOOTH_NAME. Either way the
    # booth must start rather than die on a UnicodeDecodeError at import.
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _apply(text: str) -> None:
    """Like load_dotenv, but a key written twice keeps its last NON-EMPTY
    value: a client who adds "BANK_TOKEN=..." at the bottom while the empty
    template line is still above must not end up with an empty token."""
    merged: dict[str, str] = {}
    for line in text.splitlines():
        part = dotenv_values(stream=io.StringIO(line))
        for k, v in part.items():
            if v is None:
                continue
            if v.strip() or k not in merged:
                merged[k] = v
    for k, v in merged.items():
        os.environ.setdefault(k, v)   # real environment still wins (tests, ops)


def _load_env() -> Path | None:
    override = os.getenv("INSTABOX_ENV_FILE")
    paths = [Path(override)] if override else [BASE_DIR / n for n in _ENV_CANDIDATES]
    for p in paths:
        if p.is_file():
            try:
                _apply(_read_env_text(p))
                return p
            except Exception:  # a mangled file must not stop the booth booting
                continue
    return None


ENV_FILE = _load_env()

try:
    VERSION = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
except OSError:
    VERSION = "0.0.0"


_FILL_MARK = re.compile(r"\s*#\s*<<<.*$")


def _clean(v: str) -> str:
    # python-dotenv leaves an inline comment on empty assignments (KEY=  # note)
    v = v.strip()
    # "TOKEN=abc123# <<< ЗАПОВНИТИ": the client pasted over the padding, so
    # dotenv kept the marker as part of the value
    v = _FILL_MARK.sub("", v).strip()
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


_NUM = re.compile(r"-?\d+(?:[.,]\d+)?(?:[eE][-+]?\d+)?")
_GROUP_SPACE = re.compile(r"(?<=\d)[\s\u00a0\u2009\u202f']+(?=\d)")
_GROUP_MARK = re.compile(r"(?<=\d)[.,](?=\d{3}(?!\d))")


def _num(name: str, integer: bool = False) -> float | None:
    """The number in a hand-typed value. Ukrainian writes thousands with a
    space ("1 000"), so taking just the first digits would turn a 1000-print
    roll into 1 — and the booth would stop after its first strip."""
    raw = _GROUP_SPACE.sub("", _get(name))
    if integer:
        raw = _GROUP_MARK.sub("", raw)          # "1,000" / "1.000" -> 1000
    m = _NUM.search(raw)
    if not m:
        return None
    try:
        v = float(m.group(0).replace(",", "."))
    except (ValueError, OverflowError):
        return None
    return v if v == v and abs(v) != float("inf") else None   # drop nan/inf


def _int(name: str, default: int, lo: int | None = None, hi: int | None = None) -> int:
    v = _num(name, integer=True)
    n = default if v is None else int(v)
    if lo is not None and n < lo:
        n = default if default >= lo else lo
    if hi is not None and n > hi:
        n = hi
    return n


def _float(name: str, default: float, lo: float | None = None, hi: float | None = None) -> float:
    v = _num(name)
    f = default if v is None else v
    if lo is not None and f < lo:
        f = default if default >= lo else lo
    if hi is not None and f > hi:
        f = hi
    return f


_TRANSLIT = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
    "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
    "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
    "ю": "iu", "я": "ia", "ы": "y", "э": "e", "ё": "e", "ъ": "", "'": "", "’": "",
})


def _slug(v: str, default: str) -> str:
    """BOOTH_ID goes into file names, the Windows task name and the Monobank
    reference — keep it to characters that are safe in all three. A client
    typing 'Кабінка 1' gets 'kabinka-1', not a stripped-down '1'."""
    s = v.strip().lower().translate(_TRANSLIT)
    s = re.sub(r"[^a-z0-9_-]+", "-", s).strip("-")
    return s[:40] or default


def _bool(name: str, default: bool = False) -> bool:
    v = _get(name).lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on", "y", "+", "так", "да"}


@dataclass(frozen=True)
class Settings:
    # ── Identity of this physical booth ──────────────────────────
    booth_id: str = _slug(_get("BOOTH_ID"), "booth-local")
    booth_name: str = _get("BOOTH_NAME", "InstaBOX")
    support_phone: str = _get("SUPPORT_PHONE")
    # a Telegram handle / t.me link — shown as a scannable QR on the error
    # screens (a stranded guest can't tap text on a kiosk). Either or both.
    support_telegram: str = _get_any(["SUPPORT_TELEGRAM", "SUPPORT_TG"])

    # ── Server ──────────────────────────────────────────────────
    host: str = _get("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000, lo=1, hi=65535)
    public_base_url: str = _get("PUBLIC_BASE_URL")
    admin_token: str = _get("ADMIN_TOKEN")

    # ── Pricing ─────────────────────────────────────────────────
    price_uah: int = _int("PRICE_UAH", 150, lo=1)
    currency: str = _get("CURRENCY", "UAH")
    shots: int = _int("SHOTS", 4, lo=1, hi=10)

    # ── Acquirer (Monobank) ─────────────────────────────────────
    # no silent default: "mock" only when somebody actually wrote it
    payment_provider: str = _get("PAYMENT_PROVIDER").strip().lower()
    bank_token: str = _get_any(["BANK_TOKEN", "MONOBANK_TOKEN"])
    merchant_id: str = _get("MERCHANT_ID")
    monobank_api_base: str = _get("MONOBANK_API_BASE", "https://api.monobank.ua")
    invoice_ttl_sec: int = _int("INVOICE_TTL_SEC", 180, lo=30, hi=3600)
    poll_interval_sec: float = _float("POLL_INTERVAL_SEC", 1.5, lo=0.5, hi=30)

    # ── dslrBooth trigger: hotkey | rest ────────────────────────
    dslrbooth_trigger: str = _get("DSLRBOOTH_TRIGGER", "hotkey").strip().lower()
    dslrbooth_hotkey: str = _get("DSLRBOOTH_HOTKEY", "space")
    # LumaBooth / dslrBooth start command: GET /api/start?mode=print&password=…
    dslrbooth_api_url: str = _get_any(
        ["DSLRBOOTH_API_URL", "DSLBOOTH_API_URL", "DSLRBOOTH_REST_URL"],
        "http://localhost:1500/api/start",
    )
    dslrbooth_api_mode: str = _get("DSLRBOOTH_API_MODE", "print").strip().lower()
    dslrbooth_api_password: str = _get("DSLRBOOTH_API_PASSWORD")
    # basic dslrBooth has no feedback — the hotkey trigger checks liveness by
    # confirming the process (and optionally the foreground window) instead.
    dslrbooth_proc_names: str = _get("DSLRBOOTH_PROC_NAMES", "dslrBooth.exe,dslrBooth,LumaBooth.exe,LumaBooth")
    dslrbooth_require_foreground: bool = _bool("DSLRBOOTH_REQUIRE_FOREGROUND", False)

    # ── dslrBooth Trigger events (Pro): screens follow real events, not timers ──
    #  Pro → Settings › General › Triggers → URL:
    #    http://<this-backend>:<PORT>/dslrbooth/event?token=<DSLRBOOTH_EVENT_TOKEN>
    #  dslrBooth appends &event_type=…&param1=…  on every session event.
    dslrbooth_events_enabled: bool = _bool("DSLRBOOTH_EVENTS_ENABLED")
    dslrbooth_event_token: str = _get("DSLRBOOTH_EVENT_TOKEN")
    # generous: a guest deliberating on the retake screen still counts as alive
    booth_watchdog_sec: float = _float("BOOTH_WATCHDOG_SEC", 120, lo=15)  # no event this long ⇒ refund

    # stop selling after this many PAID sessions fail on the booth side in a row
    booth_fail_limit: int = _int("BOOTH_FAIL_LIMIT", 2, lo=1, hi=20)
    booth_fail_cooldown_sec: float = _float("BOOTH_FAIL_COOLDOWN_SEC", 900, lo=60)

    # ── Who owns the touch screen (one monitor, two fullscreen apps) ──
    #  Chrome (our kiosk) holds it except during the shoot, when dslrBooth
    #  comes forward for its own live view / countdown / retake UI.
    window_switch_enabled: bool = _bool("WINDOW_SWITCH_ENABLED", True)
    kiosk_window_title: str = _get("KIOSK_WINDOW_TITLE")   # empty → BOOTH_NAME (the page <title>)
    kiosk_proc_names: str = _get("KIOSK_PROC_NAMES", "chrome.exe,msedge.exe")
    window_raise_retries: int = _int("WINDOW_RAISE_RETRIES", 3, lo=1, hi=10)
    display_min_count: int = _int("DISPLAY_MIN_COUNT", 0, lo=0, hi=8)
    screen_guard_sec: float = _float("SCREEN_GUARD_SEC", 4, lo=1, hi=60)
    # a dslrBooth process younger than this that is on top of an idle booth
    # just (re)started and stole the screen — the guard takes it back
    screen_guard_fresh_sec: float = _float("SCREEN_GUARD_FRESH_SEC", 180, lo=10)  # 0 = off; 1 = require a screen

    # ── Session timing ─────────────────────────────────────────
    #  With events on, these are only a safety fallback. With events off
    #  (basic dslrBooth), they drive the screens — measure once on the real booth.
    session_duration_sec: float = _float("SESSION_DURATION_SEC", 35, lo=0)
    print_duration_sec: float = _float("PRINT_DURATION_SEC", 25, lo=0)
    done_duration_sec: float = _float("DONE_DURATION_SEC", 6, lo=0)
    get_ready_sec: float = _float("GET_READY_SEC", 2.0, lo=0)     # "Дивіться в камеру!" hold before 3·2·1
    shot_countdown_sec: int = _int("SHOT_COUNTDOWN_SEC", 3, lo=0, hi=30)  # the "3·2·1" before each shot
    trigger_retries: int = _int("TRIGGER_RETRIES", 3, lo=1, hi=10)
    trigger_retry_pause_sec: float = _float("TRIGGER_RETRY_PAUSE_SEC", 2.0, lo=0)

    # ── Telegram alerts to the owner ────────────────────────────
    telegram_bot_token: str = _get_any(["TELEGRAM_ALERTS_BOT_TOKEN", "TELEGRAM_BOT_TOKEN"])
    telegram_chat_id: str = _get_any(["TELEGRAM_ADMIN_CHAT_ID", "TELEGRAM_CHAT_ID"])
    daily_summary_hour: int = _int("DAILY_SUMMARY_HOUR", 23, lo=0, hi=23)

    # ── Consumables (thermal paper) + printer health ───────────
    paper_capacity: int = _int("PAPER_CAPACITY", 700, lo=0)   # prints per roll; 0 disables
    paper_warn_prints_left: int = _int("PAPER_WARN_PRINTS_LEFT", 60, lo=0)
    printer_check_enabled: bool = _bool("PRINTER_CHECK_ENABLED", True)  # Windows only
    printer_name: str = _get("PRINTER_NAME")            # empty → the default printer

    # ── Refund resilience (dead-letter queue for money owed) ────
    watch_ttl_sec: float = _float("WATCH_TTL_SEC", 300, lo=0)          # poll an abandoned invoice this long
    refund_retry_base_sec: float = _float("REFUND_RETRY_BASE_SEC", 60, lo=5)
    refund_max_attempts: int = _int("REFUND_MAX_ATTEMPTS", 12, lo=1)
    pending_ops_interval_sec: float = _float("PENDING_OPS_INTERVAL_SEC", 45, lo=5)

    # ── Fleet heartbeat (optional) ─────────────────────────────
    heartbeat_url: str = _get("HEARTBEAT_URL")
    heartbeat_interval_sec: int = _int("HEARTBEAT_INTERVAL_SEC", 300, lo=10)

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

    # ── Derived ───────────────────────────────────────────────
    @property
    def kiosk_title(self) -> str:
        """Title substring identifying the Chrome kiosk window.

        kiosk.html renders <title>{{ booth_name }}</title>, and Chrome puts the
        page title in the window title — so BOOTH_NAME finds it by default.
        """
        return self.kiosk_window_title.strip() or self.booth_name.strip()

    @property
    def support_tg_url(self) -> str:
        """Full https://t.me/… link for the support QR, or ''."""
        v = self.support_telegram.strip().lstrip("@")
        if not v:
            return ""
        if v.startswith(("http://", "https://")):
            return v
        if v.startswith("t.me/"):
            return "https://" + v
        return "https://t.me/" + v

    @property
    def support_tg_handle(self) -> str:
        """@handle for on-screen text, or ''."""
        v = self.support_telegram.strip()
        if not v:
            return ""
        v = v.rsplit("/", 1)[-1].lstrip("@")
        return "@" + v if v else ""

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
        if ENV_FILE is None:
            w.append("no .env file found — running on built-in defaults (test mode, "
                     "no real payments). Put .env next to СТАРТ_ТУТ.bat")
        elif ENV_FILE.name != ".env":
            w.append(f"settings read from '{ENV_FILE.name}' — rename it to '.env'")
        raw_id = _get("BOOTH_ID")
        if raw_id and raw_id != self.booth_id:
            w.append(f"BOOTH_ID '{raw_id}' normalised to '{self.booth_id}' "
                     "(only a-z 0-9 - _ are safe in file and task names)")
        if self.booth_id == "booth-local":
            w.append("BOOTH_ID is default 'booth-local' — set a unique id per booth")
        if self.payment_provider not in ("monobank", "mock"):
            w.append(f"PAYMENT_PROVIDER='{self.payment_provider}' is not 'monobank' or "
                     "'mock' — the booth will NOT sell until this is fixed")
        if self.dslrbooth_trigger not in ("rest", "hotkey"):
            w.append(f"DSLRBOOTH_TRIGGER='{self.dslrbooth_trigger}' is not 'rest' or "
                     "'hotkey' — using hotkey")
        if self.dslrbooth_trigger == "rest" and not self.dslrbooth_api_password:
            w.append("DSLRBOOTH_TRIGGER=rest but DSLRBOOTH_API_PASSWORD is empty — "
                     "dslrBooth will reject the start command")
        if not self.support_phone and not self.support_telegram:
            w.append("no support contact (SUPPORT_PHONE / SUPPORT_TELEGRAM) — "
                     "the error screens give a stranded customer nowhere to turn")
        if self.payment_provider == "monobank" and not self.bank_token:
            w.append("PAYMENT_PROVIDER=monobank but BANK_TOKEN is empty")
        if self.payment_provider == "mock":
            w.append("PAYMENT_PROVIDER=mock — no real money will move")
        if not self.telegram_bot_token or not self.telegram_chat_id:
            w.append("Telegram not configured — owner gets no alerts")
        elif not re.fullmatch(r"-?\d+", self.telegram_chat_id):
            w.append(f"TELEGRAM_ADMIN_CHAT_ID='{self.telegram_chat_id}' must be a number "
                     "(get it from @userinfobot), not a @username — alerts won't arrive")
        if self.fake_trigger_ok:
            w.append("FAKE_TRIGGER_OK=1 — the camera will NOT actually fire")
        if self.dslrbooth_events_enabled and not self.dslrbooth_event_token:
            w.append("DSLRBOOTH_EVENTS_ENABLED=1 without DSLRBOOTH_EVENT_TOKEN — "
                     "/dslrbooth/event accepts unauthenticated calls")
        return w


settings = Settings()
