"""Screens of the invitations section (pure functions)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton

from app.core.i18n import Translator
from app.core.ui import Screen
from app.db.models import Invite, User
from app.keyboards import Button, kb, nav
from app.keyboards.builders import MAX_COPY_TEXT
from app.modules.admin.common import to_admin, user_short
from app.modules.invites.callbacks import NAV, UNLIMITED, InvCb, InvNew
from app.services import invites

DAYS_CHOICES = (30, 90, 365, UNLIMITED)
DEVICE_CHOICES = (1, 2, 3, 5, UNLIMITED)
USES_CHOICES = (1, 5, UNLIMITED)


class InviteState(StrEnum):
    ACTIVE = "active"
    USED = "used"
    EXPIRED = "expired"
    REVOKED = "revoked"


STATE_ICONS = {
    InviteState.ACTIVE: "🟢",
    InviteState.USED: "✅",
    InviteState.EXPIRED: "⌛️",
    InviteState.REVOKED: "🚫",
}


def invite_state(invite: Invite, now: datetime) -> InviteState:
    if not invite.is_active:
        return InviteState.REVOKED
    if invite.expires_at is not None and invite.expires_at <= now:
        return InviteState.EXPIRED
    if invite.max_uses and invite.uses >= invite.max_uses:
        return InviteState.USED
    return InviteState.ACTIVE


def invite_link(bot_username: str, invite: Invite) -> str:
    return f"https://t.me/{bot_username}?start={invites.deep_link_payload(invite)}"


def period_text(_: Translator, days: int | None) -> str:
    return _("fmt.forever") if days is None or days == UNLIMITED else _.duration(days)


def devices_text(_: Translator, devices: int) -> str:
    return _("fmt.unlimited") if devices <= 0 else _.plural(devices, "plural.devices")


def uses_text(_: Translator, invite: Invite) -> str:
    total = "∞" if invite.max_uses == 0 else str(invite.max_uses)
    return _("invites.uses_value", used=invite.uses, total=total)


def _title(_: Translator, invite: Invite) -> str:
    return invite.note or _("invites.untitled", code=invite.code)


# --- list ----------------------------------------------------------------------------------------------------------


def list_screen(_: Translator, items: Sequence[Invite], page: int, has_next: bool, now: datetime) -> Screen:
    text = _("invites.list.title")
    text += "\n\n" + (_("invites.list.intro") if items or page else _("invites.list.empty"))
    rows: list[list[InlineKeyboardButton | None]] = [
        [Button.cb(_("invites.btn.new"), InvNew(step="days"), style="success")]
    ]
    for invite in items:
        label = f"{STATE_ICONS[invite_state(invite, now)]} {_title(_, invite)} · {period_text(_, invite.days)}"
        rows.append([Button.cb(label[:64], InvCb(action="card", id=invite.id, page=page))])
    if page > 0 or has_next:
        rows.append(
            [
                Button.cb("◀️", InvCb(action="list", page=page - 1)) if page > 0 else None,
                Button.cb("▶️", InvCb(action="list", page=page + 1)) if has_next else None,
            ]
        )
    rows.append([to_admin(_)])
    return Screen(text, kb(rows))


# --- card ----------------------------------------------------------------------------------------------------------


def card_screen(_: Translator, invite: Invite, link: str, now: datetime, page: int = 0) -> Screen:
    state = invite_state(invite, now)
    lines = [
        _("invites.card.title", title=_.esc(_title(_, invite))),
        "",
        _("invites.card.period", value=period_text(_, invite.days)),
        _("invites.card.devices", value=devices_text(_, invite.device_limit)),
    ]
    if invite.traffic_limit_gb:
        lines.append(_("invites.card.traffic", value=_.plural(invite.traffic_limit_gb, "plural.gb")))
    lines.append(_("invites.card.uses", value=uses_text(_, invite)))
    if invite.expires_at is not None:
        lines.append(_("invites.card.valid_until", value=_.datetime(invite.expires_at)))
    lines.append(_("invites.card.state", value=f"{STATE_ICONS[state]} {_(f'invites.state.{state.value}')}"))
    usable = state == InviteState.ACTIVE
    if usable:
        lines += ["", _("invites.card.link", link=_.esc(link)), "", _("invites.card.hint")]
    share = "https://t.me/share/url?url=" + quote(link, safe="") + "&text=" + quote(_("invites.share_text"), safe="")
    rows: list[list[InlineKeyboardButton | None]] = []
    if usable:
        rows.append([Button.url(_("invites.btn.share"), share, style="success")])
        rows.append(
            [
                Button.copy(_("invites.btn.copy"), link) if len(link) <= MAX_COPY_TEXT else None,
                Button.cb(_("invites.btn.qr"), InvCb(action="qr", id=invite.id, page=page)),
            ]
        )
    if invite.uses:
        rows.append(
            [Button.cb(_("invites.btn.users", count=invite.uses), InvCb(action="users", id=invite.id, page=page))]
        )
    if invite.is_active:
        rows.append(
            [Button.cb(_("invites.btn.revoke"), InvCb(action="revoke", id=invite.id, page=page), style="danger")]
        )
    rows.append([Button.cb(_("nav.back"), InvCb(action="list", page=page))])
    return Screen("\n".join(lines), kb(rows))


def revoke_screen(_: Translator, invite: Invite, page: int) -> Screen:
    return Screen(
        _("invites.revoke_confirm", title=_.esc(_title(_, invite))),
        kb(
            [
                [
                    Button.cb(
                        _("invites.btn.revoke_ok"), InvCb(action="revoke_ok", id=invite.id, page=page), style="danger"
                    )
                ],
                [Button.cb(_("nav.cancel"), InvCb(action="card", id=invite.id, page=page))],
            ]
        ),
    )


def users_screen(_: Translator, invite: Invite, members: Sequence[User], page: int) -> Screen:
    lines = [_("invites.users.title", title=_.esc(_title(_, invite))), ""]
    lines += [f"• {_.esc(user_short(user))}" for user in members] or [_("invites.users.empty")]
    return Screen("\n".join(lines), kb([[Button.cb(_("nav.back"), InvCb(action="card", id=invite.id, page=page))]]))


def qr_screen(_: Translator, invite: Invite, png: bytes, page: int) -> Screen:
    return Screen(
        _("invites.qr", title=_.esc(_title(_, invite))),
        kb([[Button.cb(_("nav.back"), InvCb(action="card", id=invite.id, page=page))]]),
        photo=png,
    )


# --- wizard --------------------------------------------------------------------------------------------------------


def _choice_label(_: Translator, step: str, value: int) -> str:
    if step == "days":
        return period_text(_, value)
    if step == "devices":
        return devices_text(_, value)
    return _("invites.uses.unlimited") if value == UNLIMITED else _.plural(value, "invites.uses.people")


def wizard_screen(_: Translator, data: InvNew) -> Screen:
    """One step of the wizard: the choices of ``data.step`` with the values chosen so far."""
    step = data.step
    choices = {"days": DAYS_CHOICES, "devices": DEVICE_CHOICES, "uses": USES_CHOICES}[step]
    following = {"days": "devices", "devices": "uses", "uses": "note"}[step]
    buttons: list[InlineKeyboardButton | None] = []
    for value in choices:
        values = {"days": data.days, "devices": data.devices, "uses": data.uses, step: value}
        buttons.append(Button.cb(_choice_label(_, step, value), InvNew(step=following, **values)))
    rows: list[list[InlineKeyboardButton | None]] = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    rows.append([nav(_("nav.cancel"), NAV)])
    return Screen(_(f"invites.new.{step}"), kb(rows))


def note_screen(_: Translator, data: InvNew) -> Screen:
    summary = _(
        "invites.new.summary",
        period=period_text(_, data.days),
        devices=devices_text(_, data.devices),
        uses=_choice_label(_, "uses", data.uses),
    )
    rows = [
        [
            Button.cb(
                _("invites.btn.skip_note"), InvNew(step="create", days=data.days, devices=data.devices, uses=data.uses)
            )
        ],
        [nav(_("nav.cancel"), NAV)],
    ]
    return Screen(summary + "\n\n" + _("invites.new.note"), kb(rows))
