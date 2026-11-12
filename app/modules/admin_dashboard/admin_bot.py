"""Dashboard screens: KPIs from :func:`app.services.stats.dashboard` and a per-day table for the last week."""

from __future__ import annotations

from aiogram import F
from aiogram.types import CallbackQuery
from sqlalchemy import func, select

from app.core.context import AppContext
from app.core.filters import HasPermission, Permission
from app.core.i18n import Translator
from app.core.ui import Screen, render
from app.db.models import Panel, Payment, PaymentStatus
from app.db.session import read_session
from app.keyboards import Btn, Nav, kb, nav
from app.modules.admin.common import admin_router, to_admin
from app.services import stats
from app.services.provisioning import is_panel_down
from app.utils.time import utcnow

DASHBOARD_NAV = "adm_dash"
DAYS_NAV = "adm_dash_days"
DAYS = 7

router = admin_router("admin_dashboard")


def percent(value: float) -> str:
    return f"{value * 100:.1f}".rstrip("0").rstrip(".")


async def dashboard_text(ctx: AppContext, _: Translator) -> str:
    settings = ctx.settings
    now = utcnow()
    async with read_session(ctx.session_factory) as session, session.begin():
        data = await stats.dashboard(session, settings, now)
        pending = int(
            await session.scalar(
                select(func.count(Payment.id)).where(Payment.status.in_((PaymentStatus.CREATED, PaymentStatus.PENDING)))
            )
            or 0
        )
        panels = list(await session.scalars(select(Panel).where(Panel.is_active.is_(True))))
    healthy = sum(1 for panel in panels if not is_panel_down(panel, settings))
    lines = [
        _("admin_dashboard.header", time=_.datetime(now)),
        "",
        _(
            "admin_dashboard.users",
            total=_.number(data.users_total),
            today=_.number(data.users_new_today),
            d7=_.number(data.users_new_7d),
            d30=_.number(data.users_new_30d),
            blocked=_.number(data.users_blocked_bot),
        ),
        _(
            "admin_dashboard.subscriptions",
            active=_.number(data.subscriptions_active),
            paid=_.number(data.subscriptions_paid),
            trial=_.number(data.subscriptions_trial),
        ),
    ]
    if settings.app_mode == "shop":
        lines += [
            _(
                "admin_dashboard.revenue",
                today=_.money(data.revenue_today_kop),
                d7=_.money(data.revenue_7d_kop),
                d30=_.money(data.revenue_30d_kop),
                total=_.money(data.revenue_total_kop),
            ),
            _(
                "admin_dashboard.mrr",
                mrr=_.money(data.mrr_kop),
                arppu=_.money(data.arppu_kop),
                paying=_.number(data.paying_users),
            ),
            _("admin_dashboard.churn", churned=_.number(data.churned_30d), rate=percent(data.churn_rate_30d)),
            _(
                "admin_dashboard.conversion",
                rate=percent(data.trial_conversion),
                converted=_.number(data.trial_converted),
                trials=_.number(data.trial_users),
            ),
            _(
                "admin_dashboard.wallets",
                total=_.money(data.wallet_total_kop),
                partner=_.money(data.partner_balance_total_kop),
            ),
            _("admin_dashboard.donations", amount=_.money(data.donations_30d_kop)),
            _("admin_dashboard.pending", count=_.number(pending)),
        ]
    lines.append(_("admin_dashboard.panels", healthy=healthy, total=len(panels)))
    if settings.app_mode == "shop" and data.providers_30d:
        lines += ["", _("admin_dashboard.providers_title")]
        lines += [
            _(
                "admin_dashboard.provider_row",
                provider=_.esc(item.provider),
                count=_.number(item.payments),
                amount=_.money(item.amount_kop),
            )
            for item in data.providers_30d
        ]
    if data.top_referrers:
        lines += ["", _("admin_dashboard.referrers_title")]
        lines += [
            _(
                "admin_dashboard.referrer_row",
                name=_.esc(f"@{user.username}" if user.username else user.first_name or user.tg_id),
                user_id=user.tg_id,
                count=_.number(count),
            )
            for user, count in data.top_referrers
        ]
    return "\n".join(lines)


def _keyboard(_: Translator) -> list[list[Btn]]:
    return [
        [nav(_("nav.refresh"), DASHBOARD_NAV), nav(_("admin_dashboard.days_button"), DAYS_NAV)],
        [to_admin(_)],
    ]


@router.callback_query(Nav.filter(F.to == DASHBOARD_NAV), HasPermission(Permission.VIEW_DASHBOARD))
async def show_dashboard(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    text = await dashboard_text(ctx, _)
    await render(callback, Screen(text, kb(_keyboard(_))))


@router.callback_query(Nav.filter(F.to == DAYS_NAV), HasPermission(Permission.VIEW_DASHBOARD))
async def show_days(callback: CallbackQuery, ctx: AppContext, _: Translator) -> None:
    settings = ctx.settings
    async with read_session(ctx.session_factory) as session, session.begin():
        days = await stats.daily(session, settings, DAYS)
    key = "admin_dashboard.day_row" if settings.app_mode == "shop" else "admin_dashboard.day_row_personal"
    lines = [_("admin_dashboard.days_title", days=_.plural(DAYS, "plural.days")), ""]
    for day in reversed(days):
        lines.append(
            _(
                key,
                day=day.day.strftime("%d.%m"),
                registrations=_.number(day.registrations),
                payments=_.number(day.payments),
                revenue=_.money(day.revenue_kop),
                active=_.number(day.active_subscriptions),
            )
        )
    legend = "admin_dashboard.days_legend" if settings.app_mode == "shop" else "admin_dashboard.days_legend_personal"
    lines += ["", _(legend)]
    markup = kb([[nav(_("nav.back"), DASHBOARD_NAV)], [to_admin(_)]])
    await render(callback, Screen("\n".join(lines), markup))
