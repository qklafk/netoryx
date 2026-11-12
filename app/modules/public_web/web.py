"""Public HTTP routes: subscription URL, one-tap app import page, gift landing, Remnawave webhook.

* ``GET /sub/{token}`` (+ ``/wl``) - the subscription for VPN apps (base64 body + metadata headers); a browser gets
  a friendly page instead (status, app buttons, QR, Telegram proxy). 404 for unknown tokens; 503 when an active
  subscription has no servers to offer because a panel is down, so apps keep their current list.
* ``GET /open/{app}/{token}`` - opens the app's import deeplink (Telegram buttons cannot use app schemes) with a
  manual button and store links as a fallback.
* ``GET /gift/{code}`` - what a gift link shows outside Telegram.
* ``POST /webhooks/remnawave`` - signed panel events (HMAC of the raw body); each delivery is processed once.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.db.models import Plan, PromoKind, Subscription, User
from app.db.session import read_session
from app.modules.public_web import remnawave_hook
from app.modules.public_web.html import esc, js_string, page
from app.services import deeplinks, kv, promo, subscription_page, subscriptions, tg_proxy, users
from app.services.panels.remnawave import SIGNATURE_HEADER, RemnawaveBackend, RemnawaveWebhookError
from app.services.qr import make_qr_png_async
from app.utils.time import utcnow
from app.web.deps import Ctx
from app.web.errors import ApiError
from app.web.security import client_ip

log = logging.getLogger(__name__)

router = APIRouter(include_in_schema=False)

TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
GIFT_CODE_RE = re.compile(r"^[\w\-]{3,64}$")
WEBHOOK_KEY_PREFIX = "rwhook:"
RETRY_AFTER_SEC = 300
# User agents of VPN apps: they always get the raw subscription, whatever they send in Accept.
APP_AGENTS = (
    "happ",
    "incy",
    "v2raytun",
    "hiddify",
    "streisand",
    "shadowrocket",
    "v2rayng",
    "v2rayn",
    "clash",
    "mihomo",
    "sing-box",
    "singbox",
    "nekobox",
    "nekoray",
    "karing",
    "v2box",
    "foxray",
    "stash",
    "quantumult",
    "surge",
    "loon",
    "throne",
)
NO_STORE = {"Cache-Control": "no-store"}


# --- helpers ---------------------------------------------------------------------------------------------------------


def base_url(ctx: Any, request: Request) -> str:
    return (ctx.settings.public_base_url or str(request.base_url)).rstrip("/")


def pick_lang(ctx: Any, request: Request, preferred: str | None = None) -> str:
    """The user's language if known, else the first supported one from ``Accept-Language``, else the default."""
    if preferred:
        return str(ctx.i18n.normalize(preferred))
    enabled = users.enabled_languages(ctx.settings)
    for part in request.headers.get("accept-language", "").split(","):
        code = users.detect_language(part.split(";", 1)[0])
        if code is not None and code in enabled and code in ctx.i18n.languages:
            return code
    return str(ctx.settings.default_language)


def wants_page(request: Request) -> bool:
    """A person in a browser (not a VPN app fetching its subscription)."""
    if request.method != "GET" or request.query_params.get("raw"):
        return False
    agent = request.headers.get("user-agent", "").lower()
    if not agent.startswith("mozilla/") or any(app in agent for app in APP_AGENTS):
        return False
    return "text/html" in request.headers.get("accept", "").lower()


def _t(ctx: Any, key: str, lang: str, **kw: Any) -> str:
    return str(ctx.i18n.t(key, lang, **kw))


async def _qr_data_url(data: str) -> str | None:
    try:
        png = await make_qr_png_async(data)
    except ValueError:
        return None
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def _not_found_page(ctx: Any, lang: str) -> Response:
    title = _t(ctx, "public_web.not_found.title", lang)
    body = f"<h1>{esc(title)}</h1><p>{esc(_t(ctx, 'public_web.not_found.text', lang))}</p>"
    if ctx.bot_username:
        bot = f"https://t.me/{ctx.bot_username}"
        body += f'<a class="btn main" href="{esc(bot)}">{esc(_t(ctx, "public_web.open_bot", lang))}</a>'
    return page(title, body, lang=lang, status_code=404)


