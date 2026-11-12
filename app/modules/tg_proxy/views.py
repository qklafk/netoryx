"""Screens of the Telegram proxy section (pure: data in, :class:`Screen` out)."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton

from app.config import Settings
from app.core.i18n import Translator
from app.core.ui import Screen
from app.db.models import ProvisionState
from app.keyboards import Button, kb, nav, to_menu
from app.keyboards.builders import MAX_COPY_TEXT
from app.modules.tg_proxy.callbacks import NAV, ProxyCb
from app.services.panels.mtproto import ProxyLink
from app.services.panels.socks import SocksLink
from app.services.tg_proxy import AccessReason, ProxyEntry, UserProxyView, free_ad_tag
from app.services.tg_socks import SocksEntry, SocksView


def primary_link(entry: ProxyEntry) -> ProxyLink | None:
    return entry.links[0] if entry.links else None


def _status_icon(entry: ProxyEntry) -> str:
    if entry.ready:
        return "🟢"
    if entry.error or entry.state == ProvisionState.DISABLED:
        return "🔴"
    return "⏳"


def _limits_lines(view: UserProxyView, settings: Settings, _: Translator) -> list[str]:
    if not view.policy.limited:
        return []
    lines: list[str] = []
    if settings.tg_proxy_free_rate_limit_mbps > 0:
        lines.append(_("tg_proxy.limited_speed", mbps=settings.tg_proxy_free_rate_limit_mbps))
    if free_ad_tag(settings):
        lines.append(_("tg_proxy.limited_ad"))
    return lines


def socks_link(entry: SocksEntry) -> SocksLink | None:
    return entry.link


def _socks_block(socks: SocksView, _: Translator) -> str | None:
    """The SOCKS5 credentials of every ready server (None when there is nothing to show yet)."""
    ready = socks.ready
    if not ready:
        return _("tg_proxy.socks_pending") if socks.entries else None
    lines = [_("tg_proxy.socks_title"), _("tg_proxy.socks_intro")]
    for entry in ready:
        link = entry.link
        if link is None:
            continue
        data = _(
            "tg_proxy.socks_data",
            server=_.esc(link.server),
            port=link.port,
            user=_.esc(link.user),
            password=_.esc(link.password),
        )
        lines.append(f"{_.esc(entry.title)}\n{data}" if len(ready) > 1 else data)
    return "\n\n".join(lines)


def proxy_screen(
    view: UserProxyView,
    settings: Settings,
    _: Translator,
    *,
    no_targets: bool = False,
    socks: SocksView | None = None,
) -> Screen:
    """The main proxy screen: connect buttons per server, copy, QR, web proxy, SOCKS5, new link, FAQ."""
    reason = view.policy.reason
    if not view.policy.allowed:
        key = "tg_proxy.no_subscription" if reason == AccessReason.NO_SUBSCRIPTION else "tg_proxy.unavailable"
        return Screen(_(key), kb([[to_menu(_)]]))

    parts = [_("tg_proxy.title"), _("tg_proxy.intro")]
    ready = view.ready
    socks_ready = socks.ready if socks is not None else ()
    socks_entries = bool(socks is not None and socks.entries)
    if view.entries:
        lines = [_("tg_proxy.servers")]
        for index, entry in enumerate(view.entries):
            label = _.esc(entry.title)
            if index > 0:
                label = _("tg_proxy.backup", title=label)
            lines.append(f"{_status_icon(entry)} {label}")
        parts.append("\n".join(lines))
    if not ready and not socks_ready:
        missing = (no_targets and not socks_entries) or not (view.entries or socks_entries)
        parts.append(_("tg_proxy.no_servers") if missing else _("tg_proxy.pending"))
    limits = _limits_lines(view, settings, _)
    if limits:
        parts.append("\n".join(limits))
    socks_text = _socks_block(socks, _) if socks is not None else None
    if socks_text:
        parts.append(socks_text)
    if ready or socks_ready:
        parts.append(_("tg_proxy.private_note"))

    rows: list[list[InlineKeyboardButton | None]] = []
    for index, entry in enumerate(view.entries):
        link = primary_link(entry)
        if not entry.ready or link is None:
            continue
        rows.append(
            [
                Button.url(_("tg_proxy.btn.connect", title=entry.title), link.tme_url, style="success"),
                Button.cb(_("tg_proxy.btn.qr"), ProxyCb(action="qr", arg=index)),
            ]
        )
    for index, socks_entry in enumerate(socks.entries if socks is not None else ()):
        if socks_entry.link is None:
            continue
        rows.append(
            [
                Button.url(_("tg_proxy.btn.socks_connect", title=socks_entry.title), socks_entry.link.tme_url),
                Button.cb(_("tg_proxy.btn.qr"), ProxyCb(action="qrs", arg=index)),
            ]
        )
    if ready:
        first = primary_link(ready[0])
        copy_value = first.tg_url if first is not None else ""
        web_url = next((e.web_url for e in ready if e.web_url), None)
        rows.append(
            [
                Button.copy(_("tg_proxy.btn.copy"), copy_value) if 0 < len(copy_value) <= MAX_COPY_TEXT else None,
                Button.url(_("tg_proxy.btn.web"), web_url) if web_url else None,
            ]
        )
    if ready or socks_ready:
        rows.append(
            [
                Button.cb(_("tg_proxy.btn.rotate"), ProxyCb(action="rotate")),
                Button.cb(_("tg_proxy.btn.faq"), ProxyCb(action="faq")),
            ]
        )
    else:
        rows.append([nav(_("nav.refresh"), NAV), Button.cb(_("tg_proxy.btn.faq"), ProxyCb(action="faq"))])
    rows.append([to_menu(_)])
    return Screen("\n\n".join(parts), kb(rows))


def qr_screen(entry: ProxyEntry, png: bytes, _: Translator) -> Screen:
    caption = _("tg_proxy.qr", title=_.esc(entry.title))
    return Screen(caption, kb([[nav(_("nav.back"), NAV)]]), photo=png)


def socks_qr_screen(entry: SocksEntry, png: bytes, _: Translator) -> Screen:
    caption = _("tg_proxy.qr_socks", title=_.esc(entry.title))
    return Screen(caption, kb([[nav(_("nav.back"), NAV)]]), photo=png)


def rotate_confirm_screen(_: Translator) -> Screen:
    return Screen(
        _("tg_proxy.rotate_confirm"),
        kb(
            [
                [Button.cb(_("tg_proxy.btn.rotate_ok"), ProxyCb(action="rotate_ok"), style="danger")],
                [nav(_("nav.cancel"), NAV)],
            ]
        ),
    )


def faq_screen(_: Translator, *, socks: bool = False) -> Screen:
    text = _("tg_proxy.faq") + ("\n\n" + _("tg_proxy.faq_socks") if socks else "")
    return Screen(text, kb([[nav(_("nav.back"), NAV)], [to_menu(_)]]))
