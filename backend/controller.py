"""The single background task that drives one photobooth session at a time.

Because everything runs in one asyncio task there are no locks to reason about:
the FSM is advanced from exactly one place.
"""
from __future__ import annotations

import asyncio
import logging
import time

import httpx

from . import notify, payment
from .config import VERSION, settings
from .dslrbooth import BoothTrigger
from .payment import PaymentProvider
from .qr import qr_data_uri
from .state import Broadcaster, Session, SessionStore, State, snapshot

log = logging.getLogger("controller")


class Controller:
    def __init__(
        self,
        provider: PaymentProvider,
        trigger: BoothTrigger,
        store: SessionStore,
        broadcaster: Broadcaster,
    ) -> None:
        self.provider = provider
        self.trigger = trigger
        self.store = store
        self.broadcaster = broadcaster
        self.session = Session()
        self.started_at = time.time()
        self._stop = asyncio.Event()
        self._payment_hint = asyncio.Event()
        self._down = False          # acquirer / booth outage
        self._paused = False        # manual pause via /admin
        self._paused_by = ""
        self._summary_marker = time.strftime("%Y-%m-%d")
        self._paper_base = 0        # print count when the current roll went in

    # ───────────────────────── public API ─────────────────────────
    def notify_payment_hint(self) -> None:
        self._payment_hint.set()

    def stop(self) -> None:
        self._stop.set()

    async def pause(self, by: str = "адмін") -> None:
        if self._paused:
            return
        self._paused = True
        self._paused_by = by
        await notify.paused(by)

    async def resume(self, by: str = "адмін") -> None:
        if not self._paused:
            return
        self._paused = False
        await notify.resumed(by)

    async def reset_paper(self) -> None:
        total = await self.store.total_prints()
        self._paper_base = total
        await self.store.set_meta("paper_base", str(total))
        await self.store.set_meta("paper_warned", "0")
        await notify.send("🧻 Лічильник паперу скинуто — новий рулон")

    async def status(self) -> dict:
        since = _today_start()
        s_count, s_rev, s_prints = await self.store.stats_since(since)
        return {
            "booth_id": settings.booth_id,
            "booth_name": settings.booth_name,
            "version": VERSION,
            "state": self.session.state.value,
            "provider": self.provider.name,
            "trigger": self.trigger.name,
            "session_id": self.session.id,
            "down": self._down,
            "paused": self._paused,
            "uptime_sec": round(time.time() - self.started_at),
            "today": {"sessions": s_count, "revenue": s_rev, "prints": s_prints},
            "paper_left": await self._paper_left(),
            "last_error": self.session.error or None,
        }

    def health(self) -> dict:
        # kept for backwards-compat with the old /health shape
        return {
            "state": self.session.state.value,
            "provider": self.provider.name,
            "trigger": self.trigger.name,
            "session_id": self.session.id,
            "down": self._down,
            "paused": self._paused,
            "booth_id": settings.booth_id,
            "version": VERSION,
        }

    async def run(self) -> None:
        self._paper_base = int(await self.store.get_meta("paper_base", "0") or 0)
        await notify.startup(VERSION, self.provider.name, self.trigger.name)
        await self._reconcile_on_start()
        self.session.new_cycle()
        self._publish()
        hb = asyncio.create_task(self._heartbeat_loop(), name="heartbeat")
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("controller tick failed")
                    await self._sleep(2)
        finally:
            hb.cancel()
        log.info("controller stopped")

    # ───────────────────────── loop body ─────────────────────────
    async def _tick(self) -> None:
        st = self.session.state

        if self._paused:
            if st != State.OUT_OF_SERVICE:
                self.session.state = State.OUT_OF_SERVICE
                self.session.error = f"Пауза ({self._paused_by})"
                self._publish()
            await self._sleep(3)
            return

        if st == State.IDLE:
            ok = await self._create_invoice()
            if ok and self._down:
                self._down = False
                await notify.back_online()
            elif not ok and not self._down:
                self._down = True
                await notify.out_of_service(self.session.error)
        elif st == State.AWAITING_PAYMENT:
            await self._await_payment()
        elif st == State.PAID:
            await self._run_session()
        elif st == State.DONE:
            await self._sleep(settings.done_duration_sec)
            self.session.new_cycle()
            self._publish()
        elif st == State.REFUNDED:
            await self._sleep(8)
            self.session.new_cycle()
            self._publish()
        elif st == State.OUT_OF_SERVICE:
            await self._sleep(10)
            self.session.new_cycle()  # -> IDLE, retry
        await self._maybe_daily_summary()

    # ───────────────────────── steps ─────────────────────────
    async def _create_invoice(self) -> bool:
        try:
            inv = await self.provider.create_invoice(
                settings.price_uah, reference=self.session.id
            )
        except Exception as exc:
            log.error("create_invoice failed: %s", exc)
            self.session.state = State.OUT_OF_SERVICE
            self.session.error = f"Платіжний сервіс недоступний: {exc}"
            self._publish()
            return False
        self.session.invoice_id = inv.id
        self.session.pay_url = inv.pay_url
        self.session.amount_uah = inv.amount_uah
        self.session.state = State.AWAITING_PAYMENT
        self.session.phase_deadline = time.time() + settings.invoice_ttl_sec
        await self.store.save(self.session)
        self._publish()
        return True

    async def _await_payment(self) -> None:
        deadline = self.session.phase_deadline or (time.time() + settings.invoice_ttl_sec)
        while time.time() < deadline and not self._stop.is_set() and not self._paused:
            self._payment_hint.clear()
            try:
                status = await self.provider.get_status(self.session.invoice_id)
            except Exception as exc:
                log.warning("status poll error: %s", exc)
                status = payment.PROCESSING

            if status == payment.SUCCESS:
                self.session.state = State.PAID
                self.session.paid_at = time.time()
                self.session.phase_deadline = None
                await self.store.save(self.session)
                self._publish()
                return
            if status in (payment.FAILURE, payment.EXPIRED, payment.REVERSED):
                log.info("invoice %s ended as %s", self.session.invoice_id, status)
                break

            try:
                await asyncio.wait_for(
                    self._payment_hint.wait(), timeout=settings.poll_interval_sec
                )
            except asyncio.TimeoutError:
                pass

        self.session.new_cycle()
        self._publish()

    async def _run_session(self) -> None:
        await notify.payment_received(self.session.amount_uah, self.session.id)

        if not await self._trigger_with_retries():
            await self._refund("Кабінка не відповідає (камера або ПЗ)")
            return

        await self._enter(State.SHOOTING, settings.session_duration_sec)
        await self._sleep(settings.session_duration_sec)

        await self._enter(State.PRINTING, settings.print_duration_sec)
        await self._sleep(settings.print_duration_sec)

        self.session.finished_at = time.time()
        await self._enter(State.DONE, settings.done_duration_sec)
        await self.store.save(self.session, prints=1)
        await self._after_print()

    async def _trigger_with_retries(self) -> bool:
        for attempt in range(1, settings.trigger_retries + 1):
            self.session.trigger_attempts = attempt
            try:
                if await self.trigger.start():
                    return True
            except Exception as exc:
                log.error("trigger raised: %s", exc)
            log.warning("booth trigger %d/%d failed", attempt, settings.trigger_retries)
            if attempt < settings.trigger_retries:
                await self._sleep(settings.trigger_retry_pause_sec)
        return False

    async def _refund(self, reason: str) -> None:
        self.session.error = reason
        await self._enter(State.REFUNDING, None)
        ok = False
        try:
            ok = await self.provider.refund(
                self.session.invoice_id, self.session.amount_uah
            )
        except Exception as exc:
            log.error("refund failed: %s", exc)
        await notify.session_failed(self.session.amount_uah, self.session.id, reason)
        await notify.refund_result(self.session.id, ok)

        self.session.finished_at = time.time()
        self.session.state = State.REFUNDED
        self.session.phase_deadline = time.time() + 8
        await self.store.save(self.session)
        self._publish()

    async def _reconcile_on_start(self) -> None:
        last = await self.store.load_last()
        if not last:
            return
        stuck = {State.PAID.value, State.SHOOTING.value, State.PRINTING.value,
                 State.REFUNDING.value}
        if last["state"] not in stuck:
            return
        log.warning("interrupted session %s in %s — refunding", last["id"], last["state"])
        sid, inv, amount = last["id"], last["invoice_id"], last["amount_uah"]
        ok = False
        try:
            ok = await self.provider.refund(inv, amount)
        except Exception as exc:
            log.error("reconcile refund failed: %s", exc)
        await notify.send(
            f"♻️ Після перезапуску знайдено незавершену сесію <code>{sid}</code> "
            f"({last['state']}). Повернення {amount} {settings.currency}: "
            f"{'успішно' if ok else 'НЕ ВДАЛОСЯ — перевір вручну'}"
        )
        closed = Session(
            id=sid, invoice_id=inv, amount_uah=amount, state=State.REFUNDED,
            created_at=last["created_at"], finished_at=time.time(),
            error="reconciled after restart",
        )
        await self.store.save(closed)

    # ───────────────────────── paper ─────────────────────────
    async def _paper_left(self) -> int | None:
        if settings.paper_capacity <= 0:
            return None
        used = await self.store.total_prints() - self._paper_base
        return max(0, settings.paper_capacity - used)

    async def _after_print(self) -> None:
        left = await self._paper_left()
        if left is None:
            return
        warned = await self.store.get_meta("paper_warned", "0") == "1"
        if left <= settings.paper_warn_prints_left and not warned:
            await self.store.set_meta("paper_warned", "1")
            await notify.paper_low(left)

    # ───────────────────────── daily summary ─────────────────────────
    async def _maybe_daily_summary(self) -> None:
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if today == self._summary_marker or now.tm_hour < settings.daily_summary_hour:
            return
        self._summary_marker = today
        count, revenue, prints = await self.store.stats_since(time.time() - 24 * 3600)
        await notify.daily_summary(count, revenue, prints, await self._paper_left())

    # ───────────────────────── heartbeat ─────────────────────────
    async def _heartbeat_loop(self) -> None:
        if not settings.heartbeat_url:
            return
        while not self._stop.is_set():
            try:
                payload = await self.status()
                payload["ts"] = time.time()
                async with httpx.AsyncClient(timeout=8.0) as client:
                    await client.post(settings.heartbeat_url, json=payload)
            except Exception as exc:
                log.debug("heartbeat failed: %s", exc)
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.heartbeat_interval_sec
                )
            except asyncio.TimeoutError:
                pass

    # ───────────────────────── helpers ─────────────────────────
    async def _enter(self, state: State, duration: float | None) -> None:
        self.session.state = state
        self.session.phase_deadline = (time.time() + duration) if duration else None
        await self.store.save(self.session)
        self._publish()

    async def _sleep(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass

    def _publish(self) -> None:
        snap = snapshot(self.session)
        if self.session.state == State.AWAITING_PAYMENT and self.session.pay_url:
            target = self.session.pay_url
            if target.startswith("/") and settings.public_base_url:
                target = settings.public_base_url.rstrip("/") + target
            snap["qr_data_uri"] = qr_data_uri(target)
        self.broadcaster.publish(snap)


def _today_start() -> float:
    t = time.localtime()
    return time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
