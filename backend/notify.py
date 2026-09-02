"""Telegram alerts to the owner. No-op when not configured."""
from __future__ import annotations

import logging

import httpx

from .config import settings

log = logging.getLogger("notify")


async def send(text: str) -> None:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("[notify skipped] %s", text)
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            await client.post(
                url,
                json={
                    "chat_id": settings.telegram_chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except Exception as exc:  # never let notification failure break the flow
        log.warning("telegram send failed: %s", exc)


async def payment_received(amount_uah: int, session_id: str) -> None:
    await send(f"💰 <b>+{amount_uah} грн</b>\nСесія <code>{session_id}</code>")


async def session_failed(amount_uah: int, session_id: str, reason: str) -> None:
    await send(
        f"⚠️ <b>Оплата пройшла, а зйомка — ні</b>\n"
        f"Сесія <code>{session_id}</code>\nПричина: {reason}\n"
        f"Кошти {amount_uah} грн повертаю на картку."
    )


async def refund_result(session_id: str, ok: bool) -> None:
    await send(
        f"{'✅' if ok else '❌'} Повернення коштів по сесії "
        f"<code>{session_id}</code>: {'успішно' if ok else 'НЕ ВДАЛОСЯ — перевір вручну'}"
    )


async def out_of_service(reason: str) -> None:
    await send(f"🛑 <b>Кабінка призупинена</b>\n{reason}")


async def back_online() -> None:
    await send("✅ Кабінка знову працює")


async def daily_summary(count: int, revenue_uah: int, prints: int) -> None:
    await send(
        f"📊 <b>Підсумок за добу</b>\n"
        f"Сесій: {count}\nВиручка: {revenue_uah} грн\nВідбитків: {prints}"
    )
