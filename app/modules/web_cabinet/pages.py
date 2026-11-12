"""Where payment providers send customers after paying: a neutral page that leads back to the cabinet or the bot."""

from __future__ import annotations

from html import escape

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

from app.web.deps import Ctx
from app.web.pages import page

router = APIRouter(tags=["Web cabinet: pages"])


@router.get("/pay/return", response_class=HTMLResponse, include_in_schema=False)
async def payment_return(ctx: Ctx) -> HTMLResponse:
    """Thank-you page; the payment itself is confirmed by the provider in the background."""
    base = (ctx.settings.public_base_url or "").rstrip("/")
    links: list[str] = []
    if ctx.settings.cabinet_enabled and base:
        links.append(f'<p><a href="{escape(base, quote=True)}/cabinet/">Открыть кабинет / Open the web cabinet</a></p>')
    if ctx.bot_username:
        links.append(f'<p><a href="https://t.me/{escape(ctx.bot_username, quote=True)}">Telegram</a></p>')
    body = "<p>Спасибо! Оплата обрабатывается, подписка обновится в течение минуты.</p>"
    body += "<p>Thank you! The payment is being processed; your subscription updates within a minute.</p>"
    return page(escape(ctx.settings.brand_name), body + "".join(links))
