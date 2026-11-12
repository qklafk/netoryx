"""``/start`` payloads (deep links ``t.me/<bot>?start=<payload>``).

Registration links (only for users who just joined): ``ref_<code>`` referral, ``c_<code>`` ad campaign. Actions:
``gift_<code>``, ``promo_<code>``, ``inv_<code>`` (invitation, personal mode). Screens: ``plan_<id>`` (hidden
plans: ``plan_<id>-<token>``), ``renew`` / ``buy``, ``connect``, ``sub``, ``proxy``. A bare unknown payload of a new
user is tried as a referral code (links of older bots carried the plain code).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum

from app.config import Settings
from app.core.context import AppContext
from app.core.i18n import Translator
from app.db.models import Plan, PromoKind, PurchaseKind, User
from app.db.session import read_session
from app.services import campaigns, invites, notifications, promo, referrals, subscriptions
from app.services.invites import InviteOutcome
from app.services.notify import safe_send
from app.services.promo import PromoOutcome
from app.services.referrals import ReferralOutcome, ReferralResult
from app.utils.time import utcnow

log = logging.getLogger(__name__)

# A /start replayed after the legal consent or the channel gate still counts as the first one this long after
# registration (the user was registered by the intercepted update).
FRESH_WINDOW = timedelta(hours=1)
MAX_PAYLOAD_LENGTH = 64


class Kind(StrEnum):
    NONE = "none"
    REFERRAL = "ref"
    CAMPAIGN = "campaign"
    GIFT = "gift"
    PROMO = "promo"
    INVITE = "invite"
    IMPORT = "import"
    PLAN = "plan"
    BUY = "buy"
    CONNECT = "connect"
    SUB = "sub"
    PROXY = "proxy"
    UNKNOWN = "unknown"


_PREFIXES: tuple[tuple[str, Kind], ...] = (
    ("ref_", Kind.REFERRAL),
    ("ref-", Kind.REFERRAL),
    ("r_", Kind.REFERRAL),
    ("gift_", Kind.GIFT),
    ("promo_", Kind.PROMO),
    ("import_", Kind.IMPORT),
    (invites.DEEP_LINK_PREFIX, Kind.INVITE),
    (campaigns.DEEP_LINK_PREFIX, Kind.CAMPAIGN),
    ("plan_", Kind.PLAN),
)
_WORDS: dict[str, Kind] = {
    "renew": Kind.BUY,
    "buy": Kind.BUY,
    "connect": Kind.CONNECT,
    "sub": Kind.SUB,
    "proxy": Kind.PROXY,
}


@dataclass(frozen=True, slots=True)
class Payload:
    kind: Kind
    value: str = ""

    @property
    def plan_id(self) -> int | None:
        number = self.value.partition("-")[0]
        return int(number) if self.kind == Kind.PLAN and number.isdigit() else None

    @property
    def plan_token(self) -> str:
        """The token of a ``plan_<id>-<token>`` link (hidden plans open only with it)."""
        return self.value.partition("-")[2] if self.kind == Kind.PLAN else ""


def parse(payload: str | None) -> Payload:
    text = (payload or "").strip()[:MAX_PAYLOAD_LENGTH]
    if not text:
        return Payload(Kind.NONE)
    lowered = text.lower()
    if lowered in _WORDS:
        return Payload(_WORDS[lowered])
    for prefix, kind in _PREFIXES:
        if lowered.startswith(prefix) and len(text) > len(prefix):
            return Payload(kind, text[len(prefix) :])
    return Payload(Kind.UNKNOWN, text)


def is_fresh(user: User, created: bool, now: datetime | None = None) -> bool:
    """The user joined with this ``/start`` (or with the intercepted one that is being replayed)."""
    return created or (now or utcnow()) - user.created_at <= FRESH_WINDOW


@dataclass(slots=True)
class StartResult:
    """What the payload did: notices for the user (shown above the next screen) and a subscription to push."""

    notices: list[str] = field(default_factory=list)
    sync_sub_id: int | None = None
    referral: ReferralResult | None = None
    campaign: str | None = None


# --- registration links -----------------------------------------------------------------------------------------------
async def apply_registration(
    ctx: AppContext, settings: Settings, _: Translator, user_id: int, payload: Payload, *, fresh: bool
) -> StartResult:
    """Referral and campaign attribution of a newly registered user (no-op for anyone else)."""
    result = StartResult()
    is_ref = payload.kind == Kind.REFERRAL or (payload.kind == Kind.UNKNOWN and fresh)
    if not fresh or settings.app_mode != "shop" or not (is_ref or payload.kind == Kind.CAMPAIGN):
        return result
    async with ctx.session_factory.begin() as session:
        user = await session.get(User, user_id)
        if user is None:
            return result
        if is_ref:
            result.referral = await referrals.apply_on_registration(session, settings, user, payload.value, is_new=True)
        elif await campaigns.attribute(session, user_id, payload.value, is_new=True):
            result.campaign = campaigns.normalize_code(payload.value)
    referral = result.referral
    if referral is not None and referral.outcome == ReferralOutcome.APPLIED:
        result.notices.append(_referral_notice(_, referral))
        if referral.invitee_bonus_days:
            async with read_session(ctx.session_factory) as session, session.begin():
                sub = await subscriptions.get_for_user(session, user_id)
            result.sync_sub_id = sub.id if sub is not None else None
        if referral.inviter_id is not None:
            await _tell_inviter(ctx, settings, referral)
    return result


def _referral_notice(_: Translator, referral: ReferralResult) -> str:
    if referral.invitee_bonus_days:
        return _("core_user.ref.joined_days", days=_.duration(referral.invitee_bonus_days))
    if referral.invitee_bonus_kop:
        return _("core_user.ref.joined_money", amount=_.money(referral.invitee_bonus_kop))
    return _("core_user.ref.joined")


async def _tell_inviter(ctx: AppContext, settings: Settings, referral: ReferralResult) -> None:
    inviter_id = referral.inviter_id
    if inviter_id is None:
        return
    async with read_session(ctx.session_factory) as session, session.begin():
        inviter = await session.get(User, inviter_id)
    if inviter is None or inviter.bot_blocked:
        return
    _ = ctx.i18n.translator(inviter.language, settings.zone)
    if referral.inviter_bonus_days:
        text = _("core_user.ref.inviter_days", days=_.duration(referral.inviter_bonus_days))
    elif referral.inviter_bonus_kop:
        text = _("core_user.ref.inviter_money", amount=_.money(referral.inviter_bonus_kop))
    else:
        text = _("core_user.ref.inviter")
    await safe_send(ctx.bot, inviter_id, text, session_factory=ctx.session_factory)


# --- gift and promo codes ---------------------------------------------------------------------------------------------
_PROMO_ERRORS: dict[PromoOutcome, str] = {
    PromoOutcome.NOT_FOUND: "core_user.code.not_found",
    PromoOutcome.EXPIRED: "core_user.code.expired",
    PromoOutcome.EXHAUSTED: "core_user.code.exhausted",
    PromoOutcome.ALREADY_USED: "core_user.code.already_used",
    PromoOutcome.ONLY_NEW: "core_user.code.only_new",
}


async def redeem_code(ctx: AppContext, settings: Settings, _: Translator, user_id: int, code: str) -> StartResult:
    """Redeem a gift or promo code: money goes to the wallet, days extend the subscription."""
    result = StartResult()
    plan: Plan | None = None
    gift = False
    bonus_kop = bonus_days = 0
    expires: datetime | None = None
    async with ctx.session_factory.begin() as session:
        user = await session.get(User, user_id)
        if user is None:
            return result
        outcome, code_row = await promo.redeem(session, settings, user, code)
        if outcome is PromoOutcome.OK and code_row is not None:
            gift = code_row.kind is PromoKind.GIFT
            bonus_kop, bonus_days = code_row.bonus_kop, code_row.bonus_days
            if bonus_days > 0:
                plan = await session.get(Plan, code_row.plan_id) if code_row.plan_id is not None else None
                kind = PurchaseKind.GIFT_REDEEM if gift else PurchaseKind.PROMO
                purchase = await subscriptions.grant_days(session, settings, user, bonus_days, kind=kind, plan=plan)
                sub = await subscriptions.get_for_user(session, purchase.user_id)
                result.sync_sub_id = sub.id if sub is not None else None
                expires = purchase.expires_after
    if outcome is not PromoOutcome.OK or code_row is None:
        result.notices.append(_(_PROMO_ERRORS.get(outcome, "core_user.code.not_found")))
        return result
    if gift and expires is not None:
        title = _.esc(plan.title) if plan is not None else _.duration(bonus_days)
        result.notices.append(_("core_user.code.gift_ok", plan=title, expires=_.date(expires)))
    else:
        if expires is not None:
            result.notices.append(_("core_user.code.days_ok", days=_.duration(bonus_days), expires=_.date(expires)))
        if bonus_kop > 0:
            result.notices.append(_("core_user.code.money_ok", amount=_.money(bonus_kop)))
    return result


# --- invitations ------------------------------------------------------------------------------------------------------
async def redeem_invite(ctx: AppContext, settings: Settings, _: Translator, user_id: int, code: str) -> StartResult:
    result = StartResult()
    async with ctx.session_factory.begin() as session:
        user = await session.get(User, user_id)
        if user is None:
            return result
        redeemed = await invites.redeem(session, settings, user, code)
        note = redeemed.invite.note if redeemed.invite is not None else None
        sub = redeemed.subscription
        sub_id = sub.id if sub is not None else None
        unlimited = sub is not None and invites.is_unlimited(sub)
        expires = sub.expires_at if sub is not None else None
        username = user.username
    if redeemed.outcome == InviteOutcome.ALREADY_USED:
        result.notices.append(_("core_user.invite.already_used"))
        return result
    if not redeemed.ok or expires is None:
        result.notices.append(_("access.invite_invalid"))
        return result
    result.sync_sub_id = sub_id
    if unlimited:
        result.notices.append(_("core_user.invite.accepted_forever"))
    else:
        result.notices.append(_("core_user.invite.accepted", expires=_.date(expires)))
    admin = ctx.i18n.translator(ctx.i18n.default_lang, settings.zone)
    who = f"@{username}" if username else str(user_id)
    text = admin("core_user.admin.invite_redeemed", user=admin.esc(who), user_id=user_id, note=admin.esc(note or "-"))
    notifications.notify(ctx, notifications.AdminEvent.USERS, text)
    return result
