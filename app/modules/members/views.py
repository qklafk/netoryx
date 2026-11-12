"""Screens of the members section (pure functions)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from aiogram.types import InlineKeyboardButton

from app.core.i18n import Translator
from app.core.ui import Screen
from app.keyboards import Button, kb
from app.modules.admin.common import to_admin, user_title
from app.modules.members.callbacks import UNLIMITED, MemCb
from app.modules.members.service import Member

EXTEND_CHOICES = (30, 90, 365, UNLIMITED)
DEVICE_CHOICES = (1, 2, 3, 5, UNLIMITED)
TRAFFIC_CHOICES = (UNLIMITED, 50, 100, 300)
STATE_ICONS = {"active": "🟢", "paused": "⏸", "expired": "⌛️"}


def _name(member: Member) -> str:
    user = member.user
    return (user.first_name or "").strip() or (f"@{user.username}" if user.username else str(user.tg_id))


def access_text(_: Translator, member: Member, now: datetime) -> str:
    if member.unlimited:
        return _("fmt.forever")
    expires = member.sub.expires_at
    if expires <= now:
        return _("members.expired_on", date=_.date(expires))
    days_left = max(1, (expires - now).days)
    return _("members.until", date=_.date(expires), left=_.plural(days_left, "plural.days"))


def list_screen(_: Translator, members: Sequence[Member], page: int, pages: int, total: int, now: datetime) -> Screen:
    text = _("members.list.title", count=total)
    text += "\n\n" + (_("members.list.intro") if total else _("members.list.empty"))
    rows: list[list[InlineKeyboardButton | None]] = []
    for member in members:
        state = member.state(now)
        until = "∞" if member.unlimited else member.sub.expires_at.strftime("%d.%m.%y")
        label = f"{STATE_ICONS[state]} {_name(member)} · {until}"
        rows.append([Button.cb(label[:64], MemCb(action="card", uid=member.user.tg_id, page=page))])
    if pages > 1:
        rows.append(
            [
                Button.cb("◀️", MemCb(action="list", page=page - 1)) if page > 0 else None,
                Button.cb(f"{page + 1}/{pages}", MemCb(action="list", page=page)),
                Button.cb("▶️", MemCb(action="list", page=page + 1)) if page + 1 < pages else None,
            ]
        )
    rows.append([to_admin(_)])
    return Screen(text, kb(rows))


def card_screen(_: Translator, member: Member, now: datetime, page: int = 0) -> Screen:
    sub, uid = member.sub, member.user.tg_id
    state = member.state(now)
    lines = [
        _("members.card.title", user=user_title(member.user)),
        "",
        _("members.card.state", value=f"{STATE_ICONS[state]} {_(f'members.state.{state}')}"),
        _("members.card.access", value=access_text(_, member, now)),
        _("members.card.devices", value=_("fmt.unlimited") if sub.device_limit <= 0 else str(sub.device_limit)),
        _(
            "members.card.traffic",
            value=_("fmt.unlimited") if sub.traffic_limit_gb <= 0 else _.plural(sub.traffic_limit_gb, "plural.gb"),
        ),
        _("members.card.since", value=_.date(member.user.created_at)),
    ]
    paused = state == "paused"
    rows: list[list[InlineKeyboardButton | None]] = [
        [
            Button.cb(_("members.btn.extend"), MemCb(action="ext", uid=uid, page=page), style="success"),
            Button.cb(
                _("members.btn.resume") if paused else _("members.btn.pause"),
                MemCb(action="resume" if paused else "pause", uid=uid, page=page),
            ),
        ],
        [
            Button.cb(_("members.btn.limits"), MemCb(action="lim", uid=uid, page=page)),
            Button.cb(_("members.btn.reset"), MemCb(action="reset", uid=uid, page=page)),
        ],
        [
            Button.cb(_("members.btn.rotate"), MemCb(action="rot", uid=uid, page=page)),
            Button.cb(_("members.btn.remove"), MemCb(action="del", uid=uid, page=page), style="danger"),
        ],
        [Button.cb(_("nav.back"), MemCb(action="list", page=page))],
    ]
    return Screen("\n".join(lines), kb(rows))


def _back(_: Translator, member: Member, page: int) -> InlineKeyboardButton:
    return Button.cb(_("nav.back"), MemCb(action="card", uid=member.user.tg_id, page=page))


def extend_screen(_: Translator, member: Member, now: datetime, page: int) -> Screen:
    uid = member.user.tg_id
    buttons = [
        Button.cb(
            _("fmt.forever") if days == UNLIMITED else "+" + _.duration(days),
            MemCb(action="ext_ok", uid=uid, value=days, page=page),
        )
        for days in EXTEND_CHOICES
    ]
    text = _("members.extend", name=_.esc(_name(member)), access=access_text(_, member, now))
    return Screen(text, kb([buttons[:2], buttons[2:], [_back(_, member, page)]]))


def limits_screen(_: Translator, member: Member, page: int) -> Screen:
    uid, sub = member.user.tg_id, member.sub

    def mark(label: str, current: bool) -> str:
        return f"✓ {label}" if current else label

    devices = [
        Button.cb(
            mark("∞" if n == UNLIMITED else str(n), sub.device_limit == max(n, 0)),
            MemCb(action="dev_ok", uid=uid, value=n, page=page),
        )
        for n in DEVICE_CHOICES
    ]
    traffic = [
        Button.cb(
            mark(
                _("fmt.unlimited") if gb == UNLIMITED else _.plural(gb, "plural.gb"), sub.traffic_limit_gb == max(gb, 0)
            ),
            MemCb(action="gb_ok", uid=uid, value=gb, page=page),
        )
        for gb in TRAFFIC_CHOICES
    ]
    text = _("members.limits", name=_.esc(_name(member)))
    return Screen(text, kb([devices, traffic[:2], traffic[2:], [_back(_, member, page)]]))


def confirm_screen(_: Translator, member: Member, action: str, page: int) -> Screen:
    """Confirmation of ``rot`` (new link) or ``del`` (remove the member)."""
    uid = member.user.tg_id
    key = "members.rotate_confirm" if action == "rot" else "members.remove_confirm"
    ok = Button.cb(
        _("members.btn.rotate_ok" if action == "rot" else "members.btn.remove_ok"),
        MemCb(action=f"{action}_ok", uid=uid, page=page),
        style="danger",
    )
    return Screen(_(key, name=_.esc(_name(member))), kb([[ok], [_back(_, member, page)]]))
