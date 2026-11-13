"""Plans, purchases, payments and the wallet."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Literal
from uuid import uuid4

from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BufferedInputFile
from fastapi import APIRouter, Query, Request

from app.config import Settings
from app.db.models import LedgerAccount, Payment, PaymentProvider, PaymentStatus, Plan, User
from app.db.session import read_session
from app.modules.miniapp_api import deps
from app.modules.miniapp_api.account import sync_subscription
from app.modules.miniapp_api.deps import GIFTS, PAYMENTS, PLANS, WALLET, error, needs
from app.modules.miniapp_api.schemas import (
    BuyIn,
    BuyOut,
    ChainTransferOut,
    GiftOut,
    ManualTransferOut,
    Ok,
    PaymentOut,
    PlanBrief,
    PlanOut,
    PlansOut,
    ProviderOut,
    SavedCardOut,
    SavedCardsOut,
    TopupIn,
    TopupLimits,
    TransactionOut,
    WalletOut,
)
from app.services import invites, promo, saved_cards, subscriptions, wallet
from app.services import plans as plans_service
from app.services.notifications import AdminEvent
from app.services.payments import onchain, stars
from app.services.payments import service as payments
from app.services.payments.base import GatewayError, PaymentGateway
from app.services.subscriptions import SubErrorCode, SubscriptionError
from app.web.auth import MiniAppUser, in_cabinet
from app.web.deps import Ctx, ReadDb

log = logging.getLogger(__name__)

router = APIRouter(tags=["Mini App: shop"])

CREATE_PAYMENT_LIMIT = (10, 600)  # new invoices per user per 10 minutes
BUY_LIMIT = (20, 600)
PAYMENT_CHECK_LIMIT = (1, 5)  # provider status checks per payment
RECEIPT_LIMIT = (5, 600)  # receipt uploads per user per 10 minutes
HISTORY_PAGE = 20
STARS = PaymentProvider.STARS.value
MANUAL = PaymentProvider.MANUAL.value
MAX_RECEIPT_BYTES = 10 * 1024 * 1024  # Telegram's limit for photos uploaded by a bot
RECEIPT_PATH = "/payments/receipt"
# Accepted receipt formats by their leading bytes: (signature, file name, sent as a photo).
_RECEIPT_SIGNATURES: tuple[tuple[bytes, str, bool], ...] = (
    (b"\xff\xd8\xff", "receipt.jpg", True),
    (b"\x89PNG\r\n\x1a\n", "receipt.png", True),
    (b"%PDF-", "receipt.pdf", False),
)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------
def _gateways(ctx: Any, request: Request) -> list[PaymentGateway]:
    """Enabled gateways; Telegram Stars only exist inside Telegram, so the web cabinet does not get them."""
    registry = getattr(ctx, "gateways", None)
    gateways = list(registry.enabled()) if registry is not None else []
    return [g for g in gateways if not (in_cabinet(request) and g.key == STARS)]


def _gateway(ctx: Any, user: User, key: str, request: Request) -> PaymentGateway:
    registry = getattr(ctx, "gateways", None)
    gateway = registry.get(key) if registry is not None else None
    if gateway is None or (in_cabinet(request) and gateway.key == STARS):
        raise error(ctx, user, 400, "unknown_provider")
    return gateway


def _provider_title(ctx: Any, user: User, gateway: PaymentGateway) -> str:
    return deps.optional_text(ctx, user, f"provider.{gateway.key}") or gateway.title


def _kind(gateway: PaymentGateway) -> Literal["redirect", "stars", "onchain", "manual"]:
    if gateway.key == STARS:
        return "stars"
    if gateway.key == MANUAL:
        return "manual"
    if callable(getattr(gateway, "transfer_instructions", None)):
        return "onchain"
    return "redirect"


def can_save_card(settings: Settings, gateway: PaymentGateway | None) -> bool:
    """The provider can keep a card for auto-renewal and the owner allows it."""
    return settings.saved_cards_enabled and gateway is not None and callable(getattr(gateway, "charge_saved", None))


def _provider_out(ctx: Any, user: User, gateway: PaymentGateway) -> ProviderOut:
    return ProviderOut(
        key=gateway.key,
        title=_provider_title(ctx, user, gateway),
        min_amount_kop=gateway.min_amount_kop,
        recurring=gateway.key == STARS,
        kind=_kind(gateway),
        save_card=can_save_card(ctx.settings, gateway),
    )


def _discounts(visible: list[Plan], prices: dict[int, int]) -> dict[int, int]:
    """Discount of each plan against the most expensive per-month price (usually the shortest plan)."""
    monthly = {
        plan.id: round(prices[plan.id] * 30 / plan.duration_days)
        for plan in visible
        if prices[plan.id] > 0 and plan.duration_days > 0
    }
    base = max(monthly.values(), default=0)
    if base <= 0:
        return {}
    return {pid: max(0, round((1 - price / base) * 100)) for pid, price in monthly.items()}


def _transfer_out(ctx: Any, user: User, payment: Payment, gateway: PaymentGateway | None) -> ChainTransferOut | None:
    details = onchain.instructions(gateway, payment) if gateway is not None else None
    if details is None:
        return None
    _ = deps.translator(ctx, user)
    hours = max(1, int(ctx.settings.payment_max_pending_hours))
    notes = [
        deps.text(ctx, user, "onchain.hint_comment" if details.comment else "onchain.hint_amount"),
        deps.text(ctx, user, "onchain.footer", hours=_.plural(hours, "plural.hours")),
    ]
    return ChainTransferOut(
        network=details.network,
        currency=details.currency,
        address=details.address,
        amount=details.amount,
        comment=details.comment,
        wallet_url=details.wallet_url,
        deep_link=details.deep_link,
        notes=notes,
    )


def payment_out(ctx: Any, user: User, payment: Payment, invoice_link: str | None = None) -> PaymentOut:
    """The payment with what the buyer needs to pay it (blockchain or manual transfer details)."""
    registry = getattr(ctx, "gateways", None)
    gateway = registry.get(payment.provider) if registry is not None else None
    is_open = payment.status in payments.OPEN_STATUSES
    manual = None
    if payment.provider == MANUAL:
        manual = ManualTransferOut(
            details=(ctx.settings.manual_transfer_details or "").strip() if is_open else "",
            receipt_sent=bool(payment.receipt_file_id),
        )
    return PaymentOut(
        id=payment.id,
        provider=payment.provider,
        status=payment.status.value,
        amount_kop=payment.amount_kop,
        plan_id=payment.plan_id,
        is_gift=payment.is_gift,
        is_recurring=payment.is_recurring,
        provider_amount=payment.provider_amount,
        provider_currency=payment.provider_currency,
        url=payment.confirmation_url,
        invoice_link=invoice_link,
        created_at=payment.created_at,
        credited_at=payment.credited_at,
        subscription_id=payment.subscription_id,
        save_card=bool(payment.save_method),
        transfer=_transfer_out(ctx, user, payment, gateway) if is_open else None,
        manual=manual,
    )


def gift_link(ctx: Any, code: str) -> str | None:
    bot_username = getattr(ctx, "bot_username", None)
    return f"https://t.me/{bot_username}?start=gift_{code}" if bot_username else None


async def _balance(ctx: Any, user_id: int, account: LedgerAccount = LedgerAccount.MAIN) -> int:
    async with read_session(ctx.session_factory) as session:
        return await wallet.balance(session, user_id, account)


async def _create_payment(
    ctx: Any,
    user: User,
    gateway: PaymentGateway,
    amount_kop: int,
    *,
    plan: Plan | None = None,
    plan_price_kop: int | None = None,
    is_gift: bool = False,
    is_recurring: bool = False,
    provider_amount: str | None = None,
    description: str | None = None,
    subscription_id: int | None = None,
    new_subscription: bool = False,
    label: str | None = None,
    save_card: bool = False,
) -> PaymentOut:
    """Persist the payment, create the invoice and, for Telegram Stars, an invoice link for the Mini App."""
    settings: Settings = ctx.settings
    try:
        payment = await payments.create_payment(
            ctx.session_factory,
            settings,
            gateway,
            user.tg_id,
            amount_kop,
            plan_id=plan.id if plan is not None else None,
            plan_price_kop=plan_price_kop,
            is_gift=is_gift,
            is_recurring=is_recurring,
            provider_amount=provider_amount,
            description=description,
            subscription_id=subscription_id,
            new_subscription=new_subscription,
            subscription_label=label,
            save_method=save_card,
        )
    except ValueError:
        raise error(
            ctx, user, 400, "bad_amount", min=deps.translator(ctx, user).money(gateway.min_amount_kop)
        ) from None
    except GatewayError:
        raise error(ctx, user, 502, "provider_unavailable") from None
    invoice_link: str | None = None
    if gateway.key == STARS:
        try:
            invoice_link = await ctx.bot.create_invoice_link(**stars.build_invoice_params(payment, plan, settings))
        except (TelegramAPIError, ValueError) as exc:
            log.warning("payment %s: Stars invoice link failed: %s", payment.id, exc)
            await payments.mark_status(ctx.session_factory, payment.id, PaymentStatus.CANCELED)
            raise error(ctx, user, 502, "provider_unavailable") from None
    return payment_out(ctx, user, payment, invoice_link)


# ---------------------------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------------------------
@router.get("/plans", response_model=PlansOut, dependencies=[needs(PLANS)], summary="Plans and payment methods")
async def list_plans(
    request: Request,
    ctx: Ctx,
    user: MiniAppUser,
    db: ReadDb,
    plan_id: Annotated[int | None, Query(description="Also show this hidden plan (deep link)")] = None,
    plan_token: Annotated[str, Query(max_length=64, description="Signed token of the hidden plan's link")] = "",
) -> PlansOut:
    settings: Settings = ctx.settings
    active = await plans_service.list_active(db)
    db_user = await db.get(User, user.tg_id)
    paid = await plans_service.has_paid_before(db, user.tg_id)
    visible = [
        plan
        for plan in active
        if db_user is not None
        and plans_service.available_to(plan, db_user, paid=paid)
        and (
            not plan.is_hidden
            or (plan.id == plan_id and plans_service.link_token_valid(ctx.secret_box, plan.id, plan_token))
        )
    ]
    personal = await promo.personal_discount(db, user.tg_id)
    prices = {plan.id: personal.price(plan.price_kop) for plan in visible}
    discounts = _discounts([p for p in visible if not p.is_hidden], prices)
    payments_on = await deps.feature_on(ctx, PAYMENTS, user.tg_id)
    gateways = _gateways(ctx, request) if payments_on else []
    return PlansOut(
        plans=[
            PlanOut(
                id=p.id,
                title=p.title,
                duration_days=p.duration_days,
                price_kop=prices[p.id],
                monthly_price_kop=round(prices[p.id] * 30 / p.duration_days),
                discount_percent=discounts.get(p.id, 0),
                price_stars=plans_service.discounted_stars_price(p, prices[p.id], settings),
                gift_price_kop=p.price_kop,
                gift_price_stars=plans_service.discounted_stars_price(p, p.price_kop, settings),
                device_limit=p.device_limit,
                traffic_limit_gb=p.traffic_limit_gb,
                includes_tg_proxy=p.includes_tg_proxy,
                hidden=p.is_hidden,
            )
            for p in visible
        ],
        balance_kop=await wallet.balance(db, user.tg_id),
        providers=[_provider_out(ctx, user, g) for g in gateways],
        gifts_available=await deps.feature_on(ctx, GIFTS, user.tg_id),
        topup=TopupLimits(
            min_kop=settings.min_topup_kop,
            max_kop=settings.max_topup_kop,
            quick_amounts_kop=[amount * 100 for amount in settings.topup_quick_amounts_rub],
        ),
    )


@router.post(
    "/plans/{plan_id}/buy",
    response_model=BuyOut,
    dependencies=[needs(PLANS)],
    summary="Buy a plan (or a gift) from the balance or through a payment provider",
)
async def buy_plan(plan_id: int, body: BuyIn, request: Request, ctx: Ctx, user: MiniAppUser) -> BuyOut:
    settings: Settings = ctx.settings
    if body.new_subscription and body.subscription_id is not None:
        raise error(ctx, user, 422, "no_subscription")
    if body.subscription_id is not None:
        async with read_session(ctx.session_factory) as session:
            target = await subscriptions.get_for_user(session, user.tg_id, body.subscription_id)
        if target is None:
            raise error(ctx, user, 404, "no_subscription")
        if not body.gift and invites.is_unlimited(target):
            raise error(ctx, user, 409, "renew_unavailable")
    if body.gift and not await deps.feature_on(ctx, GIFTS, user.tg_id):
        raise error(ctx, user, 404, "feature_disabled")
    async with read_session(ctx.session_factory) as session:
        plan = await plans_service.get(session, plan_id)
        db_user = await session.get(User, user.tg_id)
        balance = await wallet.balance(session, user.tg_id)
        paid = await plans_service.has_paid_before(session, user.tg_id)
        price_kop = (await promo.personal_discount(session, user.tg_id)).price(plan.price_kop) if plan else 0
    if (
        plan is None
        or db_user is None
        or not plan.is_active
        or not plans_service.available_to(plan, db_user, paid=paid)
    ):
        raise error(ctx, user, 404, "plan_not_found")
    # Hidden plans are sold only through their signed link, and never as gifts.
    if plan.is_hidden and (body.gift or not plans_service.link_token_valid(ctx.secret_box, plan.id, body.plan_token)):
        raise error(ctx, user, 404, "plan_not_found")
    # A group discount belongs to the buyer's own tariff.  Gift codes retain the full tariff value for the recipient.
    if body.gift:
        price_kop = plan.price_kop
    if body.provider is None:
        await deps.rate_limit(request, ctx, user, "buy", *BUY_LIMIT)
        return await _buy_from_balance(
            ctx,
            user,
            plan,
            gift=body.gift,
            request_id=body.request_id,
            subscription_id=body.subscription_id,
            new_subscription=body.new_subscription,
            label=body.label,
        )

    if not await deps.feature_on(ctx, PAYMENTS, user.tg_id):
        raise error(ctx, user, 404, "feature_disabled")
    gateway = _gateway(ctx, user, body.provider, request)
    if body.recurring and (gateway.key != STARS or body.gift or plan.duration_days != 30):
        raise error(ctx, user, 400, "recurring_unavailable")
    if body.save_card and (body.gift or body.recurring or not can_save_card(settings, gateway)):
        raise error(ctx, user, 400, "save_card_unavailable")
    await deps.rate_limit(request, ctx, user, "payment", *CREATE_PAYMENT_LIMIT)
    if gateway.key == STARS:
        # Stars pay the whole plan at its Stars price; the balance is left alone.
        price_stars = plans_service.discounted_stars_price(plan, price_kop, settings)
        if price_stars <= 0:
            raise error(ctx, user, 400, "bad_amount", min=deps.translator(ctx, user).money(gateway.min_amount_kop))
        if body.recurring and price_stars > stars.MAX_SUBSCRIPTION_STARS:
            raise error(ctx, user, 400, "recurring_unavailable")
        amount, provider_amount = price_kop, str(price_stars)
    else:
        # Pay only what the balance lacks; the plan is bought from the balance once the money arrives.
        missing = price_kop - balance
        amount = max(missing, gateway.min_amount_kop) if missing > 0 else price_kop
        provider_amount = None
    payment = await _create_payment(
        ctx,
        user,
        gateway,
        amount,
        plan=plan,
        plan_price_kop=None if body.gift else price_kop,
        is_gift=body.gift,
        is_recurring=body.recurring,
        subscription_id=body.subscription_id,
        new_subscription=body.new_subscription and not body.gift,
        label=body.label,
        save_card=body.save_card,
        provider_amount=provider_amount,
        description=deps.text(
            ctx, user, "payment.gift" if body.gift else "payment.plan", brand=settings.brand_name, plan=plan.title
        ),
    )
    log.info("mini app: user %s pays %s for plan %s (gift=%s)", user.tg_id, payment.id, plan.id, body.gift)
    return BuyOut(result="payment", payment=payment, balance_kop=balance)


async def _buy_from_balance(
    ctx: Any,
    user: User,
    plan: Plan,
    *,
    gift: bool,
    request_id: str | None,
    subscription_id: int | None = None,
    new_subscription: bool = False,
    label: str | None = None,
) -> BuyOut:
    """Buy from the balance; the client's ``request_id`` makes a repeated request charge once."""
    settings: Settings = ctx.settings
    ref = request_id or uuid4().hex
    try:
        async with ctx.session_factory.begin() as session:
            row = await session.get(User, user.tg_id)
            assert row is not None
            if not plans_service.available_to(plan, row, paid=await plans_service.has_paid_before(session, row.tg_id)):
                raise error(ctx, user, 404, "plan_not_found")
            if gift:
                code = await promo.create_gift(session, settings, row, plan, f"miniapp:{user.tg_id}:{ref}")
                gift_code = code.code
                sub_id = None
                expires_at = None
            else:
                # Ledger refs are per user, so the client's id needs no user prefix ("purchase:app-<id>").
                payment_ref = f"app-{request_id}" if request_id else None
                purchase = await subscriptions.purchase_plan(
                    session,
                    settings,
                    row,
                    plan,
                    payment_id=payment_ref,
                    subscription_id=subscription_id,
                    new_subscription=new_subscription,
                    label=label,
                    price_kop=(await promo.personal_discount(session, row.tg_id)).price(plan.price_kop),
                )
                gift_code = None
                expires_at = purchase.expires_after
                sub_id = purchase.subscription_id
            balance = await wallet.balance(session, user.tg_id)
    except SubscriptionError as exc:
        if exc.code is SubErrorCode.INSUFFICIENT_FUNDS:
            current = await _balance(ctx, user.tg_id)
            async with read_session(ctx.session_factory) as session:
                price_kop = (await promo.personal_discount(session, user.tg_id)).price(plan.price_kop)
            if gift:
                price_kop = plan.price_kop
            missing = max(0, price_kop - current)
            raise error(
                ctx,
                user,
                402,
                "insufficient_funds",
                extra={"missing_kop": missing, "balance_kop": current},
                missing=deps.translator(ctx, user).money(missing),
            ) from None
        status = {SubErrorCode.NO_PLAN: 404, SubErrorCode.BANNED: 403}.get(exc.code, 409)
        raise error(ctx, user, status, exc.code.value) from None
    if gift_code is not None:
        brief = PlanBrief(id=plan.id, title=plan.title, duration_days=plan.duration_days)
        return BuyOut(
            result="gift",
            gift=GiftOut(code=gift_code, link=gift_link(ctx, gift_code), plan=brief),
            balance_kop=balance,
        )
    if sub_id is not None:
        await sync_subscription(ctx, sub_id)
    log.info("mini app: user %s bought plan %s from the balance", user.tg_id, plan.id)
    return BuyOut(result="purchased", expires_at=expires_at, balance_kop=balance, subscription_id=sub_id)


