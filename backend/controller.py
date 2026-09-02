"""The single background task that drives one photobooth session at a time.

Because everything runs in one asyncio task there are no locks to reason about:
the FSM is advanced from exactly one place.
"""
from __future__ import annotations

import asyncio
import logging
import time

from . import notify, payment
from .config import settings
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
        self._stop = asyncio.Event()
        self._payment_hint = asyncio.Event()
        self._down = False  # currently in an acquirer/booth outage
        self._summary_marker = time.strftime("%Y-%m-%d")

    # ───────────────────────── public API ─────────────────────────
    def notify_payment_hint(self) -> None:
        """Called by the webhook route to wake the poller immediately."""
        self._payment_hint.set()

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict:
        return {
            "state": self.session.state.value,
            "provider": self.provider.name,
            "trigger": self.trigger.name,
            "session_id": self.session.id,
            "down": self._down,
        }

    async def run(self) -> None:
        await self._reconcile_on_start()
        self.session.new_cycle()
        self._publish()
        while not self._stop.is_set():
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("controller tick failed")
                await self._sleep(2)
        log.info("controller stopped")

    # ───────────────────────── loop body ─────────────────────────
    async def _tick(self) -> None:
        st = self.session.state
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
            self.session.new_cycle()  # -> IDLE, retry creating an invoice
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
        while time.time() < deadline and not self._stop.is_set():
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

            try:  # sleep poll_interval, but wake early if the webhook pinged us
                await asyncio.wait_for(
                    self._payment_hint.wait(), timeout=settings.poll_interval_sec
                )
            except asyncio.TimeoutError:
                pass

        # expired / failed / TTL -> fresh invoice
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

    async def _trigger_with_retries(self) -> bool:
        for attempt in range(1, settings.trigger_retries + 1):
            self.session.trigger_attempts = attempt
            try:
                if await self.trigger.start():
                    return True
            except Exception as exc:
                log.error("trigger raised: %s", exc)
            log.warning(
                "booth trigger %d/%d failed", attempt, settings.trigger_retries
            )
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
        log.warning("found interrupted session %s in %s — refunding", last["id"], last["state"])
        sid, inv, amount = last["id"], last["invoice_id"], last["amount_uah"]
        ok = False
        try:
            ok = await self.provider.refund(inv, amount)
        except Exception as exc:
            log.error("reconcile refund failed: %s", exc)
        await notify.send(
            f"♻️ Після перезапуску знайдено незавершену сесію <code>{sid}</code> "
            f"({last['state']}). Повернення {amount} грн: "
            f"{'успішно' if ok else 'НЕ ВДАЛОСЯ — перевір вручну'}"
        )
        closed = Session(
            id=sid, invoice_id=inv, amount_uah=amount, state=State.REFUNDED,
            created_at=last["created_at"], finished_at=time.time(),
            error="reconciled after restart",
        )
        await self.store.save(closed)

    async def _maybe_daily_summary(self) -> None:
        now = time.localtime()
        today = time.strftime("%Y-%m-%d", now)
        if today == self._summary_marker or now.tm_hour < settings.daily_summary_hour:
            return
        self._summary_marker = today
        since = time.time() - 24 * 3600
        count, revenue, prints = await self.store.stats_since(since)
        await notify.daily_summary(count, revenue, prints)

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
