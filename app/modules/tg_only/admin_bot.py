"""Bot admin screen of the shared Telegram link: the public page, the subscription link, a QR code, "new link"."""

from __future__ import annotations

from aiogram import F
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery

from app.core.context import AppContext
from app.core.i18n import Translator
from app.core.permissions import HasPermission, Permission
from app.core.ui import Screen, render
from app.keyboards import Button, back, kb
from app.modules.admin.common import admin_router
from app.services import tg_only
from app.services.qr import make_qr_png_async

router = admin_router("tg_only_admin")
router.callback_query.filter(HasPermission(Permission.PANELS))


class TgOnlyCb(CallbackData, prefix="tgo"):
    a: str


def _skipped_line(_: Translator, item: tg_only.Skipped) -> str:
    reason = _(f"tg_only.skipped.{item.reason}", country=item.country or "")
    return f"• {_.esc(item.panel_name)} - {reason}"


def home_text(_: Translator, view: tg_only.AdminView) -> str:
    if not view.enabled:
        return _("tg_only.off")
    lines = [_("tg_only.home_head", page=_.esc(view.page), url=_.esc(view.url)), ""]
    if view.servers:
        for server in view.servers:
            mark = "✅" if server.ready else "⏳"
            lines.append(f"{mark} {_.esc(server.title)}")
            if server.error:
                lines.append(f"    <i>{_.esc(server.error)}</i>")
    else:
        lines.append(_("tg_only.no_servers"))
    if view.skipped:
        lines += ["", _("tg_only.skipped_head"), *(_skipped_line(_, item) for item in view.skipped)]
    lines += ["", _("tg_only.howto")]
    return "\n".join(lines)


async def home_screen(ctx: AppContext, _: Translator) -> Screen:
    view = await tg_only.admin_view(ctx.session_factory, ctx.settings)
    rows = []
    if view.enabled:
        rows.append(
            [Button.cb(_("tg_only.btn.qr"), TgOnlyCb(a="qr")), Button.cb(_("tg_only.btn.rotate"), TgOnlyCb(a="rotate"))]
        )
        rows.append([Button.cb(_("nav.refresh"), TgOnlyCb(a="sync"))])
    rows.append([back("admin", _)])
    return Screen(home_text(_, view), kb(rows))


@router.callback_query(TgOnlyCb.filter(F.a == "home"))
async def open_home(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await render(callback, await home_screen(ctx, _))


@router.callback_query(TgOnlyCb.filter(F.a == "sync"))
async def sync_now(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await tg_only.reconcile(ctx.session_factory, ctx.panels, ctx.settings)
    await render(callback, await home_screen(ctx, _))


@router.callback_query(TgOnlyCb.filter(F.a == "qr"))
async def show_qr(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    view = await tg_only.admin_view(ctx.session_factory, ctx.settings)
    png = await make_qr_png_async(view.url)
    await render(callback, Screen(_("tg_only.qr"), kb([[Button.cb(_("nav.back"), TgOnlyCb(a="home"))]]), photo=png))


@router.callback_query(TgOnlyCb.filter(F.a == "rotate"))
async def ask_rotate(callback: CallbackQuery, _: Translator) -> None:
    rows = [
        [Button.cb(_("tg_only.btn.rotate_ok"), TgOnlyCb(a="rotate_ok"), style="danger")],
        [Button.cb(_("nav.cancel"), TgOnlyCb(a="home"))],
    ]
    await render(callback, Screen(_("tg_only.rotate_confirm"), kb(rows)))


@router.callback_query(TgOnlyCb.filter(F.a == "rotate_ok"))
async def rotate(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    await tg_only.rotate(ctx.session_factory, ctx.panels, ctx.settings)
    await render(callback, await home_screen(ctx, _), answer=_("tg_only.rotated"))
