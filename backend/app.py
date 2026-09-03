"""FastAPI app: kiosk page, SSE stream, health/status, webhook, admin, mock pay."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import VERSION, BASE_DIR, settings
from .controller import Controller
from .dslrbooth import build_trigger
from .logging_setup import setup as setup_logging
from .payment import MockProvider, build_provider
from .state import Broadcaster, SessionStore

setup_logging()
log = logging.getLogger("app")

templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
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

    task = asyncio.create_task(controller.run(), name="controller")
    log.info("started %s v%s  provider=%s trigger=%s  %s %s",
             settings.booth_id, VERSION, provider.name, trigger.name,
             settings.price_uah, settings.currency)
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
            "booth_name": settings.booth_name,
            "support_phone": settings.support_phone,
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
        ("Оплата / тригер", f"{s['provider']} / {s['trigger']}"),
        ("Аптайм", f"{s['uptime_sec'] // 3600} год {s['uptime_sec'] % 3600 // 60} хв"),
        ("Сьогодні", f"{t['sessions']} сесій · {t['revenue']} {settings.currency} · {t['prints']} фото"),
        ("Папір", "—" if paper is None else f"≈ {paper} відбитків"),
        ("Остання помилка", s["last_error"] or "—"),
    ]
    body = "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>{s['booth_name']} · статус</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;max-width:520px;margin:24px auto;padding:0 16px;color:#16151a}}
h1{{font-size:19px}} table{{width:100%;border-collapse:collapse}}
th{{text-align:left;color:#8a8a8e;font-weight:600;padding:8px 12px 8px 0;white-space:nowrap;vertical-align:top}}
td{{padding:8px 0}} tr+tr{{border-top:1px solid #eee}}</style>
<h1>{s['booth_name']} · статус</h1><table>{body}</table>""",
    )


# ───────────────────────── admin (token) ─────────────────────────
def _admin_ok(request: Request) -> bool:
    if not settings.admin_token:
        return False
    given = request.query_params.get("token") or request.headers.get("X-Admin-Token")
    return given == settings.admin_token


@app.post("/admin/{action}")
async def admin(action: str, request: Request) -> JSONResponse:
    if not _admin_ok(request):
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