# ---------------------------------------------------------------------------------------------
# Top-up & payments
# ---------------------------------------------------------------------------------------------
@router.post(
    "/wallet/topup",
    response_model=PaymentOut,
    dependencies=[needs(WALLET, PAYMENTS)],
    summary="Top up the balance",
)
async def topup(body: TopupIn, request: Request, ctx: Ctx, user: MiniAppUser) -> PaymentOut:
    settings: Settings = ctx.settings
    gateway = _gateway(ctx, user, body.provider, request)
    if gateway.key == MANUAL:  # a manual transfer can only pay for a plan: the receipt flow starts from checkout
        raise error(ctx, user, 400, "unknown_provider")
    _ = deps.translator(ctx, user)
    low = max(settings.min_topup_kop, gateway.min_amount_kop)
    if not low <= body.amount_kop <= settings.max_topup_kop:
        raise error(
            ctx,
            user,
            400,
            "topup_range",
            extra={"min_kop": low, "max_kop": settings.max_topup_kop},
            min=_.money(low),
            max=_.money(settings.max_topup_kop),
        )
    if body.save_card and not can_save_card(settings, gateway):
        raise error(ctx, user, 400, "save_card_unavailable")
    await deps.rate_limit(request, ctx, user, "payment", *CREATE_PAYMENT_LIMIT)
    description = deps.text(ctx, user, "payment.topup", brand=settings.brand_name)
    return await _create_payment(ctx, user, gateway, body.amount_kop, description=description, save_card=body.save_card)