async def _load_user(ctx: Any, token: str) -> tuple[User, Subscription | None] | None:
    async with read_session(ctx.session_factory) as session:
        return await subscriptions.resolve_token(session, token)


# --- subscription ----------------------------------------------------------------------------------------------------


@router.api_route("/sub/{token}", methods=["GET", "HEAD"])
async def subscription(token: str, request: Request, ctx: Ctx) -> Response:
    return await _subscription(ctx, request, token, "all")


@router.api_route("/sub/{token}/wl", methods=["GET", "HEAD"])
async def subscription_whitelist(token: str, request: Request, ctx: Ctx) -> Response:
    return await _subscription(ctx, request, token, "whitelist")


@router.api_route("/sub/{token}/server/{server_id}", methods=["GET", "HEAD"])
async def subscription_server(token: str, server_id: str, request: Request, ctx: Ctx) -> Response:
    if not re.fullmatch(r"[a-f0-9]{16}", server_id):
        return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
    return await _subscription(ctx, request, token, "all", server_id=server_id)


@router.api_route("/sub/{token}/loc/{location_id}", methods=["GET", "HEAD"])
async def subscription_location(token: str, location_id: int, request: Request, ctx: Ctx) -> Response:
    if location_id < 1:
        return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
    return await _subscription(ctx, request, token, "all", location_id=location_id)


async def _subscription(
    ctx: Any,
    request: Request,
    token: str,
    profile: subscription_page.Profile,
    *,
    location_id: int | None = None,
    server_id: str | None = None,
) -> Response:
    if not TOKEN_RE.match(token):
        return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
    if profile == "all" and location_id is None and server_id is None and wants_page(request):
        return await _landing(ctx, request, token)
    rendered = await subscription_page.render(
        ctx.session_factory,
        ctx.panels,
        ctx.settings,
        token,
        profile=profile,
        client_headers=dict(request.headers),
        client_ip=client_ip(request, ctx.settings),
        secret_box=ctx.secret_box,
        location_id=location_id,
        server_id=server_id,
        track_devices=True,
    )
    if rendered is None:
        return PlainTextResponse("Not found", status_code=404, headers=NO_STORE)
    if rendered.servers_unavailable:
        headers = {**NO_STORE, "Retry-After": str(RETRY_AFTER_SEC)}
        return PlainTextResponse("Servers are temporarily unavailable", status_code=503, headers=headers)
    return Response(rendered.body, media_type=rendered.media_type, headers={**rendered.headers, **NO_STORE})


def _status_line(ctx: Any, lang: str, user: User, sub: Subscription | None, now: datetime) -> str:
    state = subscription_page.access_state(user, sub, now)
    if state == subscription_page.AccessState.ACTIVE and sub is not None:
        if sub.expires_at.year >= 2099:
            text = _t(ctx, "public_web.sub.active_forever", lang)
        else:
            date = ctx.i18n.date(sub.expires_at, lang, ctx.settings.zone)
            text = _t(ctx, "public_web.sub.active_until", lang, date=date)
        return f'<p class="ok">{esc(text)}</p>'
    return f'<p class="bad">{esc(_t(ctx, f"public_web.sub.{state.value}", lang))}</p>'


async def _proxy_block(ctx: Any, lang: str, user_id: int) -> str:
    try:
        view = await tg_proxy.user_view(ctx.session_factory, ctx.settings, ctx.secret_box, user_id)
    except Exception:
        log.exception("subscription page: proxy view of user %s failed", user_id)
        return ""
    ready = view.ready
    if not ready:
        return ""
    parts = [f"<h2>{esc(_t(ctx, 'public_web.proxy.title', lang))}</h2>"]
    parts.append(f'<p class="muted">{esc(_t(ctx, "public_web.proxy.text", lang))}</p>')
    for entry in ready:
        link = entry.links[0]
        label = _t(ctx, "public_web.proxy.connect", lang, title=entry.title)
        parts.append(f'<a class="btn" href="{esc(link.tg_url)}">{esc(label)}</a>')
    qr = await _qr_data_url(ready[0].links[0].tme_url)
    if qr:
        parts.append(f'<img class="qr" alt="QR" src="{qr}">')
    return "".join(parts)


