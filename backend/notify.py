"""Telegram alerts to the owner. No-op when not configured.

Every message is prefixed with the booth id + name so an owner running a
fleet knows instantly which machine needs attention.
"""
from __future__ import annotations

import html
import logging
import re

import httpx

from .config import settings

log = logging.getLogger("notify")

def esc(v: object) -> str:
    """Escape a value for Telegram HTML (names, reasons, exception text)."""
    return html.escape(str(v), quote=False)


# BOOTH_NAME is typed by the client: "Фото & Кава" unescaped would make
# Telegram reject EVERY message with "can't parse entities".
_TAG = f"🅱️ <b>{esc(settings.booth_name)}</b> · <code>{esc(settings.booth_id)}</code>"


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"</?[a-zA-Z][^>]*>", "", text))


async def send(text: str) -> None:
    body = f"{_TAG}\n{text}"
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        log.info("[telegram off] %s", text.replace("\n", " | "))
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            payload = {
                "chat_id": settings.telegram_chat_id,
                "text": body,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            r = await client.post(url, json=payload)
            if r.status_code == 400 and "parse" in r.text.lower():
                # some interpolated text broke the markup — an alert delivered
                # as plain text beats an alert silently dropped
                payload.pop("parse_mode")
                payload["text"] = _plain(body)
                r = await client.post(url, json=payload)
            if r.status_code != 200:
                log.warning("telegram %s: %s", r.status_code, r.text[:200])
    except Exception as exc:  # never let a notification failure break the flow
        log.warning("telegram send failed: %s", exc)


async def startup(version: str, provider: str, trigger: str) -> None:
    await send(
        f"▶️ Запуск  ·  v{version}\n"
        f"Оплата: {provider}   Тригер: {trigger}   Ціна: {settings.price_uah} {settings.currency}"
    )


async def payment_received(amount_uah: int, session_id: str) -> None:
    await send(f"💰 <b>+{amount_uah} {settings.currency}</b>\nСесія <code>{session_id}</code>")


async def session_failed(amount_uah: int, session_id: str, reason: str) -> None:
    await send(
        f"⚠️ <b>Оплата пройшла, а зйомка — ні</b>\n"
        f"Сесія <code>{session_id}</code>\nПричина: {esc(reason)}\n"
        f"Повертаю {amount_uah} {settings.currency} на картку."
    )


async def refund_result(session_id: str, ok: bool) -> None:
    await send(
        f"{'✅' if ok else '❌'} Повернення коштів по сесії "
        f"<code>{session_id}</code>: {'успішно' if ok else 'НЕ ВДАЛОСЯ — перевір вручну'}"
    )


async def out_of_service(reason: str) -> None:
    await send(f"🛑 <b>Кабінка не працює</b>\n{esc(reason)}")


async def back_online() -> None:
    await send("✅ Кабінка знову працює")


async def paused(by: str = "адмін") -> None:
    await send(f"⏸ Кабінку поставлено на паузу ({by})")


async def resumed(by: str = "адмін") -> None:
    await send(f"▶️ Кабінку знято з паузи ({by})")


async def paper_low(prints_left: int) -> None:
    await send(
        f"🧻 <b>Закінчується папір</b>\n"
        f"Залишилось ≈ <b>{prints_left}</b> відбитків. Час поміняти рулон."
    )


async def daily_summary(count: int, revenue_uah: int, prints: int, paper_left: int | None) -> None:
    lines = [
        "📊 <b>Підсумок за добу</b>",
        f"Сесій: {count}",
        f"Виручка: {revenue_uah} {settings.currency}",
        f"Відбитків: {prints}",
    ]
    if paper_left is not None:
        lines.append(f"Паперу лишилось ≈ {paper_left}")
    await send("\n".join(lines))