@router.get(
    "/payments/{payment_id}",
    response_model=PaymentOut,
    dependencies=[needs(PAYMENTS)],
    summary="Payment status (checks the provider while it is pending)",
)
async def payment_status(payment_id: str, request: Request, ctx: Ctx, user: MiniAppUser) -> PaymentOut:
    payment = await _own_payment(ctx, user, payment_id)
    gateway = getattr(ctx, "gateways", None)
    provider = gateway.get(payment.provider) if gateway is not None else None
    # Like the bot's "check payment": ask the provider once it knows the payment or can find it by our id.
    known = payment.provider_payment_id or getattr(provider, "polls_by_payment_id", False)
    if payment.status in payments.OPEN_STATUSES and provider is not None and provider.supports_polling and known:
        limiter = getattr(request.app.state, "rate_limiter", None)
        allowed = limiter is None or (await limiter.hit(f"miniapp:paycheck:{payment.id}", *PAYMENT_CHECK_LIMIT)).allowed
        if allowed:
            try:
                status = await provider.fetch(payment)
            except GatewayError as exc:
                log.info("payment %s: status check failed: %s", payment.id, exc)
            else:
                await payments.apply_provider_status(
                    ctx.session_factory, ctx.settings, payment, status, ctx.on_payment_credited
                )
                payment = await _own_payment(ctx, user, payment_id)
    return payment_out(ctx, user, payment)


