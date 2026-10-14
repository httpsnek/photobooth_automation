"""Regression smoke tests — run before pushing an update to the fleet.

    pip install -r requirements-dev.txt
    pytest -q
"""
import asyncio
import contextlib
import os
import time

import pytest

os.environ.update(
    PAYMENT_PROVIDER="mock",
    FAKE_TRIGGER_OK="1",
    BOOTH_ID="booth-pytest",
    SESSION_DURATION_SEC="0.02",
    PRINT_DURATION_SEC="0.02",
    DONE_DURATION_SEC="0.02",
    PAPER_CAPACITY="5",
    PAPER_WARN_PRINTS_LEFT="3",
)

from backend import config, notify  # noqa: E402
from backend.controller import Controller  # noqa: E402
from backend.dslrbooth import FakeTrigger  # noqa: E402
from backend.payment import MockProvider, PaymentError  # noqa: E402
from backend.state import Broadcaster, Session, SessionStore, State  # noqa: E402

_real_notify_send = notify.send

pytestmark = pytest.mark.asyncio


class SpyBroadcaster(Broadcaster):
    """Records every state the controller publishes (transitions can happen
    several-per-tick, so the tick loop alone can't see them all)."""

    def __init__(self):
        super().__init__()
        self.published = []

    def publish(self, snap):
        self.published.append(snap["state"])
        super().publish(snap)


@pytest.fixture()
async def store(tmp_path):
    s = SessionStore(str(tmp_path / "t.db"))
    await s.init()
    return s


@pytest.fixture(autouse=True)
def _mute_telegram(monkeypatch):
    async def fake_send(_text):
        return None
    monkeypatch.setattr(notify, "send", fake_send)


@pytest.fixture(autouse=True)
def _fast_retries():
    object.__setattr__(config.settings, "trigger_retries", 1)
    object.__setattr__(config.settings, "trigger_retry_pause_sec", 0.01)


async def _drive(ctrl, provider, *, pay=True, ticks=60):
    for _ in range(ticks):
        if pay and ctrl.session.state == State.AWAITING_PAYMENT:
            provider.mark_paid(ctrl.session.invoice_id)
        await ctrl._tick()
        if ctrl.session.state in (State.DONE, State.REFUNDED):
            break
    return ctrl.broadcaster.published


async def test_happy_path(store):
    provider = MockProvider()
    ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
    published = await _drive(ctrl, provider)
    assert {"PAID", "SHOOTING", "PRINTING", "DONE"}.issubset(set(published))
    assert await store.total_prints() == 1


async def test_trigger_failure_refunds(store):
    class Dead(FakeTrigger):
        async def start(self):
            return False

    provider = MockProvider()
    ctrl = Controller(provider, Dead(), store, SpyBroadcaster())
    published = await _drive(ctrl, provider)
    assert "REFUNDED" in published
    assert "SHOOTING" not in published
    assert await store.total_prints() == 0


async def test_acquirer_outage_goes_out_of_service(store):
    provider = MockProvider()
    object.__setattr__(config.settings, "mock_fail", True)
    try:
        ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
        await ctrl._tick()
        assert ctrl.session.state == State.OUT_OF_SERVICE
    finally:
        object.__setattr__(config.settings, "mock_fail", False)


async def test_store_stats_and_meta(store):
    import time
    now = time.time()
    await store.save(
        Session(state=State.DONE, amount_uah=150, created_at=now, finished_at=now),
        prints=1,
    )
    assert await store.stats_since(now - 10) == (1, 150, 1)
    await store.set_meta("paper_base", "42")
    assert await store.get_meta("paper_base") == "42"


