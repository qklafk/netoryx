"""Screens of the subscription module. Pure functions of already loaded data: no database, no network."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from aiogram.types import InlineKeyboardButton

from app.config import Settings
from app.core.i18n import Translator, format_number
from app.core.ui import Screen
from app.db.models import Payment, Plan, Subscription
from app.keyboards import Button, back, grid, kb, nav, to_menu
from app.keyboards.builders import MAX_COPY_TEXT
from app.modules.payments import onchain as onchain_view
from app.modules.subscription.callbacks import (
    ADDONS,
    BUY,
    CONNECT,
    DEVICES,
    LINKS,
    QR,
    SUB,
    SUPPORT,
    TRIAL,
    WALLET,
    AddonCb,
    CheckCb,
    ConnCb,
    DevCb,
    PayCb,
    PlanCb,
    ServerCb,
    SubAct,
)
from app.modules.subscription.state import Status, SubState
from app.modules.subscription.urls import open_url, subscription_url, whitelist_url
from app.services.addons import AddonKind, Offer
from app.services.deeplinks import APPS, ClientApp, Platform, apps_for_platform
from app.services.devices import DeviceInfo, PanelDevices
from app.services.payments import onchain
from app.services.payments.base import PaymentGateway
from app.utils.bytes import GIB

MIB = 1024**2
PLATFORMS: tuple[Platform, ...] = (
    Platform.IOS,
    Platform.ANDROID,
    Platform.WINDOWS,
    Platform.MACOS,
    Platform.LINUX,
    Platform.APPLE_TV,
    Platform.ANDROID_TV,
)
TV_PLATFORMS = frozenset({Platform.APPLE_TV, Platform.ANDROID_TV})
BALANCE = "bal"  # PayCb.via of a purchase from the wallet
STARS = "stars"  # gateway key of Telegram Stars
MAX_BUTTON_TEXT = 64

Rows = list[Sequence[InlineKeyboardButton | None]]


# --- formatting ------------------------------------------------------------------------------------------------------
def fmt_traffic(_: Translator, n_bytes: int) -> str:
    """``"350 МБ"`` below a gigabyte, else ``"1,5 ГБ"`` (one decimal, locale separator)."""
    n_bytes = max(0, n_bytes)
    if n_bytes < GIB:
        return _("subscription.unit.mb", n=format_number(round(n_bytes / MIB), _.lang))
    whole, frac = divmod(round(n_bytes * 10 / GIB), 10)
    text = format_number(whole, _.lang)
    if frac:
        text += ("," if _.lang == "ru" else ".") + str(frac)
    return _("subscription.unit.gb", n=text)


def fmt_devices(_: Translator, limit: int) -> str:
    return _.plural(limit, "plural.devices") if limit > 0 else _("fmt.unlimited")


def fmt_traffic_limit(_: Translator, limit_gb: int) -> str:
    return _.plural(limit_gb, "plural.gb") if limit_gb > 0 else _("fmt.unlimited")


def fmt_sub_traffic_limit(_: Translator, sub: Subscription) -> str:
    """Render a base GB quota or an exact quota after a purchased traffic pack."""
    if sub.traffic_limit_bytes is not None:
        return fmt_traffic(_, sub.traffic_limit_bytes)
    return fmt_traffic_limit(_, sub.traffic_limit_gb)


def fmt_expiry(_: Translator, state: SubState) -> str:
    """``"4 декабря 2026 (ещё 12 дней)"`` / ``"бессрочно"``."""
    if state.sub is None:
        return ""
    if state.unlimited:
        return _("fmt.forever")
    date = _.date(state.sub.expires_at)
    if state.status == Status.ACTIVE:
        return _("subscription.card.expires_left", date=date, left=_.plural(state.days_left, "plural.days"))
    return date


def _price(plan: Plan, prices: Mapping[int, int] | None = None) -> int:
    return prices.get(plan.id, plan.price_kop) if prices is not None else plan.price_kop


def _monthly_price_kop(plan: Plan, price_kop: int) -> int:
    return round(price_kop * 30 / plan.duration_days)


def reference_monthly_kop(plans: Sequence[Plan], prices: Mapping[int, int] | None = None) -> int | None:
    """Monthly price of the shortest paid plan: discounts of longer plans are computed against it."""
    paid = [plan for plan in plans if _price(plan, prices) > 0 and plan.duration_days > 0]
    if not paid:
        return None
    shortest = min(paid, key=lambda plan: (plan.duration_days, _price(plan, prices)))
    return _monthly_price_kop(shortest, _price(shortest, prices))


def discount_percent(plan: Plan, reference_kop: int | None, price_kop: int | None = None) -> int:
    """Saving of ``plan`` per month against the reference price, whole percent (0 when none)."""
    price = plan.price_kop if price_kop is None else price_kop
    if not reference_kop or price <= 0 or plan.duration_days <= 30:
        return 0
    saving = 1 - _monthly_price_kop(plan, price) / reference_kop
    return max(0, round(saving * 100))


def monthly_rub_kop(plan: Plan, price_kop: int | None = None) -> int:
    """Per-month price rounded to whole rubles for display."""
    return round(_monthly_price_kop(plan, plan.price_kop if price_kop is None else price_kop) / 100) * 100


def gateway_title(_: Translator, gateway: PaymentGateway) -> str:
    key = f"subscription.gw.{gateway.key}"
    return _(key) if _.i18n.has(key) else gateway.title


def platform_title(_: Translator, platform: Platform | str) -> str:
    return _(f"subscription.platform.{Platform(platform).value}")


def _clip(text: str) -> str:
    return text if len(text) <= MAX_BUTTON_TEXT else text[: MAX_BUTTON_TEXT - 1] + "…"


def subscription_list(_: Translator, subscriptions: Sequence[Subscription], *, shop: bool) -> Screen:
    rows: Rows = []
    for sub in subscriptions:
        title = sub.label or f"#{sub.id}"
        rows.append([nav(_clip(f"🔐 {title} · {_.date(sub.expires_at)}"), SUB, sub.id)])
    if shop:
        rows.append([nav(_("subscription.multi.new_button"), BUY, -1)])
    rows.append([to_menu(_)])
    return Screen(_("subscription.multi.list_title"), kb(rows))


def purchase_targets(_: Translator, subscriptions: Sequence[Subscription]) -> Screen:
    rows: Rows = [
        [nav(_clip(_("subscription.multi.renew_target", name=sub.label or f"#{sub.id}")), BUY, sub.id)]
        for sub in subscriptions
    ]
    rows += [[nav(_("subscription.multi.new_button"), BUY, -1)], [to_menu(_)]]
    return Screen(_("subscription.multi.choose_purchase"), kb(rows))


def server_list(_: Translator, state: SubState, groups: Sequence[dict[str, Any]]) -> Screen:
    sid = state.sub.id if state.sub else 0
    rows: Rows = [[Button.cb(_clip(f"🌍 {group['name']}"), ServerCb(s=sid, server=group["id"]))] for group in groups]
    rows.append([back(CONNECT, _, sid), to_menu(_)])
    return Screen(_("subscription.multi.links_title" if groups else "subscription.multi.links_empty"), kb(rows))


def server_detail(_: Translator, state: SubState, group: dict[str, Any]) -> Screen:
    sid = state.sub.id if state.sub else 0
    lines = [_("subscription.multi.server_title", name=_.esc(group["name"]))]
    rows: Rows = []
    url = str(group.get("subscription_url") or "")
    if url:
        lines += ["", _("subscription.multi.server_sub", url=_.esc(url))]
        if len(url) <= MAX_COPY_TEXT:
            rows.append([Button.copy(_("subscription.btn.copy"), url)])
        rows.append([Button.cb(_("subscription.btn.qr"), ServerCb(s=sid, server=group["id"], q=True))])
    for index, profile in enumerate(group.get("profiles", [])):
        rows.append([Button.cb(_clip(f"🔑 {profile['name']}"), ServerCb(s=sid, server=group["id"], i=index))])
    rows.append([back(LINKS, _, sid), to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def server_profile(_: Translator, state: SubState, group: dict[str, Any], index: int, *, qr: bytes | None) -> Screen:
    sid = state.sub.id if state.sub else 0
    profile = group["profiles"][index]
    uri = str(profile["uri"])
    lines = [_("subscription.multi.profile_title", name=_.esc(profile["name"]))]
    if qr is None and len(uri) < 3500:
        lines += ["", f"<code>{_.esc(uri)}</code>"]
    rows: Rows = []
    if len(uri) <= MAX_COPY_TEXT:
        rows.append([Button.copy(_("subscription.multi.copy_profile"), uri)])
    if qr is None:
        rows.append([Button.cb(_("subscription.btn.qr"), ServerCb(s=sid, server=group["id"], i=index, q=True))])
    rows.append([Button.cb(_("nav.back"), ServerCb(s=sid, server=group["id"])), to_menu(_)])
    return Screen("\n".join(lines), kb(rows), photo=qr)


# --- subscription card -----------------------------------------------------------------------------------------------
def sub_card(
    _: Translator,
    settings: Settings,
    state: SubState,
    *,
    used_bytes: int | None = None,
    header: str | None = None,
) -> Screen:
    """The "My subscription" card: status, plan, expiry, limits, traffic and auto-renewal, with the actions."""
    shop = settings.app_mode == "shop"
    sub = state.sub
    lines: list[str] = [header, ""] if header else []
    lines.append(_("subscription.card.title"))
    if sub is not None and state.total_subscriptions > 1:
        lines.append(_("subscription.multi.name", name=_.esc(sub.label or f"#{sub.id}")))
    rows: Rows
    if sub is None:
        lines += ["", _("subscription.card.none" if shop else "subscription.card.none_personal")]
        trial = state.trial_available(settings)
        if trial:
            lines.append(_("subscription.card.trial_hint", trial=_.duration(settings.trial_days)))
        rows = [
            [nav(_("subscription.btn.trial"), TRIAL)] if trial else [],
            [nav(_("subscription.btn.buy"), BUY)] if shop else [],
            [to_menu(_)],
        ]
        return Screen("\n".join(lines), kb(rows))

    status = state.status
    lines += ["", _(f"subscription.card.status_{status.value}")]
    if state.complimentary:
        lines.append(_("subscription.card.complimentary"))
    elif sub.is_trial:
        lines.append(_("subscription.card.trial"))
    elif state.plan is not None:
        lines.append(_("subscription.card.plan", plan=_.esc(state.plan.title)))
    key = "subscription.card.forever" if state.unlimited else "subscription.card.expires"
    lines.append(_(key, expires=fmt_expiry(_, state)))
    lines.append(_("subscription.card.devices", devices=fmt_devices(_, sub.device_limit)))
    if used_bytes is not None:
        if (sub.traffic_limit_bytes or 0) > 0 or sub.traffic_limit_gb > 0:
            used, limit = fmt_traffic(_, used_bytes), fmt_sub_traffic_limit(_, sub)
            lines.append(_("subscription.card.traffic_limited", used=used, limit=limit))
        else:
            lines.append(_("subscription.card.traffic", used=fmt_traffic(_, used_bytes)))
    renewable = state.can_auto_renew(settings)
    if renewable:
        lines.append(_("subscription.card.auto_renew_on" if sub.auto_renew else "subscription.card.auto_renew_off"))
        plan = state.plan
        if sub.auto_renew and sub.stars_charge_id:
            lines.append(_("subscription.card.auto_renew_stars"))
        elif sub.auto_renew and plan is not None and state.user.balance_kop < plan.price_kop:
            balance, price = _.money(state.user.balance_kop), _.money(plan.price_kop)
            lines.append(_("subscription.card.auto_renew_low", balance=balance, price=price))
    if status == Status.EXPIRED:
        lines += ["", _("subscription.card.expired_hint" if shop else "subscription.card.expired_hint_personal")]
    elif status == Status.DISABLED:
        lines += ["", _("subscription.card.disabled_hint")]

    rows = [
        [nav(_("subscription.btn.connect"), CONNECT, state.sub.id if state.sub else "")]
        if status == Status.ACTIVE
        else [],
        [nav(_("subscription.btn.renew"), BUY, sub.id)]
        if shop and not state.complimentary and not state.unlimited
        else [],
    ]
    if renewable:
        if sub.auto_renew:
            rows.append([Button.cb(_("subscription.btn.auto_renew_off"), SubAct(act="ar0", s=sub.id))])
        else:
            rows.append([Button.cb(_("subscription.btn.auto_renew_on"), SubAct(act="ar1", s=sub.id))])
    if status == Status.ACTIVE:
        rows.append(
            [
                nav(_("subscription.btn.devices"), DEVICES, sub.id),
                Button.cb(_("subscription.btn.rotate"), SubAct(act="rot", s=sub.id)),
            ]
        )
        if shop and (
            settings.traffic_pack_price_kop > 0
            or settings.traffic_reset_price_kop > 0
            or settings.device_addon_price_kop > 0
        ):
            rows.append([nav(_("subscription.btn.addons"), ADDONS, sub.id)])
    if status == Status.DISABLED:
        rows.append([nav(_("subscription.btn.support"), SUPPORT)])
    if state.total_subscriptions > 1:
        rows.append([nav(_("subscription.multi.list_button"), SUB, "list")])
    if shop:
        rows.append([nav(_("subscription.multi.new_button"), BUY, -1)])
    rows.append([nav(_("nav.refresh"), SUB, sub.id), to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def addons_screen(_: Translator, state: SubState, offers: Sequence[Offer], nonce: str) -> Screen:
    sub = state.sub
    if sub is None:
        return Screen(_("error.unavailable"), kb([[to_menu(_)]]))
    lines = [
        _("subscription.addons.title"),
        "",
        _("subscription.addons.balance", balance=_.money(state.user.balance_kop)),
    ]
    if not offers:
        lines.append(_("subscription.addons.unavailable"))
        return Screen("\n".join(lines), kb([[nav(_("nav.back"), SUB, sub.id)], [to_menu(_)]]))
    labels = {
        AddonKind.TRAFFIC_PACK: _("subscription.addons.traffic_pack"),
        AddonKind.TRAFFIC_RESET: _("subscription.addons.traffic_reset"),
        AddonKind.DEVICE_ADDON: _("subscription.addons.device_addon"),
    }
    rows: Rows = []
    for item in offers:
        quantity = (
            _.plural(item.quantity, "plural.gb")
            if item.kind is AddonKind.TRAFFIC_PACK
            else _.plural(item.quantity, "plural.devices")
        )
        rows.append(
            [
                Button.cb(
                    _(
                        "subscription.addons.buy",
                        title=labels[item.kind],
                        quantity=quantity,
                        price=_.money(item.price_kop),
                    ),
                    AddonCb(k=item.kind.value, s=sub.id, n=nonce),
                )
            ]
        )
    rows += [[nav(_("nav.back"), SUB, sub.id)], [to_menu(_)]]
    return Screen("\n".join(lines), kb(rows))


def trial_started(_: Translator, settings: Settings, state: SubState) -> Screen:
    text = _("subscription.trial.started", trial=_.duration(settings.trial_days), expires=fmt_expiry(_, state))
    return Screen(
        text, kb([[nav(_("subscription.btn.connect"), CONNECT, state.sub.id if state.sub else "")], [to_menu(_)]])
    )


def rotate_confirm(_: Translator, subscription_id: int = 0) -> Screen:
    rows: Rows = [
        [Button.cb(_("subscription.rotate.yes"), SubAct(act="rot!", s=subscription_id), style="danger")],
        [back(SUB, _)],
    ]
    return Screen(_("subscription.rotate.confirm"), kb(rows))


def rotated(_: Translator, url: str | None, subscription_id: int = 0) -> Screen:
    text = _("subscription.rotate.done", url=_.esc(url)) if url else _("subscription.rotate.done_no_url")
    rows: Rows = [
        [Button.copy(_("subscription.btn.copy"), url)] if url and len(url) <= MAX_COPY_TEXT else [],
        [nav(_("subscription.btn.connect"), CONNECT, subscription_id)],
        [to_menu(_)],
    ]
    return Screen(text, kb(rows))


# --- plans and checkout ----------------------------------------------------------------------------------------------
def plans_screen(
    _: Translator, state: SubState, plans: Sequence[Plan], *, target: int = 0, prices: Mapping[int, int] | None = None
) -> Screen:
    """Plans with price, per-month price and saving; tapping a plan opens it."""
    if not plans:
        return Screen(_("subscription.plans.empty"), kb([[to_menu(_)]]))
    target = target or (state.sub.id if state.sub else -1)
    reference = reference_monthly_kop(plans, prices)
    lines = [_("subscription.plans.title"), ""]
    rows: Rows = []
    for plan in plans:
        price_kop = _price(plan, prices)
        discount = discount_percent(plan, reference, price_kop)
        price = _.money(price_kop)
        if discount:
            per_month = _.money(monthly_rub_kop(plan, price_kop))
            lines.append(
                _(
                    "subscription.plans.line_discount",
                    title=_.esc(plan.title),
                    price=price,
                    per_month=per_month,
                    discount=discount,
                )
            )
            label = _("subscription.plans.button_discount", title=plan.title, price=price, discount=discount)
        else:
            lines.append(_("subscription.plans.line", title=_.esc(plan.title), price=price))
            label = _("subscription.plans.button", title=plan.title, price=price)
        rows.append([Button.cb(_clip(label), PlanCb(id=plan.id, s=target))])
    if target != -1 and state.sub is not None and state.status == Status.ACTIVE and not state.unlimited:
        lines += ["", _("subscription.plans.extends", expires=_.date(state.sub.expires_at))]
    lines += ["", _("subscription.plans.footer")]
    rows.append([back(SUB, _), to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def plan_screen(
    _: Translator,
    state: SubState,
    plan: Plan,
    gateways: Sequence[PaymentGateway],
    *,
    nonce: str,
    stars_price: int | None,
    wallet_enabled: bool,
    price_kop: int | None = None,
    token: str = "",
    target: int = 0,
) -> Screen:
    """A plan with its terms and the ways to pay (the wallet if it is enough, else providers for the difference).

    ``token`` is the link token of a hidden plan: its pay buttons carry it.
    """
    target = target or (state.sub.id if state.sub else -1)
    balance = state.user.balance_kop
    price = plan.price_kop if price_kop is None else price_kop
    lines = [
        _("subscription.plan.title", title=_.esc(plan.title)),
        "",
        _("subscription.plan.duration", duration=_.duration(plan.duration_days)),
        _("subscription.plan.devices", devices=fmt_devices(_, plan.device_limit)),
        _("subscription.plan.traffic", traffic=fmt_traffic_limit(_, plan.traffic_limit_gb)),
        _("subscription.plan.price", price=_.money(price)),
    ]
    if plan.duration_days > 30 and price > 0:
        lines.append(_("subscription.plan.per_month", per_month=_.money(monthly_rub_kop(plan, price))))
    if target != -1 and state.sub is not None and state.status == Status.ACTIVE and not state.unlimited:
        lines += ["", _("subscription.plan.extends", expires=_.date(state.sub.expires_at))]
        if state.plan is not None and state.plan.id != plan.id:
            lines.append(_("subscription.plan.switch"))
    if target == -1:
        lines += ["", _("subscription.multi.buy_new")]
    elif state.sub is not None:
        lines += ["", _("subscription.multi.renew_target", name=_.esc(state.sub.label or f"#{state.sub.id}"))]
    lines += ["", _("subscription.plan.balance", balance=_.money(balance))]

    rows: Rows = []
    if balance >= price:
        label = _("subscription.plan.pay_balance", price=_.money(price))
        rows.append([Button.cb(label, PayCb(plan=plan.id, via=BALANCE, n=nonce, t=token, s=target), style="success")])
    else:
        missing = price - balance
        if gateways:
            key = "subscription.plan.missing" if balance > 0 else "subscription.plan.choose_method"
            lines += ["", _(key, missing=_.money(missing))]
            for gateway in gateways:
                label = gateway_title(_, gateway)
                if gateway.key == STARS and stars_price:
                    label = _("subscription.plan.stars_button", title=label, stars=_.stars(stars_price))
                rows.append([Button.cb(_clip(label), PayCb(plan=plan.id, via=gateway.key, n=nonce, t=token, s=target))])
        else:
            lines += ["", _("subscription.plan.no_methods", missing=_.money(missing))]
        if wallet_enabled:
            rows.append([nav(_("subscription.btn.top_up"), WALLET)])
    if state.sub is not None:
        rows.append(
            [
                Button.cb(_("subscription.multi.renew_button"), PlanCb(id=plan.id, t=token, s=state.sub.id)),
                Button.cb(_("subscription.multi.new_button"), PlanCb(id=plan.id, t=token, s=-1)),
            ]
        )
    rows.append([back(BUY, _, target), to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def purchased(_: Translator, plan: Plan, state: SubState, *, synced: bool) -> Screen:
    lines = [_("subscription.buy.done", plan=_.esc(plan.title), expires=fmt_expiry(_, state))]
    if not synced:
        lines += ["", _("subscription.buy.syncing")]
    return Screen(
        "\n".join(lines),
        kb([[nav(_("subscription.btn.connect"), CONNECT, state.sub.id if state.sub else "")], [to_menu(_)]]),
    )


def invoice_screen(_: Translator, settings: Settings, payment: Payment, plan: Plan, gateway: PaymentGateway) -> Screen:
    """A created invoice: a link to the provider's payment page and a status check."""
    details = onchain.instructions(gateway, payment)
    if details is not None:
        return onchain_view.transfer_screen(
            _,
            payment,
            details,
            hours=settings.payment_max_pending_hours,
            check=Button.cb(_("subscription.invoice.check"), CheckCb(id=payment.id)),
            footer=[back(BUY, _), to_menu(_)],
        )
    text = _(
        "subscription.invoice.text",
        amount=_.money(payment.amount_kop),
        provider=_.esc(gateway_title(_, gateway)),
        plan=_.esc(plan.title),
        hours=_.plural(max(1, settings.payment_max_pending_hours), "plural.hours"),
    )
    url = payment.confirmation_url
    rows: Rows = [
        [Button.url(_("subscription.invoice.pay"), url, style="success")] if url else [],
        [Button.cb(_("subscription.invoice.check"), CheckCb(id=payment.id))],
        [back(BUY, _), to_menu(_)],
    ]
    return Screen(text, kb(rows))


