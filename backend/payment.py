"""Payment providers behind one interface.

`mock`     - in-memory, for local dev without a real acquirer.
`monobank` - Monobank Acquiring (https://api.monobank.ua/docs/acquiring).

Normalized status vocabulary used by the rest of the app:
    created | processing | success | failure | expired | reversed
"""
from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass, field

import httpx

from .config import settings

log = logging.getLogger("payment")

CREATED = "created"
PROCESSING = "processing"
SUCCESS = "success"
FAILURE = "failure"
EXPIRED = "expired"
REVERSED = "reversed"


@dataclass
class Invoice:
    id: str
    pay_url: str
    amount_uah: int
    created_at: float = field(default_factory=time.time)


class PaymentError(RuntimeError):
    pass


class PaymentProvider:
    name = "base"

    async def create_invoice(self, amount_uah: int, reference: str) -> Invoice:
        raise NotImplementedError

    async def get_status(self, invoice_id: str) -> str:
        raise NotImplementedError

    async def refund(self, invoice_id: str, amount_uah: int) -> bool:
        raise NotImplementedError

    async def verify_webhook(self, body: bytes, x_sign: str) -> bool:
        """True if the webhook body is authentic. Default: cannot verify."""
        return False

    async def aclose(self) -> None:
        pass


# ─────────────────────────── Mock ───────────────────────────
class MockProvider(PaymentProvider):
    name = "mock"

    def __init__(self) -> None:
        self._invoices: dict[str, dict] = {}

    async def create_invoice(self, amount_uah: int, reference: str) -> Invoice:
        if settings.mock_fail:
            raise PaymentError("MOCK_FAIL=1: simulated acquirer outage")
        inv_id = f"mock-{int(time.time() * 1000)}"
        self._invoices[inv_id] = {"status": CREATED, "amount": amount_uah, "ref": reference}
        return Invoice(id=inv_id, pay_url=f"/mock/pay/{inv_id}", amount_uah=amount_uah)

    async def get_status(self, invoice_id: str) -> str:
        return self._invoices.get(invoice_id, {}).get("status", FAILURE)

    async def refund(self, invoice_id: str, amount_uah: int) -> bool:
        if invoice_id in self._invoices:
            self._invoices[invoice_id]["status"] = REVERSED
            return True
        return False

    async def verify_webhook(self, body: bytes, x_sign: str) -> bool:
        return True  # nothing to spoof in dev

    def mark_paid(self, invoice_id: str) -> bool:
        if invoice_id in self._invoices:
            self._invoices[invoice_id]["status"] = SUCCESS
            return True
        return False


# ───────────────────────── Monobank ─────────────────────────
_MONO_STATUS = {
    "created": CREATED,
    "processing": PROCESSING,
    "hold": PROCESSING,
    "success": SUCCESS,
    "failure": FAILURE,
    "expired": EXPIRED,
    "reversed": REVERSED,
}


class MonobankProvider(PaymentProvider):
    name = "monobank"

    def __init__(self) -> None:
        if not settings.bank_token:
            raise PaymentError("BANK_TOKEN is not set")
        self._client = httpx.AsyncClient(
            base_url=settings.monobank_api_base,
            headers={"X-Token": settings.bank_token},
            timeout=httpx.Timeout(10.0),
        )
        self._pubkey_pem: bytes | None = None

    async def create_invoice(self, amount_uah: int, reference: str) -> Invoice:
        # reference is written to the bank statement + echoed in the webhook,
        # so tag it with the booth id — one statement, many booths.
        payload = {
            "amount": amount_uah * 100,  # kopiykas
            "ccy": 980,
            "merchantPaymInfo": {
                "reference": f"{settings.booth_id}/{reference}",
                "destination": f"{settings.booth_name}: фотосесія ({settings.shots} фото)",
                "comment": f"{settings.booth_name} · {settings.booth_id}",
            },
            "validity": settings.invoice_ttl_sec,
            "paymentType": "debit",
        }
        if settings.public_base_url:
            payload["webHookUrl"] = f"{settings.public_base_url.rstrip('/')}/webhook"
        r = await self._client.post("/api/merchant/invoice/create", json=payload)
        if r.status_code != 200:
            raise PaymentError(f"create_invoice {r.status_code}: {r.text}")
        data = r.json()
        return Invoice(id=data["invoiceId"], pay_url=data["pageUrl"], amount_uah=amount_uah)

    async def get_status(self, invoice_id: str) -> str:
        r = await self._client.get(
            "/api/merchant/invoice/status", params={"invoiceId": invoice_id}
        )
        if r.status_code != 200:
            raise PaymentError(f"status {r.status_code}: {r.text}")
        return _MONO_STATUS.get(r.json().get("status", ""), PROCESSING)

    async def refund(self, invoice_id: str, amount_uah: int) -> bool:
        r = await self._client.post(
            "/api/merchant/invoice/cancel", json={"invoiceId": invoice_id}
        )
        if r.status_code != 200:
            raise PaymentError(f"refund {r.status_code}: {r.text}")
        return _MONO_STATUS.get(r.json().get("status", ""), "") in {REVERSED, PROCESSING}

    async def _pubkey(self) -> bytes:
        if self._pubkey_pem is None:
            r = await self._client.get("/api/merchant/pubkey")
            r.raise_for_status()
            self._pubkey_pem = base64.b64decode(r.json()["key"])
        return self._pubkey_pem

    async def verify_webhook(self, body: bytes, x_sign: str) -> bool:
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec

            pub = serialization.load_pem_public_key(await self._pubkey())
            pub.verify(
                base64.b64decode(x_sign), body,
                ec.ECDSA(hashes.SHA256()),
            )
            return True
        except Exception as exc:
            log.warning("webhook signature check failed: %s", exc)
            return False

    async def aclose(self) -> None:
        await self._client.aclose()


def build_provider() -> PaymentProvider:
    if settings.payment_provider == "monobank":
        return MonobankProvider()
    return MockProvider()
