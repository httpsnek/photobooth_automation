"""Regression smoke tests — run before pushing an update to the fleet.

    pip install -r requirements-dev.txt
    pytest -q
"""
import os

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
from backend.payment import MockProvider  # noqa: E402
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


async def test_pause_blocks_sessions(store):
    provider = MockProvider()
    ctrl = Controller(provider, FakeTrigger(), store, SpyBroadcaster())
    await ctrl.pause("test")
    await ctrl._tick()
    assert ctrl.session.state == State.OUT_OF_SERVICE
    await ctrl.resume("test")
    assert not ctrl._paused