@contextlib.contextmanager
def _override(**kw):
    old = {k: getattr(config.settings, k) for k in kw}
    for k, v in kw.items():
        object.__setattr__(config.settings, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            object.__setattr__(config.settings, k, v)


async def _to_paid(ctrl, provider):
    await ctrl._tick()                              # IDLE -> AWAITING_PAYMENT
    provider.mark_paid(ctrl.session.invoice_id)
    await ctrl._tick()                              # AWAITING_PAYMENT -> PAID


async def test_event_driven_session(store):
    with _override(dslrbooth_events_enabled=True, get_ready_sec=0.01,
                   print_duration_sec=0.02, done_duration_sec=0.02):
        provider = MockProvider()
        ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
        await _to_paid(ctrl, provider)

        async def feed():
            await asyncio.sleep(0.05)
            await ctrl.on_booth_event("shoot_begin")
            for i in range(4):
                await ctrl.on_booth_event("shot_countdown", {"seconds": 0.01})
                await ctrl.on_booth_event("shot_capture")
                await ctrl.on_booth_event("shot_saved", {"file": f"{i}.jpg"})
            await ctrl.on_booth_event("processing")
            await ctrl.on_booth_event("printing", {"copies": 1})
            await ctrl.on_booth_event("session_done")

        await asyncio.gather(ctrl._tick(), feed())  # PAID -> SHOOTING -> PRINTING -> DONE

        published = ctrl.broadcaster.published
        assert {"SHOOTING", "PRINTING", "DONE"}.issubset(set(published))
        assert ctrl.session.state == State.DONE
        assert await store.total_prints() == 1


async def test_event_watchdog_refunds(store):
    with _override(dslrbooth_events_enabled=True, get_ready_sec=0.01,
                   booth_watchdog_sec=0.05):
        provider = MockProvider()
        ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
        await _to_paid(ctrl, provider)
        await ctrl._tick()   # PAID -> SHOOTING, no events arrive -> watchdog -> refund
        assert ctrl.session.state == State.REFUNDED
        assert "SHOOTING" in ctrl.broadcaster.published
        assert await store.total_prints() == 0


async def test_booth_error_event_refunds(store):
    from backend.booth_events import BoothEventRouter

    with _override(dslrbooth_events_enabled=True, get_ready_sec=0.01):
        provider = MockProvider()
        ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
        router = BoothEventRouter(ctrl)
        await _to_paid(ctrl, provider)

        async def feed():
            await asyncio.sleep(0.05)
            await router.handle("session_start", {"param1": "Print"})
            await router.handle("error", {"param1": "camera disconnected"})

        await asyncio.gather(ctrl._tick(), feed())
        assert ctrl.session.state == State.REFUNDED
        assert await store.total_prints() == 0


# ─────────────── preflight / health gate (🔴3 + 🟠5) ───────────────
class Unhealthy(FakeTrigger):
    async def healthy(self):
        return False


async def test_preflight_blocks_unhealthy_booth(store):
    ctrl = Controller(MockProvider(), Unhealthy(), store, SpyBroadcaster())
    assert "dslrBooth" in await ctrl._preflight()
    ctrl._stop.set()  # makes the internal 10s sleep return at once
    await ctrl._tick()
    assert "OUT_OF_SERVICE" in ctrl.broadcaster.published
    assert "AWAITING_PAYMENT" not in ctrl.broadcaster.published
    assert ctrl.session.invoice_id == ""


async def test_preflight_blocks_when_paper_out(store):
    ctrl = Controller(MockProvider(), FakeTrigger(), store, SpyBroadcaster())
    for _ in range(5):  # PAPER_CAPACITY=5 in the test env
        await store.save(Session(state=State.DONE, created_at=1, finished_at=1), prints=1)
    assert "папір" in (await ctrl._preflight()).lower()


# ─────────────── stuck-session recovery (🔴1) ───────────────
async def test_stuck_session_recovers_and_refunds(store):
    provider = MockProvider()
    ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
    with _override(dslrbooth_events_enabled=False):
        await _to_paid(ctrl, provider)

        async def boom():
            ctrl.session.state = State.SHOOTING
            raise RuntimeError("disk full mid-session")
        ctrl._shoot_by_timers = boom

        with pytest.raises(RuntimeError):
            await ctrl._tick()                 # PAID -> run_session -> boom
        assert ctrl.session.state == State.SHOOTING   # stranded

        await ctrl._tick()                     # SHOOTING branch -> recovery
        assert "REFUNDED" in ctrl.broadcaster.published
        assert await store.total_prints() == 0


# ─────────────── idempotency / duplicate trigger (🟡7) ───────────────
async def test_invoice_never_triggers_camera_twice(store):
    fired = []

    class Counting(FakeTrigger):
        async def start(self):
            fired.append(1)
            return True

    provider = MockProvider()
    ctrl = Controller(provider, Counting(), store, SpyBroadcaster())
    with _override(dslrbooth_events_enabled=False):
        await _to_paid(ctrl, provider)
        await ctrl._tick()                     # full session
        assert len(fired) == 1

        # a glitch re-presents the same paid invoice
        ctrl.session.state = State.PAID
        await ctrl._run_session()
        assert len(fired) == 1                 # in-memory guard held

        # and the persistent guard: a terminal row for the invoice, other id
        await store.save(Session(id="ghost", invoice_id="inv-x",
                                 state=State.DONE, created_at=1))
        ctrl._triggered_invoice = ""
        ctrl.session = Session(invoice_id="inv-x", state=State.PAID)
        await ctrl._run_session()
        assert len(fired) == 1


# ─────────────── late payment after TTL (🔴2) ───────────────
async def test_late_payment_is_accepted(store):
    provider = MockProvider()
    ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
    await ctrl._tick()                                # IDLE -> AWAITING
    inv = ctrl.session.invoice_id
    ctrl.session.phase_deadline = time.time() - 1     # TTL already blown
    provider.mark_paid(inv)                           # money landed at the buzzer
    await ctrl._tick()                                # loop skipped → final check → PAID
    assert ctrl.session.state == State.PAID
    assert await store.pending_count() == 0           # not treated as abandoned


async def test_abandoned_invoice_is_watched(store):
    provider = MockProvider()
    ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
    await ctrl._tick()                                # IDLE -> AWAITING
    inv = ctrl.session.invoice_id
    ctrl.session.phase_deadline = time.time() - 1     # nobody paid
    await ctrl._tick()                                # final check inconclusive → watch queued
    assert ctrl.session.state == State.IDLE
    assert await store.pending_count() == 1

    # late payment arrives while we're watching → auto-refund path
    provider.mark_paid(inv)
    with _override(refund_retry_base_sec=0.01):
        for _ in range(6):
            await ctrl._process_pending()
            if await store.pending_count() == 0:
                break
            await asyncio.sleep(0.05)
    assert await store.pending_count() == 0


# ─────────────── refund dead-letter queue (🔴2 + 🟠6) ───────────────
async def test_failed_refund_is_queued_and_retried(store):
    class Flaky(MockProvider):
        fails_left = 2

        async def refund(self, invoice_id, amount_uah):
            if self.fails_left > 0:
                self.fails_left -= 1
                raise PaymentError("acquirer unreachable")
            return await super().refund(invoice_id, amount_uah)

    with _override(dslrbooth_events_enabled=False, refund_retry_base_sec=0.01):
        provider = Flaky()
        ctrl = Controller(provider, _Dead(), store, SpyBroadcaster())
        await _to_paid(ctrl, provider)
        await ctrl._tick()                     # trigger fails -> _refund -> refund() raises -> queued
        assert ctrl.session.state == State.REFUNDED
        assert await store.pending_count() == 1

        for _ in range(6):
            await ctrl._process_pending()
            if await store.pending_count() == 0:
                break
            await asyncio.sleep(0.08)
        assert await store.pending_count() == 0


class _Dead(FakeTrigger):
    async def start(self):
        return False


async def test_pause_blocks_sessions(store):
    provider = MockProvider()
    ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
    await ctrl.pause("test")
    await ctrl._tick()
    assert ctrl.session.state == State.OUT_OF_SERVICE
    await ctrl.resume("test")
    assert not ctrl._paused


# ── who owns the touch screen ─────────────────────────────────────────────
# One monitor, two fullscreen apps: dslrBooth must come forward for the shoot
# (its own live view / countdown / retake) and our kiosk must come back the
# instant printing starts — and on every error path, or the guest is left
# staring at dslrBooth with no idea what happened.

@contextlib.contextmanager
def _spy_windows():
    from backend import winfocus
    calls: list[str] = []
    orig = winfocus.raise_booth, winfocus.raise_kiosk

    async def booth():
        calls.append("booth")
        return True

    async def kiosk():
        calls.append("kiosk")
        return True

    winfocus.raise_booth, winfocus.raise_kiosk = booth, kiosk
    try:
        yield calls
    finally:
        winfocus.raise_booth, winfocus.raise_kiosk = orig


async def test_screen_handoff_booth_then_kiosk(store):
    with _override(dslrbooth_events_enabled=True, get_ready_sec=0.01,
                   print_duration_sec=0.02, done_duration_sec=0.02), \
         _spy_windows() as calls:
        provider = MockProvider()
        ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
        await _to_paid(ctrl, provider)

        async def feed():
            await asyncio.sleep(0.05)
            await ctrl.on_booth_event("shoot_begin")
            for i in range(4):
                await ctrl.on_booth_event("shot_countdown", {"seconds": 0.01})
                await ctrl.on_booth_event("shot_capture")
                await ctrl.on_booth_event("shot_saved", {"file": f"{i}.jpg"})
            await ctrl.on_booth_event("processing")
            await ctrl.on_booth_event("printing", {"copies": 1})
            await ctrl.on_booth_event("session_done")

        await asyncio.gather(ctrl._tick(), feed())

        assert calls and calls[0] == "kiosk", "a fresh QR must be put on our screen"
        assert "booth" in calls, "dslrBooth must own the screen for the shoot"
        i = calls.index("booth")
        assert "kiosk" in calls[i + 1:], "our kiosk never came back after the shoot"
        assert calls[-1] == "kiosk", "the session must end on our screen"
        assert ctrl.session.state == State.DONE


async def test_screen_returns_to_kiosk_on_refund(store):
    with _spy_windows() as calls:
        provider = MockProvider()
        ctrl = Controller(provider, _Dead(), store, SpyBroadcaster())
        await _to_paid(ctrl, provider)
        await ctrl._tick()          # trigger fails -> refund

        assert ctrl.session.state == State.REFUNDED
        assert calls[-1] == "kiosk", "error screen must be visible to the guest"


# ══════════════════ what goes wrong on a real booth ══════════════════════
# Runtime behaviour when the client's setup is subtly wrong. Nothing may
# crash, and nothing may keep taking money from guests it cannot serve.

class _FlakyThenOk(FakeTrigger):
    def __init__(self, fails):
        self.fails = fails

    async def start(self):
        if self.fails:
            self.fails -= 1
            return False
        return True


async def _fail_one_session(ctrl, provider):
    ctrl.session.new_cycle()
    await _to_paid(ctrl, provider)
    await ctrl._tick()
    assert ctrl.session.state == State.REFUNDED


async def test_breaker_stops_selling_after_repeated_booth_failures(store):
    # wrong API password / camera off: every session fails. Without the
    # breaker each next guest pays, waits, and gets refunded — forever.
    provider = MockProvider()
    ctrl = Controller(provider, _Dead(), store, SpyBroadcaster())
    await _fail_one_session(ctrl, provider)
    assert await ctrl._preflight() == "", "one failure must not stop the booth"
    await _fail_one_session(ctrl, provider)
    reason = await ctrl._preflight()
    assert "поспіль" in reason, reason

    ctrl.session.new_cycle()
    ctrl._stop.set()               # make the out-of-service sleep return at once
    invoices_before = len(provider._invoices)
    ctrl.broadcaster.published.clear()
    await ctrl._tick()
    assert "OUT_OF_SERVICE" in ctrl.broadcaster.published
    assert "AWAITING_PAYMENT" not in ctrl.broadcaster.published
    assert len(provider._invoices) == invoices_before, "no QR while the breaker is open"

    await ctrl.resume("test")
    assert await ctrl._preflight() == "", "/admin/resume must clear the breaker"


async def test_successful_session_resets_breaker(store):
    with _override(print_duration_sec=0.01, done_duration_sec=0.01):
        provider = MockProvider()
        ctrl = Controller(provider, _FlakyThenOk(fails=1), store, SpyBroadcaster())
        await _fail_one_session(ctrl, provider)
        assert ctrl._booth_failures == 1
        ctrl.session.new_cycle()
        await _drive(ctrl, provider)
        assert ctrl.session.state == State.DONE
        assert ctrl._booth_failures == 0


async def test_no_events_ever_names_the_triggers_url(store):
    # client forgot to paste the Triggers URL into dslrBooth: REST starts the
    # session but no event ever arrives. The refund reason must say why.
    with _override(dslrbooth_events_enabled=True, get_ready_sec=0.0, booth_watchdog_sec=0.05):
        provider = MockProvider()
        ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
        ctrl._events_seen = False
        await _to_paid(ctrl, provider)
        await ctrl._tick()
        assert ctrl.session.state == State.REFUNDED
        assert "Triggers URL" in ctrl.session.error


async def test_unconfigured_provider_goes_out_of_service_not_crash(store):
    from backend.payment import UnconfiguredProvider
    ctrl = Controller(UnconfiguredProvider("BANK_TOKEN порожній у .env"), FakeTrigger(),
                      store, SpyBroadcaster())
    assert "BANK_TOKEN" in await ctrl._preflight(), "the reason must reach /status and Telegram"
    ctrl._stop.set()
    await ctrl._tick()
    assert "OUT_OF_SERVICE" in ctrl.broadcaster.published
    assert "AWAITING_PAYMENT" not in ctrl.broadcaster.published


async def test_rest_wrong_password_gives_a_hint():
    import httpx
    from backend.dslrbooth import RestTrigger
    trig = RestTrigger()
    trig._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(401, text="bad password")))
    assert await trig.start() is False
    assert "DSLRBOOTH_API_PASSWORD" in trig.last_error
    await trig.aclose()


