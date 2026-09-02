"""QR code -> data URI, so the kiosk page needs no JS QR library (works offline)."""
from __future__ import annotations

import base64
import io
from functools import lru_cache

import qrcode


@lru_cache(maxsize=8)
def qr_data_uri(text: str) -> str:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=10,
        border=1,  # small quiet zone; the overlay sits on its own white card
    )
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{b64}"
