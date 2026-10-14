"""Installer helpers for setup.ps1, which calls them through the venv python.

    python -m backend.envtool ensure-tokens   fill empty ADMIN_TOKEN / DSLRBOOTH_EVENT_TOKEN
    python -m backend.envtool booth-id        the normalised BOOTH_ID (task names)
    python -m backend.envtool summary         the URLs the installer needs next

Done here rather than in PowerShell: Windows PowerShell 5.1 reads and writes
non-BOM files in the ANSI codepage, which mangles a Cyrillic BOOTH_NAME. These
helpers keep whatever encoding the client's Notepad saved the file in.
"""
from __future__ import annotations

import contextlib
import re
import secrets
import sys
from pathlib import Path

from .config import BASE_DIR, ENV_FILE

_TOKENS = ("ADMIN_TOKEN", "DSLRBOOTH_EVENT_TOKEN")


def _decode(raw: bytes) -> tuple[str, str]:
    if raw.startswith(b"\xef\xbb\xbf"):
        try:
            return raw[3:].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError:
            return "", ""
    for enc in ("utf-8", "cp1251"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return "", ""   # undecodable — callers must not rewrite such a file


def ensure_tokens(path: Path) -> list[str]:
    text, enc = _decode(path.read_bytes())
    if not enc:
        return []
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    filled: list[str] = []
    for key in _TOKENS:
        pat = re.compile(rf"^\s*{key}\s*=\s*(.*)$")
        idx = next((i for i, ln in enumerate(lines) if pat.match(ln)), None)
        current = pat.match(lines[idx]).group(1).split(" #", 1)[0].strip() if idx is not None else ""
        if current and not current.startswith("#"):
            continue
        value = secrets.token_urlsafe(16)
        if idx is None:
            lines.append(f"{key}={value}")
        else:
            lines[idx] = f"{key}={value}"
        filled.append(key)
    if filled:
        body = newline.join(lines) + newline
        data = body.encode("utf-8") if enc == "utf-8-sig" else body.encode(enc)
        if enc == "utf-8-sig":
            data = b"\xef\xbb\xbf" + data
        path.write_bytes(data)
    return filled


def stop_running() -> int:
    """Stop this folder's backend, watchdog and their restart loops, so a
    re-run of the installer starts fresh with the edited .env. Restart loops
    (run.bat / watchdog.bat) go first — kill only python and run.bat would
    bring the old process straight back.

    Matching is strict on purpose: workers must be a python process, loops a
    cmd.exe, and the chain of processes that launched us is never touched."""
    try:
        import psutil
    except Exception:
        return 0
    import os
    root = str(BASE_DIR).lower()
    spare = {os.getpid()}
    with contextlib.suppress(Exception):
        spare |= {a.pid for a in psutil.Process().parents()}
    loops, workers = [], []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if p.info["pid"] in spare:
                continue
            name = (p.info["name"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or []).lower()
            if root not in cmd and root not in (p.cwd() or "").lower():
                continue
            if name in ("cmd.exe", "cmd") and ("run.bat" in cmd or "watchdog.bat" in cmd):
                loops.append(p)
            elif name.startswith("python") and (
                ("uvicorn" in cmd and "backend.app" in cmd) or "watchdog.py" in cmd
            ):
                workers.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    n = 0
    for p in loops + workers:
        try:
            p.kill()
            n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return n


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    env = ENV_FILE or (BASE_DIR / ".env")

    if cmd == "stop":
        print(f"stopped {stop_running()} process(es)")
        return 0

    if cmd == "ensure-tokens":
        if not env.is_file():
            print("no .env yet")
            return 1
        filled = ensure_tokens(env)
        print("generated: " + ", ".join(filled) if filled else "tokens already set")
        return 0

    # everything below reads settings, so import after any token write
    from .config import settings

    if cmd == "booth-id":
        print(settings.booth_id)
        return 0

    if cmd == "summary":
        port, tok = settings.port, settings.admin_token
        lan = argv[2].strip() if len(argv) > 2 and argv[2].strip() else ""
        print(f"BOOTH_ID     {settings.booth_id}")
        print(f"Kiosk        http://localhost:{port}/kiosk")
        print(f"Status       http://localhost:{port}/status")
        if lan:
            print(f"From phone   http://{lan}:{port}/status")
        if tok:
            host = lan or "localhost"
            print(f"Window test  http://{host}:{port}/admin/test-windows?token={tok}")
        print("dslrBooth > Settings > General > Triggers, URL:")
        print(f"             http://127.0.0.1:{port}/dslrbooth/event")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