@router.post(
    RECEIPT_PATH,
    response_model=PaymentOut,
    dependencies=[needs(PAYMENTS)],
    summary="Attach the receipt of a manual card/SBP transfer (raw JPEG, PNG or PDF request body)",
)
async def upload_receipt(
    request: Request,
    ctx: Ctx,
    user: MiniAppUser,
    payment_id: Annotated[str, Query(min_length=1, max_length=36)],
) -> PaymentOut:
    """The file is sent to the buyer's chat with the bot (Telegram keeps it) and staff review it there."""
    payment = await _own_payment(ctx, user, payment_id)
    if payment.provider != MANUAL or payment.status not in payments.OPEN_STATUSES:
        raise error(ctx, user, 409, "receipt_not_expected")
    await deps.rate_limit(request, ctx, user, "receipt", *RECEIPT_LIMIT)
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > MAX_RECEIPT_BYTES:
            raise error(ctx, user, 413, "receipt_too_large", max=f"{MAX_RECEIPT_BYTES // (1024 * 1024)} MB")
    if not data:
        raise error(ctx, user, 400, "receipt_empty")
    kind = next((item for item in _RECEIPT_SIGNATURES if data.startswith(item[0])), None)
    if kind is None:
        raise error(ctx, user, 415, "receipt_bad_type")
    _sig, filename, as_photo = kind
    amount = deps.translator(ctx, user).money(payment.amount_kop)
    caption = deps.text(ctx, user, "receipt.caption", amount=amount)
    upload = BufferedInputFile(bytes(data), filename=filename)
    file_id, media_type = await _send_receipt(ctx, user, upload, caption, as_photo)
    if file_id is None:
        raise error(ctx, user, 502, "receipt_failed")
    async with ctx.session_factory.begin() as session:
        row = await session.get(Payment, payment.id, with_for_update=True)
        if row is None or row.user_id != user.tg_id or row.status not in payments.OPEN_STATUSES:
            raise error(ctx, user, 409, "receipt_not_expected")
        row.receipt_file_id = file_id
        row.receipt_media_type = media_type
    log.info("mini app: user %s sent a receipt for payment %s", user.tg_id, payment.id)
    deps.notify_admins(
        ctx,
        AdminEvent.PAYMENTS,
        "admin.receipt",
        id=payment.id,
        amount=ctx.i18n.translator(ctx.settings.default_language, ctx.settings.zone).money(payment.amount_kop),
        user_id=user.tg_id,
        username=f"@{user.username}" if user.username else "",
    )
    return payment_out(ctx, user, await _own_payment(ctx, user, payment.id))


