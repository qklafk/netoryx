"""Tests for app.services.qr."""

from __future__ import annotations

import io
import threading

import pytest
import qrcode
from PIL import Image
from qrcode.constants import ERROR_CORRECT_M

from app.services import qr
from app.services.qr import BORDER, BOX_SIZE, MAX_DATA_BYTES, make_qr_png, make_qr_png_async

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SUB_URL = "https://bot.example.com/sub/abc123"


def modules_for(data: str) -> int:
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_M)
    code.add_data(data)
    code.make(fit=True)
    return code.modules_count


def test_png_has_expected_geometry_and_colors() -> None:
    png = make_qr_png(SUB_URL)
    assert png.startswith(PNG_SIGNATURE)
    image = Image.open(io.BytesIO(png))
    side = (modules_for(SUB_URL) + 2 * BORDER) * BOX_SIZE
    assert image.size == (side, side)
    gray = image.convert("L")
    assert gray.getpixel((0, 0)) == 255  # quiet zone is white
    corner = BORDER * BOX_SIZE + 1
    assert gray.getpixel((corner, corner)) == 0  # finder pattern is black


def test_custom_scale() -> None:
    image = Image.open(io.BytesIO(make_qr_png(SUB_URL, box_size=2, border=1)))
    assert image.size[0] == (modules_for(SUB_URL) + 2) * 2


def test_long_share_link_fits() -> None:
    link = "vless://" + "a" * 36 + "@203.0.113.10:443?" + "x=y&" * 150 + "#name"
    assert make_qr_png(link).startswith(PNG_SIGNATURE)


def test_unicode_data() -> None:
    assert make_qr_png("vless://u@h:1#🇩🇪 Германия").startswith(PNG_SIGNATURE)


@pytest.mark.parametrize("data", ["", "x" * (MAX_DATA_BYTES + 1), "ж" * (MAX_DATA_BYTES // 2 + 1)])
def test_rejects_empty_or_oversized(data: str) -> None:
    with pytest.raises(ValueError):
        make_qr_png(data)


def test_max_size_still_encodes() -> None:
    assert make_qr_png("A" * MAX_DATA_BYTES).startswith(PNG_SIGNATURE)


async def test_async_wrapper_runs_in_worker_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    main = threading.get_ident()
    seen: list[int] = []
    original = qr.make_qr_png

    def spy(data: str, **kwargs: int) -> bytes:
        seen.append(threading.get_ident())
        return original(data, **kwargs)

    monkeypatch.setattr(qr, "make_qr_png", spy)
    png = await make_qr_png_async(SUB_URL, box_size=3)
    assert png.startswith(PNG_SIGNATURE)
    assert seen and seen[0] != main
