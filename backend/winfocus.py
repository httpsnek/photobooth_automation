"""Who owns the touch screen right now.

The booth has ONE touch monitor (HDMI + USB touch) wired to the laptop, and
two fullscreen apps share it:

    Chrome (our kiosk)  — QR / payment, "друкуємо", "готово", error screens
    dslrBooth           — live view, countdown, retake (its own native UI)

The controller hands the glass over at the state transitions: dslrBooth owns
it from the moment a session is triggered until `printing`/`session_end`, our
kiosk owns it the rest of the time.

Everything here no-ops off Windows, so the dev machine and the tests are
unaffected.
"""
from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

from .config import settings

log = logging.getLogger("winfocus")

_IS_WIN = sys.platform == "win32"

SW_RESTORE = 9
SM_CMONITORS = 80
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
_SWP = 0x0002 | 0x0001 | 0x0040          # NOMOVE | NOSIZE | SHOWWINDOW

_dll: tuple | None = None                # (user32, kernel32, wintypes) once bound


def _api():
    """Bind user32/kernel32 once, with the arg/res types that matter on 64-bit.

    Without explicit restypes ctypes assumes c_int and truncates HWNDs, which
    silently breaks every comparison below.
    """
    global _dll
    if _dll is not None:
        return _dll
    if not _IS_WIN:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        u32.GetForegroundWindow.restype = wintypes.HWND
        u32.SetForegroundWindow.argtypes = [wintypes.HWND]
        u32.BringWindowToTop.argtypes = [wintypes.HWND]
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsIconic.argtypes = [wintypes.HWND]
        u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
        u32.SetWindowPos.argtypes = [
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ]
        u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u32.GetSystemMetrics.argtypes = [ctypes.c_int]
        k32.GetCurrentThreadId.restype = wintypes.DWORD

        _dll = (ctypes, wintypes, u32, k32)
        return _dll
    except Exception as exc:  # pragma: no cover — win32 only
        log.warning("win32 unavailable, window switching disabled: %s", exc)
        return None


def _pids_for(names: set[str]) -> set[int]:
    if not names:
        return set()
    try:
        import psutil
        return {
            p.pid for p in psutil.process_iter(["name"])
            if (p.info.get("name") or "").lower() in names
        }
    except Exception:
        return set()


def _find_window(titles: tuple[str, ...], proc_names: set[str]) -> int:
    """Best visible top-level window matching a title substring or a process.

    Ties break on area: the fullscreen window wins over tool/hidden ones.
    """
    api = _api()
    if api is None:
        return 0
    ctypes, wintypes, u32, _ = api

    wanted = tuple(t.lower() for t in titles if t)
    pids = _pids_for(proc_names)
    best, best_area = 0, -1

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _lparam):
        nonlocal best, best_area
        if not u32.IsWindowVisible(hwnd):
            return True
        n = u32.GetWindowTextLengthW(hwnd)
        title = ""
        if n:
            buf = ctypes.create_unicode_buffer(n + 1)
            u32.GetWindowTextW(hwnd, buf, n + 1)
            title = (buf.value or "").lower()

        hit = bool(wanted) and any(w in title for w in wanted)
        if not hit and pids:
            pid = wintypes.DWORD()
            u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hit = pid.value in pids and bool(title)
        if not hit:
            return True

        r = wintypes.RECT()
        area = 0
        if u32.GetWindowRect(hwnd, ctypes.byref(r)):
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
        if area > best_area:
            best, best_area = ctypes.cast(hwnd, ctypes.c_void_p).value or 0, area
        return True

    try:
        u32.EnumWindows(_cb, 0)
    except Exception as exc:
        log.debug("EnumWindows failed: %s", exc)
    return best


def _raise(hwnd: int) -> bool:
    """Force a window to the foreground.

    Windows refuses SetForegroundWindow from a process that does not own the
    current foreground window, so we briefly attach to that window's input
    queue — the standard workaround — and fall back to a topmost toggle.
    """
    api = _api()
    if api is None or not hwnd:
        return False
    ctypes, wintypes, u32, k32 = api

    handle = ctypes.c_void_p(hwnd)
    if u32.IsIconic(handle):
        u32.ShowWindow(handle, SW_RESTORE)

    fg = u32.GetForegroundWindow()
    fg_i = ctypes.cast(fg, ctypes.c_void_p).value or 0
    if fg_i == hwnd:
        return True

    cur_tid = k32.GetCurrentThreadId()
    fg_tid = u32.GetWindowThreadProcessId(fg, None) if fg_i else 0
    attached = bool(fg_tid) and fg_tid != cur_tid and bool(
        u32.AttachThreadInput(cur_tid, fg_tid, True)
    )
    try:
        u32.BringWindowToTop(handle)
        u32.SetForegroundWindow(handle)
    finally:
        if attached:
            u32.AttachThreadInput(cur_tid, fg_tid, False)

    now = ctypes.cast(u32.GetForegroundWindow(), ctypes.c_void_p).value or 0
    if now == hwnd:
        return True

    u32.SetWindowPos(handle, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0, _SWP)
    u32.SetWindowPos(handle, ctypes.c_void_p(HWND_NOTOPMOST), 0, 0, 0, 0, _SWP)
    u32.SetForegroundWindow(handle)
    now = ctypes.cast(u32.GetForegroundWindow(), ctypes.c_void_p).value or 0
    return now == hwnd