async def _landing(ctx: Any, request: Request, token: str) -> Response:
    loaded = await _load_user(ctx, token)
    if loaded is None:
        return _not_found_page(ctx, pick_lang(ctx, request))
    user, sub = loaded
    lang = pick_lang(ctx, request, user.language)
    settings = ctx.settings
    base = base_url(ctx, request)
    sub_url = f"{base}/sub/{token}"
    title = settings.brand_name
    body = [f"<h1>{esc(title)}</h1>", _status_line(ctx, lang, user, sub, utcnow())]
    body.append(f"<h2>{esc(_t(ctx, 'public_web.sub.apps', lang))}</h2>")
    body.append(f'<p class="muted">{esc(_t(ctx, "public_web.sub.apps_hint", lang))}</p><div class="grid">')
    for app in deeplinks.APPS.values():
        body.append(f'<a class="btn" href="{esc(base)}/open/{esc(app.key)}/{esc(token)}">{esc(app.title)}</a>')
    body.append("</div>")
    body.append(f"<h2>{esc(_t(ctx, 'public_web.sub.manual', lang))}</h2>")
    body.append(f'<p class="muted">{esc(_t(ctx, "public_web.sub.manual_hint", lang))}</p>')
    body.append(f'<code id="url">{esc(sub_url)}</code>')
    copied = _t(ctx, "public_web.copied", lang)
    body.append(f'<button class="btn" id="copy" type="button">{esc(_t(ctx, "public_web.copy", lang))}</button>')
    qr = await _qr_data_url(sub_url)
    if qr:
        body.append(f'<img class="qr" alt="QR" src="{qr}">')
    body.append(await _proxy_block(ctx, lang, user.tg_id))
    links = []
    if ctx.bot_username:
        links.append(f'<a href="https://t.me/{esc(ctx.bot_username)}">{esc(_t(ctx, "public_web.open_bot", lang))}</a>')
    if settings.support_url:
        links.append(f'<a href="{esc(settings.support_url)}">{esc(_t(ctx, "public_web.support", lang))}</a>')
    if links:
        body.append(f'<p class="muted">{" · ".join(links)}</p>')
    script = (
        "document.getElementById('copy').addEventListener('click',function(){"
        f"navigator.clipboard.writeText({js_string(sub_url)}).then(function(){{"
        f"document.getElementById('copy').textContent={js_string(copied)};}});}});"
    )
    return page(title, "".join(body), lang=lang, script=script)


# --- app import page -------------------------------------------------------------------------------------------


async def _deeplink(ctx: Any, app: deeplinks.ClientApp, sub_url: str, name: str) -> str:
    settings = ctx.settings
    if app.key == "happ" and settings.sub_happ_crypto_links and getattr(ctx, "http", None) is not None:
        crypt = await deeplinks.happ_crypt_link(ctx.session_factory, ctx.http, sub_url)
        if crypt:
            return crypt
    if app.key == "incy" and settings.sub_incy_crypto_links:
        return deeplinks.incy_crypt_link(sub_url, name)
    return app.deeplink(sub_url, name)


@router.get("/open/{app_key}/{token}")
async def open_app(app_key: str, token: str, request: Request, ctx: Ctx) -> Response:
    app = deeplinks.get_app(app_key)
    loaded = await _load_user(ctx, token) if app is not None and TOKEN_RE.match(token) else None
    if app is None or loaded is None:
        return _not_found_page(ctx, pick_lang(ctx, request))
    user, _sub = loaded
    lang = pick_lang(ctx, request, user.language)
    sub_url = f"{base_url(ctx, request)}/sub/{token}"
    link = await _deeplink(ctx, app, sub_url, subscription_page.profile_title(ctx.settings, "all"))
    title = _t(ctx, "public_web.open.title", lang, app=app.title)
    button = _t(ctx, "public_web.open.button", lang, app=app.title)
    body = [
        f"<h1>{esc(title)}</h1>",
        f"<p>{esc(_t(ctx, 'public_web.open.text', lang, app=app.title))}</p>",
        f'<a class="btn main" id="open" href="{esc(link)}">{esc(button)}</a>',
        f"<h2>{esc(_t(ctx, 'public_web.open.install', lang, app=app.title))}</h2>",
    ]
    for download in app.downloads:
        platform = _t(ctx, f"public_web.platform.{download.platform.value}", lang)
        label = f"{platform} · {download.label}"
        body.append(f'<a class="btn small" href="{esc(download.url)}" rel="noopener">{esc(label)}</a>')
    body.append(
        f"<details><summary>{esc(_t(ctx, 'public_web.open.manual', lang))}</summary>"
        f'<p class="muted">{esc(_t(ctx, "public_web.sub.manual_hint", lang))}</p><code>{esc(sub_url)}</code></details>'
    )
    script = f"setTimeout(function(){{window.location.href={js_string(link)};}},150);"
    return page(title, "".join(body), lang=lang, script=script)


