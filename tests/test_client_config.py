"""What a client can type into .env by hand — none of it may crash the booth.

Each case writes a raw .env (exact bytes, so encodings and BOMs are real) and
loads it in a fresh interpreter, because config is read once at import.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

_PROBE = r"""
import json
from backend import config, notify
from backend.config import settings
from backend.payment import build_provider
from backend.dslrbooth import build_trigger
p = build_provider()
print(json.dumps({
    "env_file": config.ENV_FILE.name if config.ENV_FILE else None,
    "booth_id": settings.booth_id, "booth_name": settings.booth_name,
    "price": settings.price_uah, "shots": settings.shots, "port": settings.port,
    "paper": settings.paper_capacity, "ttl": settings.invoice_ttl_sec,
    "provider": p.name, "reason": getattr(p, "reason", ""),
    "trigger": build_trigger().name, "events": settings.dslrbooth_events_enabled,
    "window": settings.window_switch_enabled,
    "bank_token": settings.bank_token, "api_pw": settings.dslrbooth_api_password,
    "tg_url": settings.support_tg_url, "tg_handle": settings.support_tg_handle,
    "warnings": settings.warnings(), "tag": notify._TAG,
}, ensure_ascii=False))
"""


def load(tmp_path: Path, raw: bytes | None, name: str = ".env") -> dict:
    env_file = tmp_path / name
    if raw is not None:
        env_file.write_bytes(raw)
    env = {
        "PATH": os.environ.get("PATH", ""),
        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
        "INSTABOX_ENV_FILE": str(env_file),
        "PYTHONIOENCODING": "utf-8",
    }
    r = subprocess.run([sys.executable, "-c", _PROBE], cwd=ROOT, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, f"booth crashed on this .env:\n{r.stderr[-1500:]}"
    return json.loads(r.stdout.strip().splitlines()[-1])


GOOD = (
    "BANK_TOKEN=mJZ4testtoken\n"
    "DSLRBOOTH_API_PASSWORD=pw123\n"
    "SUPPORT_TELEGRAM=@instabox\n"
    "PAYMENT_PROVIDER=monobank\n"
    "DSLRBOOTH_TRIGGER=rest\n"
    "BOOTH_NAME=InstaBOX\n"
    "BOOTH_ID=kiosk-01\n"
)


def warned(cfg: dict, needle: str) -> bool:
    return any(needle.lower() in w.lower() for w in cfg["warnings"])


# ── encodings & file shape ─────────────────────────────────────────────

def test_plain_utf8(tmp_path):
    c = load(tmp_path, GOOD.encode())
    assert c["provider"] == "monobank" and c["bank_token"] == "mJZ4testtoken"
    assert c["trigger"] == "rest"


def test_utf8_bom_does_not_eat_first_key(tmp_path):
    # older Notepad saves UTF-8 with a BOM; it used to become part of key #1
    c = load(tmp_path, b"\xef\xbb\xbf" + ("BOOTH_ID=kiosk-07\n" + GOOD).encode())
    assert c["booth_id"] in ("kiosk-07", "kiosk-01")
    assert c["bank_token"] == "mJZ4testtoken"


def test_cp1251_with_cyrillic_name(tmp_path):
    # Notepad "ANSI" save: used to crash at import with UnicodeDecodeError
    c = load(tmp_path, (GOOD + "BOOTH_NAME=Фото Кабінка\n").encode("cp1251"))
    assert c["booth_name"] == "Фото Кабінка"


def test_crlf(tmp_path):
    c = load(tmp_path, GOOD.replace("\n", "\r\n").encode())
    assert c["bank_token"] == "mJZ4testtoken" and c["api_pw"] == "pw123"


def test_empty_file(tmp_path):
    c = load(tmp_path, b"")
    assert c["provider"] == "unconfigured"


def test_binary_garbage(tmp_path):
    c = load(tmp_path, bytes(range(256)) * 4)
    assert c["provider"] in ("unconfigured", "mock")


def test_no_env_file_refuses_to_sell(tmp_path):
    c = load(tmp_path, None)
    assert c["env_file"] is None
    assert c["provider"] == "unconfigured", "no .env must NOT silently mean test mode"
    assert warned(c, "no .env")


def test_shipped_file_not_renamed(tmp_path):
    # client copies InstaBOX.env.txt into the folder and never renames it
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
    probe = tmp_path / "proj"
    import shutil
    shutil.copytree(ROOT / "backend", probe / "backend")
    (probe / "VERSION").write_text("0", encoding="utf-8")
    (probe / "InstaBOX.env.txt").write_text(GOOD, encoding="utf-8")
    r = subprocess.run([sys.executable, "-c", _PROBE], cwd=probe, env=env,
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0, r.stderr[-1500:]
    c = json.loads(r.stdout.strip().splitlines()[-1])
    assert c["env_file"] == "InstaBOX.env.txt" and c["provider"] == "monobank"
    assert warned(c, "rename")


# ── how values get typed ───────────────────────────────────────────────

@pytest.mark.parametrize("line", [
    'BANK_TOKEN="mJZ4testtoken"',
    "BANK_TOKEN='mJZ4testtoken'",
    "BANK_TOKEN = mJZ4testtoken",
    "BANK_TOKEN=mJZ4testtoken   ",
    "BANK_TOKEN=\tmJZ4testtoken",
    "BANK_TOKEN=mJZ4testtoken                # <<< ЗАПОВНИТИ",
    "BANK_TOKEN=mJZ4testtoken# <<< ЗАПОВНИТИ",       # pasted over the padding
    "BANK_TOKEN=mJZ4testtoken #коментар",
])
def test_token_typing_variants(tmp_path, line):
    c = load(tmp_path, (GOOD.replace("BANK_TOKEN=mJZ4testtoken\n", "") + line + "\n").encode())
    assert c["bank_token"] == "mJZ4testtoken", line


def test_duplicate_key_keeps_last_non_empty(tmp_path):
    raw = "BANK_TOKEN=mJZ4testtoken\n" + GOOD.replace("BANK_TOKEN=mJZ4testtoken\n", "") + "BANK_TOKEN=\n"
    assert load(tmp_path, raw.encode())["bank_token"] == "mJZ4testtoken"


def test_marker_left_untouched_means_empty(tmp_path):
    c = load(tmp_path, b"BANK_TOKEN=                # <<< \xd0\x97\nPAYMENT_PROVIDER=monobank\n")
    assert c["bank_token"] == "" and c["provider"] == "unconfigured"
    assert "BANK_TOKEN" in c["reason"]


# ── nonsense values ────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,price", [
    ("150 грн", 150), ("150грн", 150), ("1,50", 1), ("0", 150), ("-5", 150),
    ("abc", 150), ("", 150), ("1e9999", 150), ("nan", 150),
])
def test_price_never_breaks(tmp_path, raw, price):
    assert load(tmp_path, (GOOD + f"PRICE_UAH={raw}\n").encode())["price"] == price


@pytest.mark.parametrize("raw,shots", [("0", 4), ("-1", 4), ("4", 4), ("99", 10), ("чотири", 4)])
def test_shots_never_zero(tmp_path, raw, shots):
    # SHOTS=0 used to make every session end with shot==0 -> refund
    assert load(tmp_path, (GOOD + f"SHOTS={raw}\n").encode())["shots"] == shots


@pytest.mark.parametrize("raw,port", [("8000  # мій порт", 8000), ("abc", 8000), ("0", 8000), ("99999", 65535)])
def test_port(tmp_path, raw, port):
    assert load(tmp_path, (GOOD + f"PORT={raw}\n").encode())["port"] == port


@pytest.mark.parametrize("raw,provider", [
    ("monobank", "monobank"), ("Monobank", "monobank"), ("MONOBANK", "monobank"),
    (" monobank ", "monobank"), ("monobonk", "unconfigured"), ("mono", "unconfigured"),
    ("mock", "mock"),
])
def test_provider_typos_never_silently_mock(tmp_path, raw, provider):
    c = load(tmp_path, (GOOD.replace("PAYMENT_PROVIDER=monobank", f"PAYMENT_PROVIDER={raw}")).encode())
    assert c["provider"] == provider
    if provider == "unconfigured":
        assert warned(c, "will NOT sell")


@pytest.mark.parametrize("raw,trigger", [("rest", "rest"), ("REST", "rest"), ("hotkey", "hotkey"), ("restt", "hotkey")])
def test_trigger(tmp_path, raw, trigger):
    c = load(tmp_path, GOOD.replace("DSLRBOOTH_TRIGGER=rest", f"DSLRBOOTH_TRIGGER={raw}").encode())
    assert c["trigger"] == trigger


@pytest.mark.parametrize("raw,on", [("1", True), ("так", True), ("Так", True), ("yes", True),
                                    ("0", False), ("ні", False), ("", False)])
def test_bool_in_ukrainian(tmp_path, raw, on):
    assert load(tmp_path, (GOOD + f"DSLRBOOTH_EVENTS_ENABLED={raw}\n").encode())["events"] is on


# ── identity ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,slug", [
    ("kiosk-01", "kiosk-01"), ("Кабінка 1", "kabinka-1"), ("ТРЦ Ocean", "trts-ocean"),
    ("kiosk 01", "kiosk-01"), ("../../etc", "etc"), ("", "booth-local"), ("!!!", "booth-local"),
])
def test_booth_id_safe_for_files_and_tasks(tmp_path, raw, slug):
    c = load(tmp_path, GOOD.replace("BOOTH_ID=kiosk-01", f"BOOTH_ID={raw}").encode())
    assert c["booth_id"] == slug


def test_booth_name_with_html_chars_keeps_telegram_alive(tmp_path):
    # "&" unescaped made Telegram reject EVERY message
    c = load(tmp_path, GOOD.replace("BOOTH_NAME=InstaBOX", "BOOTH_NAME=Фото & Кава <1>").encode())
    assert "&amp;" in c["tag"] and "&lt;1&gt;" in c["tag"] and "<1>" not in c["tag"]


@pytest.mark.parametrize("raw,url,handle", [
    ("instabox", "https://t.me/instabox", "@instabox"),
    ("@instabox", "https://t.me/instabox", "@instabox"),
    ("t.me/instabox", "https://t.me/instabox", "@instabox"),
    ("https://t.me/instabox", "https://t.me/instabox", "@instabox"),
    (" @instabox ", "https://t.me/instabox", "@instabox"),
])
def test_support_telegram_forms(tmp_path, raw, url, handle):
    c = load(tmp_path, GOOD.replace("SUPPORT_TELEGRAM=@instabox", f"SUPPORT_TELEGRAM={raw}").encode())
    assert c["tg_url"] == url and c["tg_handle"] == handle


def test_chat_id_as_username_is_flagged(tmp_path):
    c = load(tmp_path, (GOOD + "TELEGRAM_ALERTS_BOT_TOKEN=1:x\nTELEGRAM_ADMIN_CHAT_ID=@owner\n").encode())
    assert warned(c, "must be a number")


def test_rest_without_password_is_flagged(tmp_path):
    c = load(tmp_path, GOOD.replace("DSLRBOOTH_API_PASSWORD=pw123\n", "").encode())
    assert warned(c, "DSLRBOOTH_API_PASSWORD is empty")


@pytest.mark.parametrize("raw,paper", [
    ("1 000", 1000), ("1 000", 1000), ("1,000", 1000), ("1.000", 1000),
    ("400", 400), ("400 шт", 400), ("0", 0), ("-1", 700),
])
def test_paper_thousands_separators(tmp_path, raw, paper):
    # "1 000" read as 1 stopped the booth after its first strip
    assert load(tmp_path, (GOOD + f"PAPER_CAPACITY={raw}\n").encode())["paper"] == paper


def test_provider_line_deleted_is_not_test_mode(tmp_path):
    c = load(tmp_path, GOOD.replace("PAYMENT_PROVIDER=monobank\n", "").encode())
    assert c["provider"] == "unconfigured"