async def test_rest_health_probe_never_calls_take_photo():
    # the probe used to GET take_photo?mode=status — one firmware quirk away
    # from firing a free session on every check
    from backend.dslrbooth import RestTrigger
    received = []

    async def on_conn(reader, writer):
        received.append(await reader.read(1024))
        writer.close()

    server = await asyncio.start_server(on_conn, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    with _override(dslrbooth_api_url=f"http://127.0.0.1:{port}/api/take_photo"):
        trig = RestTrigger()
        assert await trig.healthy() is True
        await asyncio.sleep(0.05)
        assert all(not chunk for chunk in received), f"probe sent HTTP: {received}"
        server.close()
        await server.wait_closed()
        assert await trig.healthy() is False
        assert "1500" not in trig.unhealthy_reason() and str(port) in trig.unhealthy_reason()
        await trig.aclose()


async def test_telegram_falls_back_to_plain_text(monkeypatch):
    # any interpolated text that breaks the HTML markup must not silence alerts
    import httpx
    sent = []

    def handler(req):
        body = __import__("json").loads(req.content)
        sent.append(body)
        if body.get("parse_mode") == "HTML":
            return httpx.Response(400, text='{"description":"Bad Request: can\'t parse entities"}')
        return httpx.Response(200, text="{}")

    real_client = httpx.AsyncClient
    monkeypatch.setattr(notify.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    with _override(telegram_bot_token="1:x", telegram_chat_id="42"):
        await _real_notify_send("Причина: <Response [401]> & ще щось")
    assert len(sent) == 2 and "parse_mode" not in sent[1]
    assert "<b>" not in sent[1]["text"] and "&amp;" not in sent[1]["text"]


async def test_local_dslrbooth_events_need_no_token():
    # a typo in the hand-typed Triggers URL token used to drop every event
    from starlette.requests import Request
    from backend import app as appmod

    class Router:
        def __init__(self):
            self.calls = []

        async def handle(self, event_type, params):
            self.calls.append(event_type)

    router = Router()
    appmod.app.state.booth_events = router

    def req(host, qs):
        return Request({"type": "http", "method": "GET", "path": "/dslrbooth/event",
                        "query_string": qs.encode(), "headers": [], "client": (host, 5000),
                        "app": appmod.app})

    with _override(dslrbooth_event_token="secret"):
        assert (await appmod.dslrbooth_event(req("127.0.0.1", "event_type=session_start"))).status_code == 200
        assert (await appmod.dslrbooth_event(req("::1", "event_type=printing&token=typo"))).status_code == 200
        assert (await appmod.dslrbooth_event(req("192.168.1.9", "event_type=session_end"))).status_code == 403
        assert (await appmod.dslrbooth_event(
            req("192.168.1.9", "event_type=session_end&token=secret"))).status_code == 200
    assert router.calls == ["session_start", "printing", "session_end"]


async def test_screen_guard_never_fights_a_session_or_an_admin(store, monkeypatch):
    from backend import winfocus
    calls = []

    async def reclaim(_busy):
        calls.append(ctrl.session.state)
        return False

    monkeypatch.setattr(winfocus, "reclaim_from_booth", reclaim)
    ctrl = Controller(MockProvider(), FakeTrigger(), store, SpyBroadcaster())
    with _override(screen_guard_sec=0.01):
        async def run_for(seconds):
            calls.clear()
            ctrl._stop.clear()
            task = asyncio.create_task(ctrl._screen_guard_loop())
            await asyncio.sleep(seconds)
            ctrl._stop.set()
            await task

        ctrl.session.state = State.SHOOTING
        await run_for(0.08)
        assert calls == [], "guard must not touch the screen during a shoot"

        ctrl.session.state = State.AWAITING_PAYMENT
        ctrl._paused = True
        await run_for(0.08)
        assert calls == [], "guard must leave a paused (maintenance) booth alone"

        ctrl._paused = False
        await run_for(0.08)
        assert calls, "idle booth: guard should be checking"


async def test_preflight_lists_every_problem_at_once(store):
    from backend.payment import UnconfiguredProvider

    class Down(FakeTrigger):
        async def healthy(self):
            return False

    ctrl = Controller(UnconfiguredProvider("BANK_TOKEN порожній у .env"), Down(),
                      store, SpyBroadcaster())
    reason = await ctrl._preflight()
    assert "BANK_TOKEN" in reason and "dslrBooth" in reason, reason
    assert reason.index("BANK_TOKEN") < reason.index("dslrBooth"), "config errors first"


async def test_admin_links_open_a_confirm_page_and_get_changes_nothing():
    # the owner opens /admin/paper-reset from a phone (a GET). It used to be
    # POST-only -> 405, so the paper counter could never be reset by link.
    # GET must not act either: Telegram fetches pasted links for previews.
    from starlette.requests import Request
    from backend import app as appmod

    class Ctrl:
        def __init__(self):
            self.calls = []

        async def pause(self, by): self.calls.append("pause")
        async def resume(self, by): self.calls.append("resume")
        async def reset_paper(self): self.calls.append("paper-reset")
        async def status(self): return {"ok": True}

    ctrl = Ctrl()
    appmod.app.state.controller = ctrl

    def req(method, path, qs, accept="text/html"):
        return Request({"type": "http", "method": method, "path": path,
                        "query_string": qs.encode(), "client": ("192.168.1.5", 1),
                        "headers": [(b"accept", accept.encode())], "app": appmod.app})

    with _override(admin_token="tok"):
        page = await appmod.admin_confirm("paper-reset", req("GET", "/admin/paper-reset", "token=tok"))
        assert page.status_code == 200 and b'method="post"' in page.body
        assert ctrl.calls == [], "GET must never perform the action"
        assert (await appmod.admin_confirm("paper-reset", req("GET", "/x", "token=no"))).status_code == 401

        done = await appmod.admin("paper-reset", req("POST", "/admin/paper-reset", "token=tok"))
        assert ctrl.calls == ["paper-reset"] and "скинуто".encode() in done.body
        api = await appmod.admin("pause", req("POST", "/admin/pause", "token=tok", accept="*/*"))
        assert api.media_type == "application/json" and ctrl.calls[-1] == "pause"


async def test_printing_screen_held_even_if_session_end_comes_instantly(store):
    with _override(dslrbooth_events_enabled=True, get_ready_sec=0.0,
                   print_duration_sec=0.4, done_duration_sec=0.01):
        provider = MockProvider()
        ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
        await _to_paid(ctrl, provider)

        async def feed():
            await asyncio.sleep(0.05)
            for k in ("shoot_begin", "shot_countdown", "shot_capture", "shot_saved",
                      "processing", "printing", "session_done"):
                await ctrl.on_booth_event(k, {"seconds": 0.01} if k == "shot_countdown" else {})

        t0 = time.monotonic()
        await asyncio.gather(ctrl._tick(), feed())
        assert ctrl.session.state == State.DONE
        assert time.monotonic() - t0 >= 0.4, "«Друкуємо» must stay up for PRINT_DURATION_SEC"