def _names(csv: str) -> set[str]:
    return {n.strip().lower() for n in csv.split(",") if n.strip()}


# Every raise goes through one lock, so the screen guard (below) and a
# session handoff can never interleave and leave the wrong app on top.
_LOCK = threading.Lock()
_missing: set[str] = set()   # warn once per missing window, not every call


def _bring_up(what: str, titles: tuple[str, ...], procs: set[str]) -> bool:
    with _LOCK:
        hwnd = _find_window(titles, procs)
        if not hwnd:
            if what not in _missing:
                _missing.add(what)
                log.warning("%s window not found (titles=%s procs=%s)",
                            what, titles, sorted(procs))
            return False
        _missing.discard(what)
        for attempt in range(1, max(1, settings.window_raise_retries) + 1):
            if _raise(hwnd):
                log.info("%s window raised", what)
                return True
            log.debug("%s raise attempt %d failed", what, attempt)
        log.warning("%s window would not come forward", what)
        return False


def _foreground_pid() -> int:
    api = _api()
    if api is None:
        return 0
    ctypes, wintypes, u32, _ = api
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(u32.GetForegroundWindow(), ctypes.byref(pid))
    return int(pid.value)


def _sync_reclaim(booth_busy) -> bool:
    """Put the kiosk back over a FRESHLY LAUNCHED dslrBooth.

    At boot Chrome and dslrBooth start in random order; a newly launched
    process is allowed to take the foreground, so dslrBooth's start screen
    can land on top of our QR — and stay there, because nothing else raises
    the kiosk until a paid session ends, which can't happen without a QR.

    Only a dslrBooth process younger than SCREEN_GUARD_FRESH_SEC counts: an
    owner who deliberately opens dslrBooth's settings on a running booth is
    left alone (and /admin/pause stops the guard entirely).
    """
    with _LOCK:
        if booth_busy():
            return False
        fg = _foreground_pid()
        if not fg or fg not in _pids_for(_names(settings.dslrbooth_proc_names)):
            return False
        try:
            import psutil
            age = time.time() - psutil.Process(fg).create_time()
        except Exception:
            return False
        if age > settings.screen_guard_fresh_sec:
            return False
        hwnd = _find_window((settings.kiosk_title,), _names(settings.kiosk_proc_names))
        if not hwnd or booth_busy():
            return False
        ok = _raise(hwnd)
        if ok:
            log.info("kiosk reclaimed the screen from a freshly started dslrBooth")
        return ok


async def reclaim_from_booth(booth_busy) -> bool:
    if not (_IS_WIN and settings.window_switch_enabled):
        return False
    return await asyncio.to_thread(_sync_reclaim, booth_busy)


def _sync_raise_booth() -> bool:
    return _bring_up("dslrBooth", ("dslrbooth", "lumabooth"), _names(settings.dslrbooth_proc_names))


def _sync_raise_kiosk() -> bool:
    return _bring_up("kiosk", (settings.kiosk_title,), _names(settings.kiosk_proc_names))


async def raise_booth() -> bool:
    """Give the screen to dslrBooth (live view / countdown / retake)."""
    if not (_IS_WIN and settings.window_switch_enabled):
        return False
    return await asyncio.to_thread(_sync_raise_booth)


async def raise_kiosk() -> bool:
    """Take the screen back for our Chrome kiosk. Safe to call repeatedly."""
    if not (_IS_WIN and settings.window_switch_enabled):
        return False
    return await asyncio.to_thread(_sync_raise_kiosk)


def display_count() -> int:
    """Number of display monitors Windows currently sees (0 = unknown)."""
    api = _api()
    if api is None:
        return 0
    _, _, u32, _ = api
    try:
        return int(u32.GetSystemMetrics(SM_CMONITORS))
    except Exception:
        return 0


async def display_problem() -> str:
    """Reason the guest-facing screen looks gone, or ''.

    The touch monitor is the whole product: if HDMI is pulled or it loses
    power, Windows drops the display and migrates the windows somewhere
    nobody can see — while the backend stays perfectly healthy. Without this
    check the booth would keep selling sessions into a black screen.
    """
    want = settings.display_min_count
    if not (_IS_WIN and want > 0):
        return ""
    have = await asyncio.to_thread(display_count)
    if have and have < want:
        return "Екран не підключено (перевір HDMI/живлення монітора)"
    return ""