def manual_transfer_screen(_: Translator, payment: Payment, plan: Plan, details: str) -> Screen:
    text = _(
        "subscription.manual.text",
        amount=_.money(payment.amount_kop),
        plan=_.esc(plan.title),
        details=_.esc(details),
    )
    return Screen(text, kb([[to_menu(_)]]))


def stars_invoice_sent(_: Translator, plan: Plan) -> Screen:
    return Screen(_("subscription.invoice.stars", plan=_.esc(plan.title)), kb([[back(BUY, _), to_menu(_)]]))


def ask_email(_: Translator) -> Screen:
    return Screen(_("subscription.email.ask"), kb([[back(BUY, _)]]))


# --- connection ------------------------------------------------------------------------------------------------------
def inactive_screen(_: Translator, settings: Settings, state: SubState) -> Screen:
    """Shown instead of connection screens while there is no active subscription."""
    if state.status == Status.DISABLED:
        rows: Rows = [[nav(_("subscription.btn.support"), SUPPORT)], [to_menu(_)]]
        return Screen(_("subscription.card.disabled_hint"), kb(rows))
    if state.trial_available(settings):
        text = _("subscription.connect.need_sub_trial", trial=_.duration(settings.trial_days))
        rows = [[nav(_("subscription.btn.trial"), TRIAL)], [nav(_("subscription.btn.buy"), BUY)], [to_menu(_)]]
        return Screen(text, kb(rows))
    if settings.app_mode != "shop":
        return Screen(_("subscription.connect.need_sub_personal"), kb([[to_menu(_)]]))
    return Screen(_("subscription.connect.need_sub"), kb([[nav(_("subscription.btn.buy"), BUY)], [to_menu(_)]]))


