"""Web admin API of "Мой налог" receipts (mounted under ``/api/admin/npd_receipts``)."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.permissions import Permission
from app.db.models import TaxReceipt, TaxReceiptStatus
from app.modules.admin_api.deps import Actor, bad_request, kick_jobs, not_found, require
from app.services import tax_receipts
from app.services.npd import NpdAuthError, NpdClient, NpdError, NpdSession
from app.web.deps import PageParams, ReadDb

router = APIRouter(tags=["npd receipts"])

Viewer = Annotated[Actor, Depends(require(Permission.PAYMENTS_VIEW))]
Editor = Annotated[Actor, Depends(require(Permission.PAYMENTS_VIEW, Permission.PAYMENTS_EDIT))]
Owner = Annotated[Actor, Depends(require(Permission.SETTINGS_SECRETS))]
ReceiptId = Annotated[int, Path(ge=1)]
JOB_ID = "npd_receipts.process"


class ReceiptOut(BaseModel):
    model_config = {"from_attributes": True}

    id: int
    payment_id: str
    user_id: int
    provider: str
    status: str
    amount_kop: int
    service_name: str
    operation_time: datetime
    receipt_uuid: str | None
    receipt_url: str | None
    attempts: int
    next_attempt_at: datetime | None
    last_error: str | None
    registered_at: datetime | None
    cancelled_at: datetime | None
    created_at: datetime


class ReceiptPage(BaseModel):
    items: list[ReceiptOut]
    total: int
    limit: int
    offset: int


class OverviewOut(BaseModel):
    enabled: bool
    logged_in: bool
    inn: str | None
    send_to_user: bool
    providers: list[str]
    counts: dict[str, int]


class SmsStartIn(BaseModel):
    phone: str = Field(pattern=r"^7\d{10}$", description="79001234567")


class SmsStartOut(BaseModel):
    challenge_token: str
    expires_in: int


class SmsVerifyIn(BaseModel):
    phone: str = Field(pattern=r"^7\d{10}$")
    challenge_token: str = Field(min_length=1, max_length=4096)
    code: str = Field(pattern=r"^\d{4,8}$")


class LoginOut(BaseModel):
    inn: str | None


def _client(device: str) -> NpdClient:
    return NpdClient(device)


async def _audit(actor: Actor, action: str, target_id: int | str | None = None, **details: Any) -> None:
    async with actor.ctx.session_factory.begin() as session:
        await actor.audit(session, action, "tax_receipt", target_id, **details)


@router.get("/overview", response_model=OverviewOut)
async def overview(actor: Viewer, session: ReadDb) -> OverviewOut:
    """Whether receipts are on, the cabinet login state and receipts per status."""
    settings = actor.ctx.settings
    counts = {
        str(status): int(n)
        for status, n in await session.execute(
            select(TaxReceipt.status, func.count(TaxReceipt.id)).group_by(TaxReceipt.status)
        )
    }
    stored = await tax_receipts.load_session(actor.ctx.session_factory, actor.ctx.secret_box)
    return OverviewOut(
        enabled=bool(settings.npd_enabled),
        logged_in=stored is not None,
        inn=(stored.inn if stored else None) or settings.npd_inn,
        send_to_user=bool(settings.npd_send_to_user),
        providers=list(settings.npd_providers),
        counts=counts,
    )


@router.get("", response_model=ReceiptPage)
async def list_receipts(
    actor: Viewer, session: ReadDb, page: PageParams, status: TaxReceiptStatus | None = None, user_id: int | None = None
) -> ReceiptPage:
    """Receipts, newest first."""
    stmt = select(TaxReceipt)
    if status is not None:
        stmt = stmt.where(TaxReceipt.status == status.value)
    if user_id is not None:
        stmt = stmt.where(TaxReceipt.user_id == user_id)
    total = int(await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = await session.scalars(stmt.order_by(TaxReceipt.id.desc()).limit(page.limit).offset(page.offset))
    return ReceiptPage(
        items=[ReceiptOut.model_validate(r) for r in rows], total=total, limit=page.limit, offset=page.offset
    )


@router.post("/{receipt_id}/retry", response_model=ReceiptOut)
async def retry(actor: Editor, receipt_id: ReceiptId) -> ReceiptOut:
    """Try a failed receipt again now."""
    async with actor.ctx.session_factory.begin() as session:
        row = await tax_receipts.request_retry(session, receipt_id)
        if row is None:
            raise not_found("Receipt not found")
        await actor.audit(session, "npd.retry", "tax_receipt", receipt_id)
        out = ReceiptOut.model_validate(row)
    kick_jobs(actor.ctx, JOB_ID)
    return out


@router.post("/{receipt_id}/cancel", response_model=ReceiptOut)
async def cancel(actor: Editor, receipt_id: ReceiptId) -> ReceiptOut:
    """Annul a registered receipt (refund) or drop one that was not registered yet."""
    async with actor.ctx.session_factory.begin() as session:
        row = await tax_receipts.request_cancel(session, receipt_id)
        if row is None:
            raise not_found("Receipt not found")
        await actor.audit(session, "npd.cancel", "tax_receipt", receipt_id)
        out = ReceiptOut.model_validate(row)
    kick_jobs(actor.ctx, JOB_ID)
    return out


# --- cabinet login (owner only; no transaction is open during the calls) -------------------------------------
async def _store(actor: Actor, npd: NpdSession, method: str) -> LoginOut:
    await tax_receipts.save_session(actor.ctx.session_factory, actor.ctx.secret_box, npd)
    await _audit(actor, "npd.login", None, method=method)
    kick_jobs(actor.ctx, JOB_ID)
    return LoginOut(inn=npd.inn)


@router.post("/auth/sms/start", response_model=SmsStartOut)
async def sms_start(actor: Owner, body: SmsStartIn) -> SmsStartOut:
    """Send a login code by SMS to the taxpayer's phone."""
    device = await tax_receipts.device_id(actor.ctx.session_factory)
    async with _client(device) as client:
        try:
            token, expires = await client.start_sms(body.phone)
        except NpdError as exc:
            raise bad_request("npd_error", str(exc)) from exc
    return SmsStartOut(challenge_token=token, expires_in=expires)


