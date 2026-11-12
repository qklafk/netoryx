"""Messages the subscription jobs send: expiry reminders, auto-renewal receipts and admin alerts."""

from __future__ import annotations

from app.config import Settings
from app.core.i18n import Translator
from app.core.ui import Screen
from app.db.models import Plan, Subscription
from app.keyboards import kb, nav, to_menu
from app.modules.subscription.callbacks import BUY, CONNECT, WALLET
from app.modules.subscription.views import Rows
from app.services.panels.inventory import HealthChange


def reminder(
    _: Translator,
    settings: Settings,
    sub: Subscription,
    plan: Plan | None,
    balance_kop: int,
    days: int,
    *,
    wallet_enabled: bool,
) -> Screen:
    """Expiry reminder (``days`` left) or the "expired" notice (``days == 0``)."""
    shop = settings.app_mode == "shop"
    rows: Rows = []
    if days == 0:
        text = _("subscription.remind.expired" if shop else "subscription.remind.expired_personal")
        if shop:
            rows.append([nav(_("subscription.btn.renew"), BUY)])
        rows.append([to_menu(_)])
        return Screen(text, kb(rows))

    lines = [_("subscription.remind.soon", left=_.plural(days, "plural.days"), date=_.datetime(sub.expires_at)), ""]
    if not shop:
        lines.append(_("subscription.remind.personal"))
    elif sub.auto_renew and plan is not None and sub.stars_charge_id:
        lines.append(_("subscription.remind.stars"))
    elif sub.auto_renew and plan is not None and not sub.is_trial:
        if balance_kop >= plan.price_kop:
            lines.append(_("subscription.remind.auto_ok", plan=_.esc(plan.title), price=_.money(plan.price_kop)))
        else:
            lines.append(_("subscription.remind.auto_low", balance=_.money(balance_kop), price=_.money(plan.price_kop)))
            if wallet_enabled:
                rows.append([nav(_("subscription.btn.top_up"), WALLET)])
    elif sub.is_trial:
        lines.append(_("subscription.remind.trial"))
    else:
        lines.append(_("subscription.remind.renew"))
    if shop:
        rows.append([nav(_("subscription.btn.renew"), BUY)])
    rows.append([to_menu(_)])
    return Screen("\n".join(lines), kb(rows))


def renewed(_: Translator, plan: Plan, sub: Subscription, balance_kop: int) -> Screen:
    text = _(
        "subscription.renewed",
        plan=_.esc(plan.title),
        expires=_.date(sub.expires_at),
        price=_.money(plan.price_kop),
        balance=_.money(balance_kop),
    )
    return Screen(text, kb([[nav(_("subscription.btn.connect"), CONNECT)], [to_menu(_)]]))


def health_alert(_: Translator, change: HealthChange) -> str:
    """Admin alert for a panel / endpoint / probe health transition."""
    state = "up" if change.ok else "down"
    return _(
        f"subscription.alert.{change.kind}_{state}",
        name=_.esc(change.name),
        error=_.esc(change.error or "-"),
    )