async def _send_receipt(
    ctx: Any, user: User, upload: BufferedInputFile, caption: str, as_photo: bool
) -> tuple[str | None, str]:
    """Send the receipt to the buyer's chat; returns its Telegram file id and how it was sent."""
    bot = ctx.bot
    if as_photo:
        try:
            message = await bot.send_photo(user.tg_id, upload, caption=caption)
        except TelegramBadRequest as exc:
            log.info("receipt of user %s is not a valid photo, sending it as a file: %s", user.tg_id, exc)
        except TelegramAPIError as exc:
            log.warning("receipt of user %s was not delivered: %s", user.tg_id, exc)
            return None, "photo"
        else:
            photo = getattr(message, "photo", None)
            return (str(photo[-1].file_id) if photo else None), "photo"
    try:
        message = await bot.send_document(user.tg_id, upload, caption=caption)
    except TelegramAPIError as exc:
        log.warning("receipt of user %s was not delivered: %s", user.tg_id, exc)
        return None, "document"
    document = getattr(message, "document", None)
    return (str(document.file_id) if document is not None else None), "document"


async def _own_payment(ctx: Any, user: User, payment_id: str) -> Payment:
    async with read_session(ctx.session_factory) as session:
        payment = await session.get(Payment, payment_id) if len(payment_id) <= 36 else None
    if payment is None or payment.user_id != user.tg_id:
        raise error(ctx, user, 404, "payment_not_found")
    return payment


