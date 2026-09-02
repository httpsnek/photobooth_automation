"""FastAPI app: kiosk page, SSE stream, health, webhook, mock payment page."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, settings
from .controller import Controller
from .dslrbooth import build_trigger
from .payment import MockProvider, build_provider
from .state import Broadcaster, SessionStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
)
log = logging.getLogger("app")

templates = Jinja2Templates(directory=str(BASE_DIR / "frontend" / "templates"))


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
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
    log.info("started: provider=%s trigger=%s price=%s %s",
             provider.name, trigger.name, settings.price_uah, settings.currency)
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


app = FastAPI(title="Photobooth Automation", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "frontend" / "static")), name="static")


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse("/kiosk")


@app.get("/kiosk", response_class=HTMLResponse)
async def kiosk(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "kiosk.html",
        {
            "price": settings.price_uah,
            "currency": settings.currency,
            "shots": settings.shots,
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
    broadcaster: Broadcaster = app.state.broadcaster
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
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/health")
async def health() -> dict:
    controller: Controller = app.state.controller
    trigger = app.state.trigger
    booth_ok = True
    with contextlib.suppress(Exception):
        booth_ok = await trigger.healthy()
    return {**controller.health(), "booth_healthy": booth_ok}


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """Monobank webhook — used only as an accelerator; the poller is authoritative.

    TODO(monobank): verify the X-Sign header (ECDSA over the raw body using the
    public key from GET /api/merchant/pubkey) before trusting this at all.
    """
    controller: Controller = app.state.controller
    with contextlib.suppress(Exception):
        body = await request.json()
        log.info("webhook: %s", body)
    controller.notify_payment_hint()
    return Response(status_code=200)


# ───────────────────────── mock payment page ─────────────────────────
@app.get("/mock/pay/{invoice_id}", response_class=HTMLResponse)
async def mock_pay(invoice_id: str) -> HTMLResponse:
    provider = app.state.provider
    if not isinstance(provider, MockProvider):
        return HTMLResponse("mock disabled", status_code=404)
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Тестова оплата</title>
<style>
  body {{ font-family: system-ui, sans-serif; background:#111; color:#eee;
         display:flex; min-height:100vh; margin:0; align-items:center; justify-content:center; }}
  .card {{ text-align:center; }}
  button {{ font-size:1.4rem; padding:1rem 2rem; border:0; border-radius:12px;
           background:#22c55e; color:#04210f; font-weight:700; }}
  form {{ margin-top:1.5rem; }}
</style>
<div class="card">
  <h1>Тестова оплата</h1>
  <p>Рахунок <code>{invoice_id}</code> — {settings.price_uah} {settings.currency}</p>
  <form method="post" action="/mock/pay/{invoice_id}">
    <button type="submit">Оплатити (тест)</button>
  </form>
</div>""",
    )


@app.post("/mock/pay/{invoice_id}", response_class=HTMLResponse)
async def mock_pay_confirm(invoice_id: str) -> HTMLResponse:
    provider = app.state.provider
    if not isinstance(provider, MockProvider):
        return HTMLResponse("mock disabled", status_code=404)
    ok = provider.mark_paid(invoice_id)
    controller: Controller = app.state.controller
    controller.notify_payment_hint()
    msg = "Оплачено ✓ Поверніться до фотокабінки" if ok else "Рахунок не знайдено"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Оплата</title>
<style>body{{font-family:system-ui,sans-serif;background:#111;color:#eee;display:flex;
min-height:100vh;margin:0;align-items:center;justify-content:center;font-size:1.5rem}}</style>
<div>{msg}</div>""",
    )
