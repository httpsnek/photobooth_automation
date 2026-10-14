"""FastAPI app: kiosk page, SSE stream, health/status, webhook, admin, mock pay."""
from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import sys

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .booth_events import BoothEventRouter
from .config import VERSION, BASE_DIR, settings
from .controller import Controller
from .dslrbooth import build_trigger
from .logging_setup import setup as setup_logging
from .payment import MockProvider, build_provider
from .state import Broadcaster, SessionStore

setup_logging()
log = logging.getLogger("app")

templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))


ALREADY_RUNNING_EXIT = 17   # run.bat stops its restart loop on this code
_instance_lock = None


def _acquire_instance_lock():
    """One backend per booth. uvicorn runs lifespan BEFORE binding the port,
    so a second copy (СТАРТ_ТУТ.bat run again, run.bat double-clicked) would
    start a controller against the same DB before failing on the port — and
    its restart loop would then retry every 5s forever. An OS lock is released
    by Windows itself if the holder dies, so a killed backend never leaves a
    stale lock behind."""
    path = settings.db_file.with_suffix(".lock")
    f = open(path, "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    global _instance_lock
    _instance_lock = _acquire_instance_lock()
    if _instance_lock is None:
        log.error("another InstaBOX backend for booth %s is already running — exiting",
                  settings.booth_id)
        os._exit(ALREADY_RUNNING_EXIT)

    for w in settings.warnings():
        log.warning("CONFIG: %s", w)

    store = SessionStore()
    await store.init()
    provider = build_provider()
    trigger = build_trigger()
    broadcaster = Broadcaster()
    controller = Controller(provider, trigger, store, broadcaster)

    app.state.controller = controller
    app.state.provider = provider
    app.state.trigger = trigger
    app.state.broadcaster = broadcaster
    app.state.booth_events = BoothEventRouter(controller)

    task = asyncio.create_task(controller.run(), name="controller")
    log.info("started %s v%s  provider=%s trigger=%s events=%s  %s %s",
             settings.booth_id, VERSION, provider.name, trigger.name,
             settings.dslrbooth_events_enabled, settings.price_uah, settings.currency)
    try:
        yield
    finally:
        controller.stop()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        with contextlib.suppress(Exception):
            await provider.aclose()
        aclose = getattr(trigger, "aclose", None)
        if aclose:
            with contextlib.suppress(Exception):
                await aclose()


app = FastAPI(title=f"{settings.booth_name} kiosk", version=VERSION, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "frontend" / "static")), name="static")


def _ctrl(request: Request) -> Controller:
    return request.app.state.controller


# ───────────────────────── kiosk ─────────────────────────
@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/kiosk")


@app.get("/kiosk", response_class=HTMLResponse)
async def kiosk(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "kiosk.html",
        {
            "version": VERSION,
            "booth_name": settings.booth_name,
            "support_phone": settings.support_phone,
            "support_tg_url": settings.support_tg_url,
            "support_tg_handle": settings.support_tg_handle,
            "price": settings.price_uah,
            "currency": settings.currency,
            "shots": settings.shots,
            "session_duration": settings.session_duration_sec,
            "shot_countdown": settings.shot_countdown_sec,
            "attract_video": settings.attract_video,
            "paid_video": settings.paid_video,
            "qr_left_pct": settings.qr_left_pct,
            "qr_top_pct": settings.qr_top_pct,
            "qr_size_pct": settings.qr_size_pct,
            "qr_height_pct": settings.qr_height_pct,
            "qr_radius_pct": settings.qr_radius_pct,
        },
    )


@app.get("/qr")
async def qr(text: str = "https://instabox.example/demo") -> Response:
    from .qr import qr_png
    return Response(qr_png(text[:512]), media_type="image/png",
                    headers={"Cache-Control": "public, max-age=300"})