@router.post("/auth/sms/verify", response_model=LoginOut)
async def sms_verify(actor: Owner, body: SmsVerifyIn) -> LoginOut:
    """Finish the SMS login; the tokens are stored encrypted."""
    device = await tax_receipts.device_id(actor.ctx.session_factory)
    async with _client(device) as client:
        try:
            npd = await client.verify_sms(body.phone, body.challenge_token, body.code)
        except NpdError as exc:
            raise bad_request("npd_error", str(exc)) from exc
    return await _store(actor, npd, "sms")


@router.post("/auth/password", response_model=LoginOut)
async def password_login(actor: Owner) -> LoginOut:
    """Log in with the INN and password from the settings (also checks them)."""
    settings = actor.ctx.settings
    inn = (settings.npd_inn or "").strip()
    password = settings.npd_password.get_secret_value() if settings.npd_password else ""
    if not inn or not password:
        raise bad_request("npd_not_configured", "Set the INN and the password first")
    device = await tax_receipts.device_id(actor.ctx.session_factory)
    async with _client(device) as client:
        try:
            npd = await client.login_password(inn, password)
        except NpdAuthError as exc:
            raise bad_request("npd_auth", str(exc)) from exc
        except NpdError as exc:
            raise bad_request("npd_error", str(exc)) from exc
    return await _store(actor, npd, "password")


@router.post("/auth/logout", response_model=LoginOut)
async def logout(actor: Owner) -> LoginOut:
    """Forget the stored tokens."""
    await tax_receipts.save_session(actor.ctx.session_factory, actor.ctx.secret_box, None)
    await _audit(actor, "npd.logout")
    return LoginOut(inn=None)
