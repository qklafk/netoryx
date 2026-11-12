"""QR codes (PNG) for subscription URLs and share links."""

from __future__ import annotations

import asyncio
import io

import qrcode
from qrcode.constants import ERROR_CORRECT_M

# Scale and quiet zone tuned for Telegram photos: crisp on phones, scannable from another screen.
BOX_SIZE = 10
BORDER = 4
# Version 40 with error correction M holds 2331 bytes; anything longer cannot be encoded.
MAX_DATA_BYTES = 2331


def make_qr_png(data: str, *, box_size: int = BOX_SIZE, border: int = BORDER) -> bytes:
    """Render ``data`` as a black-on-white PNG QR code (error correction M, smallest fitting version).

    Raises ``ValueError`` for empty data or data too long for a QR code.
    """
    if not data:
        raise ValueError("QR data must not be empty")
    if len(data.encode()) > MAX_DATA_BYTES:
        raise ValueError(f"QR data too long: {len(data.encode())} bytes (max {MAX_DATA_BYTES})")
    qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M, box_size=box_size, border=border)
    qr.add_data(data)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def make_qr_png_async(data: str, *, box_size: int = BOX_SIZE, border: int = BORDER) -> bytes:
    """:func:`make_qr_png` in a worker thread (image encoding is CPU-bound)."""
    return await asyncio.to_thread(make_qr_png, data, box_size=box_size, border=border)