@app.get("/gallery", response_class=HTMLResponse)
async def gallery(request: Request, ar: str = "16 / 9") -> HTMLResponse:
    """All screens on one page — a link to send the client."""
    import re
    if not re.fullmatch(r"[\d./ ]{1,12}", ar or ""):
        ar = "16 / 9"
    screens = [
        ("connecting", "Завантаження"),
        ("awaiting_payment", "Оплата / QR"),
        ("paid", "Оплату отримано"),
        ("shooting", "Зйомка · відлік 3·2·1"),
        ("printing", "Друк"),
        ("done", "Готово"),
        ("refunded", "Помилка · повернення"),
        ("out_of_service", "Не працює"),
    ]
    cards = "".join(
        f'<figure><div class="frame"><iframe src="/kiosk?demo={s}" '
        f'loading="lazy" title="{t}"></iframe></div><figcaption>{t}</figcaption></figure>'
        for s, t in screens
    )
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{settings.booth_name} — екрани</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; padding:28px; background:#f4f4f6; color:#16151a;
         font:600 15px/1.4 "Montserrat",system-ui,sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 4px; }}
  p.sub {{ margin:0 0 24px; color:#8a8a8e; font-weight:500; }}
  .grid {{ display:grid; gap:24px 20px;
           grid-template-columns:repeat(auto-fill,minmax(340px,1fr)); }}
  figure {{ margin:0; }}
  .frame {{ aspect-ratio:{ar}; border-radius:18px; overflow:hidden;
            background:#fff; box-shadow:0 6px 24px rgba(0,0,0,.10); }}
  iframe {{ width:100%; height:100%; border:0; display:block; }}
  figcaption {{ margin-top:10px; text-align:center; color:#5a5a62; }}
</style>
<h1>{settings.booth_name} — усі екрани</h1>
<p class="sub">Живий інтерфейс, формат 16:9. «Зйомка» анімується — відлік 3·2·1 перед кожним кадром.
 Вертикально: <code>/gallery?ar=9/16</code>.</p>
<div class="grid">{cards}</div>""",
    )


@app.get("/events")
async def events(request: Request) -> StreamingResponse:
    broadcaster: Broadcaster = request.app.state.broadcaster
    queue = await broadcaster.subscribe()

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                 "X-Accel-Buffering": "no"},
    )


# ───────────────────────── health / status ─────────────────────────
@app.get("/health")
async def health(request: Request) -> dict:
    ctrl = _ctrl(request)
    booth_ok = True
    with contextlib.suppress(Exception):
        booth_ok = await request.app.state.trigger.healthy()
    return {**await ctrl.status(), "booth_healthy": booth_ok}


@app.get("/status", response_class=HTMLResponse)
async def status_page(request: Request) -> HTMLResponse:
    s = await _ctrl(request).status()
    t = s["today"]
    paper = s["paper_left"]
    rows = [
        ("Точка", f"{s['booth_name']} ({s['booth_id']})"),
        ("Версія", s["version"]),
        ("Стан", s["state"] + ("  ⏸ ПАУЗА" if s["paused"] else "")),
        ("Оплата / тригер", f"{s['provider']} / {s['trigger']}"
                            + ("  · події dslrBooth" if s.get("events") else "  · таймери")),
        ("Аптайм", f"{s['uptime_sec'] // 3600} год {s['uptime_sec'] % 3600 // 60} хв"),
        ("Сьогодні", f"{t['sessions']} сесій · {t['revenue']} {settings.currency} · {t['prints']} фото"),
        ("Папір", "—" if paper is None else f"≈ {paper} відбитків"),
        ("Черга повернень", "—" if not s.get("pending_refunds")
                            else f"⚠️ {s['pending_refunds']}"),
        ("Блокування", s.get("block_reason") or "—"),
        ("Остання помилка", s["last_error"] or "—"),
    ]
    body = "".join(f"<tr><th>{k}</th><td>{html.escape(str(v))}</td></tr>" for k, v in rows)
    name = html.escape(s["booth_name"])
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{name} · статус</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:520px;margin:24px auto;padding:0 16px;color:#16151a}}
h1{{font-size:19px}} table{{width:100%;border-collapse:collapse}}
th{{text-align:left;color:#8a8a8e;font-weight:600;padding:8px 12px 8px 0;white-space:nowrap;vertical-align:top}}
td{{padding:8px 0;white-space:pre-line}} tr+tr{{border-top:1px solid #eee}}</style>
<h1>{name} · статус</h1><table>{body}</table>
<p style="margin-top:20px;color:#8a8a8e;font-size:14px">
Перевірка перемикання вікон:
<code>/admin/test-windows?token=&lt;ADMIN_TOKEN&gt;</code></p>""",
    )


# ───────────────────────── admin (token) ─────────────────────────
def _admin_ok(request: Request) -> bool:
    if not settings.admin_token:
        return False
    given = request.query_params.get("token") or request.headers.get("X-Admin-Token")
    return given == settings.admin_token


@app.get("/admin/test-windows", response_class=HTMLResponse, include_in_schema=False)
async def admin_test_windows(request: Request) -> Response:
    """On-site check of the riskiest moving part, without spending a hryvnia.

    Best opened from a phone on the same network: the booth screen then shows
    the actual handoff while the phone reports what happened.
    """
    if not _admin_ok(request):
        return HTMLResponse("<h1>401 — невірний token</h1>", status_code=401)

    from . import winfocus
    if not winfocus._IS_WIN:
        return HTMLResponse("<meta charset=utf-8><h1>Перемикання вікон працює лише у Windows</h1>")
    if not settings.window_switch_enabled:
        return HTMLResponse("<meta charset=utf-8><h1>WINDOW_SWITCH_ENABLED=0</h1>"
                            "<p>Перемикання вимкнено в .env — увімкни, щоб перевірити.</p>")

    monitors = await asyncio.to_thread(winfocus.display_count)
    booth_ok = await winfocus.raise_booth()
    await asyncio.sleep(4)
    kiosk_ok = await winfocus.raise_kiosk()

    def row(ok: bool, name: str, hint: str) -> str:
        mark = "✅" if ok else "❌"
        return f"<tr><td>{mark}</td><td><b>{name}</b></td><td>{'' if ok else hint}</td></tr>"

    body = f"""<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{font:16px/1.5 system-ui;margin:0;padding:24px;background:#fff;color:#111}}
h1{{font-size:20px;margin:0 0 4px}}p{{color:#666;margin:0 0 20px}}
table{{border-collapse:collapse;width:100%;max-width:640px}}
td{{padding:10px 8px;border-bottom:1px solid #eee;vertical-align:top}}
td:first-child{{width:28px}}td:last-child{{color:#b00;font-size:14px}}</style>
<h1>Перевірка перемикання вікон</h1>
<p>Монiторiв у системi: <b>{monitors or "невідомо"}</b></p>
<table>
{row(bool(monitors), "Екран знайдено", "Windows не бачить жодного монітора")}
{row(booth_ok, "Вікно dslrBooth піднялось",
     "Не знайдено або не виходить на передній план. Запусти dslrBooth; "
     "перевір, що в ньому ВИМКНЕНО «always on top».")}
{row(kiosk_ok, "Вікно кіоску повернулось",
     f"Не знайдено вікно з назвою «{html.escape(settings.kiosk_title)}». "
     "Запусти start_kiosk.bat; назва береться з BOOTH_NAME.")}
</table>
<p style="margin-top:24px">Якщо обидва рядки зеленi — зйомка i повернення на наш екран
працюватимуть. Повтори: онови цю сторiнку.</p>"""
    return HTMLResponse(body)


_ADMIN_ACTIONS = {
    "pause":       ("Поставити кабінку на паузу", "Кабінку поставлено на паузу."),
    "resume":      ("Відновити продаж (зняти паузу / блокування камери)", "Продаж відновлено."),
    "paper-reset": ("Новий рулон — скинути лічильник паперу", "Лічильник паперу скинуто."),
}


def _admin_page(title: str, body: str, status: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{html.escape(settings.booth_name)} · {html.escape(title)}</title>
<style>body{{font:17px/1.5 system-ui,sans-serif;max-width:480px;margin:32px auto;padding:0 18px;color:#16151a}}
h1{{font-size:20px}} button{{font:600 18px system-ui;width:100%;padding:18px;border:0;border-radius:12px;
background:#D6256E;color:#fff}} p{{color:#555}}</style>
<h1>{html.escape(title)}</h1>{body}""",
        status_code=status,
    )


@app.get("/admin/{action}", response_class=HTMLResponse, include_in_schema=False)
async def admin_confirm(action: str, request: Request) -> Response:
    """The owner opens these links on a phone, which is a GET. Doing the
    action on GET would let a link preview (Telegram fetches pasted links)
    pause the booth on its own — so GET only shows a confirm button."""
    if not _admin_ok(request):
        return _admin_page("Невірне посилання", "<p>Перевірте token у посиланні.</p>", 401)
    if action not in _ADMIN_ACTIONS:
        return _admin_page("Невідома дія", "", 404)
    label, _ = _ADMIN_ACTIONS[action]
    token = html.escape(request.query_params.get("token", ""), quote=True)
    return _admin_page(label, f"""<form method="post" action="/admin/{action}?token={token}">
<button type="submit">Підтвердити</button></form>""")


@app.post("/admin/{action}")
async def admin(action: str, request: Request) -> Response:
    browser = "text/html" in request.headers.get("accept", "")
    if not _admin_ok(request):
        if browser:
            return _admin_page("Невірне посилання", "<p>Перевірте token у посиланні.</p>", 401)
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    ctrl = _ctrl(request)
    if action == "pause":
        await ctrl.pause("адмін")
    elif action == "resume":
        await ctrl.resume("адмін")
    elif action == "paper-reset":
        await ctrl.reset_paper()
    else:
        return JSONResponse({"error": "unknown action"}, status_code=404)
    if browser:
        _, done = _ADMIN_ACTIONS[action]
        return _admin_page("Готово", f"<p>{html.escape(done)}</p>")
    return JSONResponse(await ctrl.status())


# ───────────────────────── webhook ─────────────────────────
@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """Monobank webhook — an accelerator only; the poller stays authoritative."""
    raw = await request.body()
    provider = request.app.state.provider
    sign = request.headers.get("X-Sign", "")
    ok = False
    with contextlib.suppress(Exception):
        ok = await provider.verify_webhook(raw, sign)
    if not ok:
        log.warning("webhook rejected (bad or missing signature)")
        return Response(status_code=403)
    _ctrl(request).notify_payment_hint()
    return Response(status_code=200)


# ───────────────────────── dslrBooth Trigger events ─────────────────────────
@app.api_route("/dslrbooth/event", methods=["GET", "POST"], include_in_schema=False)
async def dslrbooth_event(request: Request) -> Response:
    """dslrBooth Pro → Settings › General › Triggers → this URL.
    It calls us (HTTP GET) with ?event_type=…&param1=… on every session event.
    """
    q = request.query_params
    # dslrBooth runs on this same PC and calls 127.0.0.1. A token typo in the
    # hand-typed Triggers URL used to silently drop every event — and with
    # no events every paid session ended in a refund. Only callers from the
    # LAN still need the token.
    host = (request.client.host if request.client else "") or ""
    local = host in ("127.0.0.1", "::1", "localhost") or host.startswith("::ffff:127.")
    if not local and settings.dslrbooth_event_token and q.get("token") != settings.dslrbooth_event_token:
        log.warning("dslrbooth event from %s rejected (bad token)", host)
        return Response(status_code=403)
    params = dict(q)
    await request.app.state.booth_events.handle(params.get("event_type", ""), params)
    return Response(status_code=200)


# ───────────────────────── mock payment page ─────────────────────────
@app.get("/mock/pay/{invoice_id}", response_class=HTMLResponse)
async def mock_pay(invoice_id: str, request: Request) -> HTMLResponse:
    if not isinstance(request.app.state.provider, MockProvider):
        return HTMLResponse("mock disabled", status_code=404)
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Тестова оплата</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;
min-height:100vh;margin:0;align-items:center;justify-content:center}}.card{{text-align:center}}
button{{font-size:1.4rem;padding:1rem 2rem;border:0;border-radius:12px;background:#22c55e;
color:#04210f;font-weight:700}}form{{margin-top:1.5rem}}</style>
<div class="card"><h1>Тестова оплата</h1>
<p>Рахунок <code>{invoice_id}</code> — {settings.price_uah} {settings.currency}</p>
<form method="post" action="/mock/pay/{invoice_id}"><button type="submit">Оплатити (тест)</button></form></div>""",
    )


@app.post("/mock/pay/{invoice_id}", response_class=HTMLResponse)
async def mock_pay_confirm(invoice_id: str, request: Request) -> HTMLResponse:
    provider = request.app.state.provider
    if not isinstance(provider, MockProvider):
        return HTMLResponse("mock disabled", status_code=404)
    ok = provider.mark_paid(invoice_id)
    _ctrl(request).notify_payment_hint()
    msg = "Оплачено ✓ Поверніться до фотокабінки" if ok else "Рахунок не знайдено"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Оплата</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;
min-height:100vh;margin:0;align-items:center;justify-content:center;font-size:1.5rem}}</style>
<div>{msg}</div>""",
    )
