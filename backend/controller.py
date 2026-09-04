"""The single background task that drives one photobooth session at a time.

Because everything runs in one asyncio task there are no locks to reason about:
the FSM is advanced from exactly one place.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time

import httpx

from . import notify, payment, printer
from .config import VERSION, settings
from .dslrbooth import BoothTrigger
from .payment import PaymentProvider
from .qr import qr_data_uri
from .state import Broadcaster, Session, SessionStore, State, snapshot

# states where a paid guest is mid-flow: if the controller ever lands back in
# _tick() with one of these, the session loop died — recover + refund.
_WORK_STATES = {State.PAID, State.SHOOTING, State.PRINTING, State.REFUNDING}

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
        # dslrBooth Trigger events (Pro): the router feeds them here, the
        # session loop consumes them instead of sleeping on fixed timers.
        self._booth_events: asyncio.Queue[tuple[str, dict]] = asyncio.Queue(maxsize=64)
        self._last_booth_event_at = 0.0
        self._triggered_invoice = ""   # in-memory dup guard for _run_session
        self._preflight_block = ""     # last reason the booth refused to sell

    # ───────────────────────── public API ─────────────────────────
    def notify_payment_hint(self) -> None:
        self._payment_hint.set()

    async def on_booth_event(self, kind: str, data: dict | None = None) -> None:
        """Called by BoothEventRouter for each dslrBooth Trigger callback."""
        self._last_booth_event_at = time.time()
        item = (kind, data or {})
        try:
            self._booth_events.put_nowait(item)
        except asyncio.QueueFull:
            # keep the newest — an old backlog is worthless
            try:
                self._booth_events.get_nowait()
            except asyncio.QueueEmpty:
                pass
            with contextlib.suppress(asyncio.QueueFull):
                self._booth_events.put_nowait(item)

    def _drain_booth_events(self) -> None:
        while not self._booth_events.empty():
            try:
                self._booth_events.get_nowait()
            except asyncio.QueueEmpty:
                break

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
            "events": settings.dslrbooth_events_enabled,
            "session_id": self.session.id,
            "down": self._down,
            "paused": self._paused,
            "block_reason": self._preflight_block or None,
            "pending_refunds": await self.store.pending_count(),
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
        bg = [
            asyncio.create_task(self._heartbeat_loop(), name="heartbeat"),
            asyncio.create_task(self._pending_ops_loop(), name="pending-refunds"),
        ]
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("controller tick failed (state=%s)",
                                  self.session.state.value)
                    if self.session.state in _WORK_STATES:
                        await self._recover_stuck_session("Внутрішня помилка сесії")
                    await self._sleep(2)
        finally:
            for t in bg:
                t.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*bg, return_exceptions=True)
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
            block = await self._preflight()
            if block:
                if not self._down or block != self._preflight_block:
                    self._down = True
                    await notify.out_of_service(block)
                self._preflight_block = block
                self.session.error = block
                self.session.state = State.OUT_OF_SERVICE
                self._publish()
                await self._sleep(10)
                self.session.new_cycle()
                return
            self._preflight_block = ""
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
        elif st in (State.SHOOTING, State.PRINTING, State.REFUNDING):
            # we only reach here if _run_session returned/crashed without
            # finishing — the guest is stranded mid-flow.
            await self._recover_stuck_session("Сесія зависла")
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

        # TTL expired (or paused/stopped). One last look — a payment may have
        # landed in the final seconds; don't drop it on the floor.
        if not self._stop.is_set() and not self._paused:
            final = payment.PROCESSING
            with contextlib.suppress(Exception):
                final = await self.provider.get_status(self.session.invoice_id)
            if final == payment.SUCCESS:
                log.info("late payment on %s — accepting", self.session.invoice_id)
                self.session.state = State.PAID
                self.session.paid_at = time.time()
                self.session.phase_deadline = None
                await self.store.save(self.session)
                self._publish()
                return
            if final not in (payment.FAILURE, payment.EXPIRED, payment.REVERSED):
                # inconclusive — keep polling this invoice in the background for
                # a few minutes; if it turns out paid the guest is gone → refund.
                await self.store.enqueue_refund(
                    invoice_id=self.session.invoice_id,
                    session_id=self.session.id,
                    amount_uah=self.session.amount_uah or settings.price_uah,
                    reason="інвойс покинуто — стежимо за пізньою оплатою",
                    kind="watch",
                )

        self.session.new_cycle()
        self._publish()

    async def _run_session(self) -> None:
        inv = self.session.invoice_id

        # idempotency: this invoice already drove a session to a terminal
        # state (dup PAID after a crash/recovery) — do NOT trigger again.
        if self._triggered_invoice == inv or await self.store.seen_invoice(
            inv, exclude_id=self.session.id
        ):
            log.warning("invoice %s already handled — skipping duplicate session", inv)
            self.session.new_cycle()
            self._publish()
            return

        await notify.payment_received(self.session.amount_uah, self.session.id)
        self._drain_booth_events()

        if not await self._trigger_with_retries():
            await self._refund("Кабінка не відповідає (камера або ПЗ)")
            return
        self._triggered_invoice = inv   # camera fired — never re-trigger this one

        if settings.dslrbooth_events_enabled:
            await self._shoot_by_events()
        else:
            await self._shoot_by_timers()

    # ── event-driven: screens follow real dslrBooth Trigger callbacks ──
    async def _shoot_by_events(self) -> None:
        sid = self.session.id
        n = settings.shots
        self.session.shot = 0
        self.session.shoot_phase = "get_ready"
        await self._enter(State.SHOOTING, None)
        # a beat to look up, even if session_start is instant
        await self._sleep(settings.get_ready_sec)

        printing_seen = False
        while not self._stop.is_set():
            try:
                kind, data = await asyncio.wait_for(
                    self._booth_events.get(), timeout=settings.booth_watchdog_sec
                )
            except asyncio.TimeoutError:
                if self.session.shot > 0:
                    await notify.send(
                        f"⚠️ Сесія <code>{sid}</code>: кабінка не надіслала <code>session_end</code> "
                        f"(зроблено кадрів: {self.session.shot}). Закрито як успішну — перевір друк."
                    )
                    break
                await self._refund("Кабінка не почала зйомку (тайм-аут)")
                return

            # once printing has started, stray shoot events are ignored
            if printing_seen and kind in {
                "shoot_begin", "shot_countdown", "shot_capture", "shot_saved", "processing"
            }:
                continue

            if kind == "shoot_begin":
                self._shoot_phase("get_ready")
            elif kind == "shot_countdown":
                self.session.shot = min(n, self.session.shot + 1)
                secs = data.get("seconds") or settings.shot_countdown_sec
                self.session.countdown_deadline = time.time() + float(secs)
                self._shoot_phase("counting")
            elif kind == "shot_capture":
                self.session.countdown_deadline = None
                self._shoot_phase("capture")
            elif kind == "shot_saved":
                pass  # progress dots already track self.session.shot
            elif kind == "processing":
                self.session.countdown_deadline = None
                self._shoot_phase("processing")
            elif kind == "printing":
                printing_seen = True
                await self._enter(State.PRINTING, settings.print_duration_sec)
            elif kind == "booth_error":
                await self._refund(
                    f"Кабінка повідомила про помилку: {data.get('detail', '') or 'невідомо'}"
                )
                return
            elif kind == "session_done":
                if self.session.shot == 0:
                    await self._refund("Сесія завершилась без жодного кадру")
                    return
                if not printing_seen:
                    await notify.send(
                        f"⚠️ Сесія <code>{sid}</code> завершилась без події <code>printing</code> "
                        f"— перевір принтер."
                    )
                break
            # any event refreshes liveness; heartbeat/unknown just loop

        await self._finish_session()

    # ── fallback: no Pro events — drive the same sub-phases on a schedule ──
    async def _shoot_by_timers(self) -> None:
        n = settings.shots
        cd = float(settings.shot_countdown_sec)
        # pause between shots (camera review/save) — spread the leftover time but
        # never dwell more than a couple of seconds regardless of the .env value
        slack = settings.session_duration_sec - settings.get_ready_sec - n * cd
        gap = min(2.5, max(0.4, slack / n)) if n else 0.4

        self.session.shot = 0
        self.session.shoot_phase = "get_ready"
        await self._enter(State.SHOOTING, None)
        await self._sleep(settings.get_ready_sec)

        for i in range(1, n + 1):
            if self._stop.is_set():
                return
            self.session.shot = i
            self.session.countdown_deadline = time.time() + cd
            self._shoot_phase("counting")
            await self._sleep(cd)
            self.session.countdown_deadline = None
            self._shoot_phase("capture")
            await self._sleep(gap)

        self._shoot_phase("processing")
        await self._sleep(1.0)

        await self._enter(State.PRINTING, settings.print_duration_sec)
        await self._sleep(settings.print_duration_sec)
        await self._finish_session()

    async def _finish_session(self) -> None:
        self.session.finished_at = time.time()
        self.session.shoot_phase = ""
        self.session.countdown_deadline = None
        await self._enter(State.DONE, settings.done_duration_sec)
        await self.store.save(self.session, prints=1)
        await self._after_print()

    def _shoot_phase(self, phase: str) -> None:
        """Publish a SHOOTING sub-phase change (no DB write — the row stays SHOOTING)."""
        self.session.state = State.SHOOTING
        self.session.shoot_phase = phase
        self.session.phase_deadline = None
        self._publish()

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
        amount = self.session.amount_uah or settings.price_uah
        ok = False
        try:
            ok = await self.provider.refund(self.session.invoice_id, amount)
        except Exception as exc:
            log.error("refund failed: %s", exc)
        await notify.session_failed(amount, self.session.id, reason)
        await notify.refund_result(self.session.id, ok)

        if not ok and self.session.invoice_id:
            # couldn't confirm — hand it to the retry queue so the money still
            # comes back once the acquirer / network recovers.
            await self.store.enqueue_refund(
                invoice_id=self.session.invoice_id,
                session_id=self.session.id,
                amount_uah=amount,
                reason=reason,
                kind="refund",
            )

        self.session.finished_at = time.time()
        self.session.state = State.REFUNDED
        self.session.phase_deadline = time.time() + 8
        await self.store.save(self.session)
        self._publish()

    async def _reconcile_on_start(self) -> None:
        """A crash left a paid session unfinished. Money-first policy:
        PAID/SHOOTING/REFUNDING -> the guest likely got nothing -> refund.
        PRINTING -> shots were taken, the strip probably printed -> just alert.
        """
        last = await self.store.load_last()
        if not last:
            return
        st = last["state"]
        refund_states = {State.PAID.value, State.SHOOTING.value, State.REFUNDING.value}
        if st not in refund_states and st != State.PRINTING.value:
            return
        sid, inv, amount = last["id"], last["invoice_id"], last["amount_uah"]

        if st == State.PRINTING.value:
            log.warning("interrupted session %s in PRINTING — alert only", sid)
            await notify.send(
                f"♻️ Після перезапуску: сесія <code>{sid}</code> обірвалась під час друку "
                f"({amount} {settings.currency}). Кадри зроблено — перевір, чи вийшло фото."
            )
            closed_state = State.DONE
            err = "reconciled: crashed during printing"
        else:
            log.warning("interrupted session %s in %s — refunding", sid, st)
            ok = False
            try:
                ok = await self.provider.refund(inv, amount)
            except Exception as exc:
                log.error("reconcile refund failed: %s", exc)
            if not ok and inv:
                # internet/acquirer likely still down right after a reboot —
                # queue it, the retry loop will keep trying.
                await self.store.enqueue_refund(
                    invoice_id=inv, session_id=sid, amount_uah=amount,
                    reason=f"reconcile after restart ({st})", kind="refund",
                )
            await notify.send(
                f"♻️ Після перезапуску знайдено незавершену сесію <code>{sid}</code> "
                f"({st}). Повернення {amount} {settings.currency}: "
                f"{'успішно' if ok else 'у чергу на повтор'}"
            )
            closed_state = State.REFUNDED
            err = "reconciled after restart"

        closed = Session(
            id=sid, invoice_id=inv, amount_uah=amount, state=closed_state,
            created_at=last["created_at"], finished_at=time.time(), error=err,
        )
        await self.store.save(closed, prints=last.get("prints", 0))

    # ─────────────────── preflight / recovery ───────────────────
    async def _preflight(self) -> str:
        """Reasons NOT to sell a session right now (checked before every QR).
        Empty string = good to go."""
        left = await self._paper_left()
        if left is not None and left <= 0:
            return "Закінчився папір — заміни рулон"

        with contextlib.suppress(Exception):
            pr = await printer.printer_problem()
            if pr:
                return pr

        try:
            if not await self.trigger.healthy():
                return "Фотобудка (dslrBooth) не відповідає"
        except Exception as exc:
            log.warning("booth health check errored: %s", exc)

        return ""

    async def _recover_stuck_session(self, reason: str) -> None:
        """The session loop died mid-flow. Refund the guest and get clean."""
        st = self.session.state.value
        inv, sid = self.session.invoice_id, self.session.id
        amount = self.session.amount_uah or settings.price_uah
        log.error("recovering stuck session %s in %s: %s", sid, st, reason)
        with contextlib.suppress(Exception):
            await notify.send(
                f"🚨 Збій сесії <code>{sid}</code> у стані {st} "
                f"({reason}). Автовідновлення + повернення коштів."
            )

        handled = False
        if inv and self.session.state != State.REFUNDED:
            try:
                await self._refund(reason)
                handled = self.session.state == State.REFUNDED
            except Exception:
                log.exception("recovery refund failed")
        # belt-and-suspenders: if _refund itself blew up (e.g. disk), make sure
        # the money is still queued for return.
        if inv and not handled and self.session.state != State.REFUNDED:
            with contextlib.suppress(Exception):
                await self.store.enqueue_refund(
                    invoice_id=inv, session_id=sid, amount_uah=amount,
                    reason=f"recovery: {reason}", kind="refund",
                )

        if self.session.state not in (State.REFUNDED, State.REFUNDING):
            self.session.new_cycle()
            self._publish()

    async def _pending_ops_loop(self) -> None:
        """Dead-letter queue: chase every refund we owe until the acquirer
        confirms 'reversed', and watch abandoned invoices for late payments."""
        while not self._stop.is_set():
            try:
                await self._process_pending()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("pending-refund loop error")
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=settings.pending_ops_interval_sec
                )
            except asyncio.TimeoutError:
                pass

    async def _process_pending(self) -> None:
        now = time.time()
        for row in await self.store.due_pending(now):
            inv = row["invoice_id"]
            amount = int(row["amount_uah"] or settings.price_uah)
            attempts = int(row["attempts"] or 0)

            if row["kind"] == "watch":
                if now - float(row["created_at"]) > settings.watch_ttl_sec:
                    await self.store.drop_pending(inv)
                    continue
                status = payment.PROCESSING
                with contextlib.suppress(Exception):
                    status = await self.provider.get_status(inv)
                if status == payment.SUCCESS:
                    log.warning("late payment on abandoned invoice %s — auto-refund", inv)
                    await self.store.update_pending(
                        inv, kind="refund", attempts=0, next_try=now,
                        reason="пізня оплата, гість пішов — автоповернення",
                    )
                    with contextlib.suppress(Exception):
                        await notify.send(
                            f"↩️ Пізня оплата по покинутому інвойсу <code>{inv}</code> "
                            f"({amount} {settings.currency}) — ставлю на автоповернення."
                        )
                elif status in (payment.FAILURE, payment.EXPIRED, payment.REVERSED):
                    await self.store.drop_pending(inv)
                else:
                    await self.store.update_pending(inv, next_try=now + 20)
                continue

            # kind == "refund"
            ok = False
            with contextlib.suppress(Exception):
                ok = await self.provider.refund(inv, amount)
            confirmed = ok
            if ok:
                with contextlib.suppress(Exception):
                    confirmed = await self.provider.get_status(inv) in (
                        payment.REVERSED, payment.PROCESSING
                    )
            if confirmed:
                await self.store.drop_pending(inv)
                with contextlib.suppress(Exception):
                    await notify.send(
                        f"✅ Повернення коштів по <code>{inv}</code> "
                        f"({amount} {settings.currency}) підтверджено."
                    )
                continue

            attempts += 1
            backoff = min(3600.0, settings.refund_retry_base_sec * (2 ** min(attempts, 8)))
            await self.store.update_pending(inv, attempts=attempts, next_try=now + backoff)
            if attempts == settings.refund_max_attempts:
                with contextlib.suppress(Exception):
                    await notify.send(
                        f"❌ Повернення по <code>{inv}</code> ({amount} {settings.currency}) "
                        f"не вдається {attempts} раз — ПОТРІБНЕ РУЧНЕ ВТРУЧАННЯ. "
                        f"Продовжую спроби."
                    )

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