# --- gift landing --------------------------------------------------------------------------------------------------


@router.get("/gift/{code}")
async def gift(code: str, request: Request, ctx: Ctx) -> Response:
    lang = pick_lang(ctx, request)
    if not GIFT_CODE_RE.match(code):
        return _not_found_page(ctx, lang)
    now = utcnow()
    async with read_session(ctx.session_factory) as session:
        found = await promo.get_by_code(session, code)
        plan = await session.get(Plan, found.plan_id) if found is not None and found.plan_id else None
    usable = (
        found is not None
        and found.kind == PromoKind.GIFT
        and found.is_active
        and (found.max_uses == 0 or found.uses < found.max_uses)
        and (found.expires_at is None or found.expires_at > now)
    )
    title = _t(ctx, "public_web.gift.title", lang)
    if not usable or found is None:
        body = f"<h1>{esc(title)}</h1><p>{esc(_t(ctx, 'public_web.gift.gone', lang))}</p>"
        if ctx.bot_username:
            bot = f"https://t.me/{ctx.bot_username}"
            body += f'<a class="btn" href="{esc(bot)}">{esc(_t(ctx, "public_web.open_bot", lang))}</a>'
        return page(title, body, lang=lang, status_code=404)
    days = found.bonus_days or (plan.duration_days if plan is not None else 0)
    what = plan.title if plan is not None else ctx.i18n.duration(days, lang)
    text = _t(ctx, "public_web.gift.text", lang, brand=ctx.settings.brand_name, what=what)
    body = f"<h1>{esc(title)}</h1><p>{esc(text)}</p>"
    if ctx.bot_username:
        link = f"https://t.me/{ctx.bot_username}?start=gift_{found.code}"
        body += f'<a class="btn main" href="{esc(link)}">{esc(_t(ctx, "public_web.gift.button", lang))}</a>'
        body += f'<p class="muted">{esc(_t(ctx, "public_web.gift.hint", lang))}</p>'
    else:
        body += f"<code>{esc(found.code)}</code>"
    return page(title, body, lang=lang)


# --- Remnawave webhook ---------------------------------------------------------------------------------------------


def webhook_key(raw: bytes) -> str:
    return WEBHOOK_KEY_PREFIX + hashlib.sha256(raw).hexdigest()[:48]


async def _process(ctx: Any, event: Any) -> None:
    try:
        outcome = await remnawave_hook.handle_event(ctx, event)
        log.debug("remnawave webhook %s: %s", event.event, outcome)
    except Exception:
        log.exception("remnawave webhook %s: processing failed", event.event)


@router.post("/webhooks/remnawave")
async def remnawave_webhook(request: Request, background: BackgroundTasks, ctx: Ctx) -> JSONResponse:
    secret_value = ctx.settings.remnawave_webhook_secret
    secret = secret_value.get_secret_value() if secret_value is not None else ""
    if not secret:
        raise ApiError(404, "not_found")
    raw = await request.body()
    if not RemnawaveBackend.verify_webhook(raw, request.headers.get(SIGNATURE_HEADER), secret):
        log.warning("remnawave webhook with a bad signature rejected")
        raise ApiError(401, "bad_signature", "Invalid signature")
    try:
        event = RemnawaveBackend.parse_webhook(raw)
    except RemnawaveWebhookError as exc:
        raise ApiError(400, "bad_request", str(exc)) from None
    async with ctx.session_factory.begin() as session:
        first = await kv.add(session, webhook_key(raw), utcnow().isoformat())
    if not first:
        return JSONResponse({"ok": True, "duplicate": True}, headers=NO_STORE)
    background.add_task(_process, ctx, event)
    return JSONResponse({"ok": True}, headers=NO_STORE)
