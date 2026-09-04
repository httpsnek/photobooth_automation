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
