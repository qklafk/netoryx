"""Fake payment page for local development (``DEV_MODE`` only): pays "test" provider invoices instantly."""

from __future__ import annotations

import logging
import re
from html import escape

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.db.models import Payment
from app.db.session import read_session
from app.services.payments import service as payments_service
from app.utils.money import rub
from app.web.deps import Ctx
from app.web.pages import page

log = logging.getLogger(__name__)

TEST_PROVIDER = "test"
_PAYMENT_ID_RE = re.compile(r"^[0-9a-fA-F-]{1,36}$")

router = APIRouter(include_in_schema=False)


def _not_found() -> HTMLResponse:
    return page("Not found", "<p>No such test payment.</p>", status_code=404)


async def _load(ctx: object, payment_id: str) -> Payment | None:
    if not _PAYMENT_ID_RE.match(payment_id):
        return None
    async with read_session(ctx.session_factory) as session:  # type: ignore[attr-defined]
        payment = await session.get(Payment, payment_id)
    if payment is None or payment.provider != TEST_PROVIDER:
        return None
    return payment


def _bot_link(ctx: object) -> str:
    username = getattr(ctx, "bot_username", "")
    if not username:
        return ""
    url = f"https://t.me/{escape(username)}"
    return f'<p><a href="{url}">Back to the bot</a></p>'


@router.get("/dev/pay/{payment_id}")
async def dev_pay_page(payment_id: str, ctx: Ctx) -> Response:
    if not ctx.settings.dev_mode:
        return _not_found()
    payment = await _load(ctx, payment_id)
    if payment is None:
        return _not_found()
    amount = escape(rub(payment.amount_kop)) + " ₽"
    if payment.status in payments_service.OPEN_STATUSES:
        body = (
            f"<p>Test payment <code>{escape(payment.id)}</code></p><p>Amount: <b>{amount}</b></p>"
            f'<form method="post"><button type="submit">Pay (test)</button></form>'
            "<p>No real money is charged: this page exists only in development mode.</p>"
        )
    else:
        body = f"<p>Amount: <b>{amount}</b></p><p>Status: <b>{escape(str(payment.status))}</b></p>{_bot_link(ctx)}"
    return page("Test payment", body)


@router.post("/dev/pay/{payment_id}")
async def dev_pay(payment_id: str, request: Request, ctx: Ctx) -> Response:
    if not ctx.settings.dev_mode:
        return _not_found()
    payment = await _load(ctx, payment_id)
    if payment is None:
        return _not_found()
    result = await payments_service.credit(
        ctx.session_factory, ctx.settings, payment.id, provider_payment_id=f"test-{payment.id}"
    )
    if result is not None:
        log.info("dev: test payment %s paid", payment.id)
        try:
            await ctx.on_payment_credited(result)
        except Exception:
            log.exception("dev: post-payment hook failed for %s", payment.id)
    return RedirectResponse(request.url.path, status_code=303)