# ---------------------------------------------------------------------------------------------
# Wallet
# ---------------------------------------------------------------------------------------------
@router.get("/wallet", response_model=WalletOut, dependencies=[needs(WALLET)], summary="Balances and history")
async def wallet_history(
    ctx: Ctx,
    user: MiniAppUser,
    db: ReadDb,
    limit: Annotated[int, Query(ge=1, le=100)] = HISTORY_PAGE,
    before_id: Annotated[int | None, Query(ge=1)] = None,
) -> WalletOut:
    rows = await wallet.history(db, user.tg_id, limit=limit + 1, before_id=before_id)
    page, more = rows[:limit], len(rows) > limit

    def title(kind: str) -> str:
        return deps.optional_text(ctx, user, f"tx.{kind}") or kind

    return WalletOut(
        balance_kop=await wallet.balance(db, user.tg_id),
        partner_balance_kop=await wallet.balance(db, user.tg_id, LedgerAccount.REFERRAL),
        items=[
            TransactionOut(
                id=row.id,
                kind=row.kind.value,
                account=row.account.value,
                amount_kop=row.amount_kop,
                title=title(row.kind.value),
                created_at=row.created_at,
            )
            for row in page
        ],
        next_before_id=page[-1].id if more and page else None,
    )


@router.get(
    "/wallet/cards", response_model=SavedCardsOut, dependencies=[needs(WALLET)], summary="Saved cards for auto-renewal"
)
async def list_cards(ctx: Ctx, user: MiniAppUser, db: ReadDb) -> SavedCardsOut:
    rows = await saved_cards.list_active(db, user.tg_id)
    return SavedCardsOut(
        enabled=bool(ctx.settings.saved_cards_enabled),
        items=[
            SavedCardOut(
                id=row.id,
                provider=row.provider,
                title=row.title,
                created_at=row.created_at,
                last_used_at=row.last_used_at,
            )
            for row in rows
        ],
    )


@router.delete("/wallet/cards/{card_id}", response_model=Ok, dependencies=[needs(WALLET)], summary="Remove a card")
async def remove_card(card_id: int, ctx: Ctx, user: MiniAppUser) -> Ok:
    async with ctx.session_factory.begin() as session:
        removed = await saved_cards.deactivate(session, user.tg_id, card_id)
    if not removed:
        raise error(ctx, user, 404, "card_not_found")
    log.info("mini app: user %s removed saved card %s", user.tg_id, card_id)
    return Ok()


@router.get("/wallet/options", dependencies=[needs(WALLET)])
async def wallet_options(request: Request, ctx: Ctx, user: MiniAppUser) -> dict[str, Any]:
    settings = ctx.settings
    gateways = _gateways(ctx, request) if await deps.feature_on(ctx, PAYMENTS, user.tg_id) else []
    return {
        "providers": [_provider_out(ctx, user, g) for g in gateways if g.key != MANUAL],
        "topup": TopupLimits(
            min_kop=settings.min_topup_kop,
            max_kop=settings.max_topup_kop,
            quick_amounts_kop=[x * 100 for x in settings.topup_quick_amounts_rub],
        ),
    }