def connect_screen(_: Translator, settings: Settings, state: SubState, *, lte: bool) -> Screen:
    """Subscription URL, platform picker and QR."""
    if state.status != Status.ACTIVE:
        return inactive_screen(_, settings, state)
    url = subscription_url(settings, state.token)
    if url is None:
        return Screen(_("subscription.connect.no_url"), kb([[to_menu(_)]]))
    lines = [_("subscription.connect.title"), "", _("subscription.connect.steps"), ""]
    lines.append(_("subscription.connect.url", url=_.esc(url)))
    wl_url = whitelist_url(settings, state.token) if settings.sub_whitelist_separate_url and lte else None
    if wl_url:
        lines += ["", _("subscription.connect.lte_url", url=_.esc(wl_url))]
    elif lte:
        lines += ["", _("subscription.connect.lte")]
    lines += ["", _("subscription.connect.keep_secret")]
    rows: Rows = [
        [Button.copy(_("subscription.btn.copy"), url)] if len(url) <= MAX_COPY_TEXT else [],
        [Button.copy(_("subscription.btn.copy_lte"), wl_url)] if wl_url and len(wl_url) <= MAX_COPY_TEXT else [],
    ]
    rows += grid(
        (Button.cb(platform_title(_, p), ConnCb(p=p.value, s=state.sub.id if state.sub else 0)) for p in PLATFORMS), 2
    )
    rows.append([nav(_("subscription.btn.qr"), QR, state.sub.id if state.sub else "")])
    if settings.sub_show_server_profiles or settings.sub_show_server_subscriptions:
        rows.append([nav(_("subscription.multi.links_button"), LINKS, state.sub.id if state.sub else "")])
    rows.append([back(SUB, _), to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def platform_screen(_: Translator, platform: Platform, subscription_id: int = 0) -> Screen:
    apps = apps_for_platform(platform)
    if not apps:
        return Screen(_("subscription.connect.no_apps"), kb([[back(CONNECT, _)]]))
    text = _("subscription.connect.platform", platform=platform_title(_, platform), app=_.esc(apps[0].title))
    buttons = [
        Button.cb(
            _("subscription.connect.recommended", app=app.title) if index == 0 else app.title,
            ConnCb(p=platform.value, a=app.key, s=subscription_id),
        )
        for index, app in enumerate(apps)
    ]
    rows: Rows = list(grid(buttons, 2))
    rows.append([back(CONNECT, _), to_menu(_)])
    return Screen(text, kb(rows))


def app_screen(
    _: Translator,
    settings: Settings,
    state: SubState,
    platform: Platform,
    app: ClientApp,
    *,
    crypt_link: str | None = None,
) -> Screen:
    """Install-and-import guide for one app on one platform."""
    if state.status != Status.ACTIVE:
        return inactive_screen(_, settings, state)
    url = subscription_url(settings, state.token)
    import_url = open_url(settings, app.key, state.token)
    if url is None or import_url is None:
        return Screen(_("subscription.connect.no_url"), kb([[to_menu(_)]]))
    steps = "subscription.app.steps_tv" if platform in TV_PLATFORMS else "subscription.app.steps"
    lines = [
        _("subscription.app.title", app=_.esc(app.title), platform=platform_title(_, platform)),
        "",
        _(steps, app=_.esc(app.title)),
        "",
        _("subscription.app.manual", url=_.esc(url)),
    ]
    if crypt_link:
        lines += ["", _("subscription.app.crypt", link=_.esc(crypt_link))]
    rows: Rows = [[Button.url(_clip(_("subscription.app.add", app=app.title)), import_url, style="success")]]
    downloads = app.downloads_for(platform)
    rows += grid((Button.url(_clip(_("subscription.app.download", label=d.label)), d.url) for d in downloads), 2)
    rows.append(
        [
            Button.copy(_("subscription.btn.copy"), url) if len(url) <= MAX_COPY_TEXT else None,
            nav(_("subscription.btn.qr"), QR, state.sub.id if state.sub else ""),
        ]
    )
    rows.append([Button.cb(_("nav.back"), ConnCb(p=platform.value, s=state.sub.id if state.sub else 0)), to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def qr_screen(_: Translator, png: bytes, subscription_id: int = 0) -> Screen:
    return Screen(_("subscription.connect.qr"), kb([[back(CONNECT, _), to_menu(_)]]), photo=png)


def known_app(key: str) -> ClientApp | None:
    return APPS.get(key)


def known_platform(value: str) -> Platform | None:
    try:
        return Platform(value)
    except ValueError:
        return None


# --- devices ---------------------------------------------------------------------------------------------------------
def device_label(_: Translator, number: int, device: DeviceInfo) -> str:
    parts = [part for part in (device.platform, device.model, device.os_version) if part]
    name = " ".join(parts) or _("subscription.devices.unknown")
    return f"{number}. {name}" + (f" · {device.ip}" if device.ip else "")


def devices_screen(_: Translator, settings: Settings, state: SubState, panels: Sequence[PanelDevices]) -> Screen:
    """Devices reported by the panels, with unlink buttons."""
    if state.status != Status.ACTIVE or state.sub is None:
        return inactive_screen(_, settings, state)
    lines = [
        _("subscription.devices.title"),
        "",
        _("subscription.devices.limit", limit=fmt_devices(_, state.sub.device_limit)),
    ]
    rows: Rows = []
    listed = [entry for entry in panels if entry.supported]
    if any(entry.devices for entry in listed):
        lines.append("")
        number = 0
        for entry in listed:
            for index, device in enumerate(entry.devices):
                number += 1
                label = device_label(_, number, device)
                lines.append(_.esc(label))
                if entry.can_delete_one:
                    rows.append(
                        [
                            Button.cb(
                                _clip(f"🗑 {label}"), DevCb(act="del", panel=entry.panel_id, i=index, s=state.sub.id)
                            )
                        ]
                    )
    elif listed:
        lines += ["", _("subscription.devices.none")]
    else:
        lines += ["", _("subscription.devices.not_listed")]
    if any(entry.error for entry in panels):
        lines += ["", _("subscription.devices.partial")]
    lines += ["", _("subscription.devices.hint")]
    rows.append([Button.cb(_("subscription.devices.reset_all"), DevCb(act="all", s=state.sub.id))])
    rows.append([back(SUB, _), to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def reset_confirm(_: Translator, state: SubState) -> Screen:
    limit = fmt_devices(_, state.sub.device_limit) if state.sub is not None else _("fmt.unlimited")
    rows: Rows = [
        [
            Button.cb(
                _("subscription.devices.reset_yes"),
                DevCb(act="all!", s=state.sub.id if state.sub else 0),
                style="danger",
            )
        ],
        [back(DEVICES, _)],
    ]
    return Screen(_("subscription.devices.reset_confirm", limit=limit), kb(rows))


def cooldown_text(_: Translator, retry_at: datetime) -> str:
    return _("subscription.devices.cooldown", time=_.datetime(retry_at))
