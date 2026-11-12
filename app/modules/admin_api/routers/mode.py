"""Product mode switch wizard: readiness checklist, preview of member changes, apply, roll back."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.core.permissions import Permission
from app.db.models import User
from app.db.session import read_session
from app.modules.admin_api import notify
from app.modules.admin_api.deps import Actor, bad_request, conflict, kick_reconcile, require
from app.modules.admin_api.schemas.mode import (
    ApplyIn,
    ApplyOut,
    CheckItemOut,
    LastSwitchOut,
    MemberChangeOut,
    PreviewOut,
    ReadinessOut,
    SwitchIn,
)
from app.modules.subscription.stars_renewal import cancel_stars_renewal
from app.services import mode_switch
from app.services.mode_switch import Mode, ModeSwitchError, NotReady

router = APIRouter(prefix="/mode", tags=["admin: mode"])

Viewer = Annotated[Actor, Depends(require(Permission.SETTINGS))]
Owner = Annotated[Actor, Depends(require(Permission.SETTINGS, Permission.SETTINGS_SECRETS))]


def _readiness(settings: Any, readiness: mode_switch.Readiness) -> ReadinessOut:
    return ReadinessOut(
        current_mode=settings.app_mode,
        ready=readiness.ready,
        missing=readiness.missing,
        items=[CheckItemOut(key=i.key, ok=i.ok, required=i.required, detail=i.detail) for i in readiness.items],
    )


async def _plan(actor: Actor, body: SwitchIn) -> mode_switch.SwitchPlan:
    settings = actor.ctx.settings
    if body.target == settings.app_mode:
        raise conflict("same_mode", f"The bot already works in {body.target} mode")
    async with read_session(actor.ctx.session_factory) as session:
        try:
            if body.target == Mode.SHOP:
                return await mode_switch.preview_to_shop(
                    session,
                    settings,
                    default_policy=body.default_policy,
                    per_user=body.per_user,
                    grace_until=body.grace_until,
                )
            return await mode_switch.preview_to_personal(session, settings)
        except ModeSwitchError as exc:
            raise bad_request("invalid_switch", str(exc)) from None


@router.get("/readiness", response_model=ReadinessOut)
async def readiness(actor: Viewer) -> ReadinessOut:
    """What is still missing before sales can be opened (plans, payments, legal pages, a permanent domain...)."""
    settings = actor.ctx.settings
    async with read_session(actor.ctx.session_factory) as session:
        return _readiness(settings, await mode_switch.readiness_for_shop(session, settings))


@router.post("/preview", response_model=PreviewOut)
async def preview(body: SwitchIn, actor: Viewer) -> PreviewOut:
    """What the switch would change: settings, each member's access, auto-renewals turned off."""
    plan = await _plan(actor, body)
    ids = [m.user_id for m in plan.members]
    async with read_session(actor.ctx.session_factory) as session:
        names = (
            dict((await session.execute(select(User.tg_id, User.username).where(User.tg_id.in_(ids)))).all())
            if ids
            else {}
        )
    return PreviewOut(
        target=plan.target,
        readiness=_readiness(actor.ctx.settings, plan.readiness),
        settings_changes=plan.settings_changes,
        members=[
            MemberChangeOut(
                user_id=m.user_id,
                username=names.get(m.user_id),
                subscription_id=m.subscription_id,
                policy=m.policy,
                expires_before=m.expires_before,
                expires_after=m.expires_after,
            )
            for m in plan.members
        ],
        auto_renew_off=len(plan.auto_renew_off),
        notify_users=len(plan.notify_user_ids) if body.notify_users else 0,
    )


def _grace_notice(ctx: Any, when: datetime) -> notify.TextBuilder:
    def build(lang: str) -> str:
        return notify.t(ctx, "admin_api.notify.mode_shop_convert", lang, date=notify.date(ctx, when, lang))

    return build


def _stars_canceler(actor: Actor) -> mode_switch.StarsCanceler:
    ctx = actor.ctx

    async def cancel(user_id: int) -> bool:
        return await cancel_stars_renewal(ctx.bot, ctx.session_factory, user_id)

    return cancel


def _setter(actor: Actor) -> Any:
    async def set_setting(key: str, value: Any) -> Any:
        return await actor.ctx.settings_provider.set(key, value, actor.tg_id, source="web", ip=actor.ip)

    return set_setting


@router.post("/apply", response_model=ApplyOut)
async def apply(body: ApplyIn, actor: Owner) -> ApplyOut:
    """Switch the product mode. The previous state is kept for a one-click rollback."""
    ctx = actor.ctx
    plan = await _plan(actor, body)
    try:
        result = await mode_switch.apply(
            ctx.session_factory,
            ctx.settings,
            plan,
            actor.tg_id,
            set_setting=_setter(actor),
            cancel_stars=_stars_canceler(actor),
            force=body.force,
            source="web",
            ip=actor.ip,
        )
    except NotReady as exc:
        raise conflict("not_ready", "Not ready: " + ", ".join(exc.missing)) from None
    notified = 0
    if body.notify_users and result.notify_user_ids:
        notified = len(result.notify_user_ids)
        if result.target == Mode.PERSONAL:
            notify.notify_users(ctx, result.notify_user_ids, notify.text(ctx, "admin_api.notify.mode_personal"))
        else:
            ends = {m.user_id: m.expires_after for m in plan.members}
            for user_id in result.notify_user_ids:
                notify.notify_user(ctx, user_id, _grace_notice(ctx, ends[user_id]))
    kick_reconcile(ctx)
    return ApplyOut(
        target=result.target,
        converted=result.converted,
        auto_renew_disabled=result.auto_renew_disabled,
        notified=notified,
        settings_failed=result.settings_failed,
    )


@router.get("/last", response_model=LastSwitchOut)
async def last_switch(actor: Viewer) -> LastSwitchOut:
    """The last switch that can be rolled back."""
    snapshot = await mode_switch.last_switch(actor.ctx.session_factory)
    if not snapshot:
        return LastSwitchOut(available=False)
    at = snapshot.get("at")
    return LastSwitchOut(
        available=True,
        target=snapshot.get("target"),
        at=datetime.fromisoformat(at) if at else None,
        actor_id=snapshot.get("actor_id"),
        settings=snapshot.get("settings") or {},
        subscriptions=len(snapshot.get("subscriptions") or {}),
    )


@router.post("/rollback", response_model=LastSwitchOut)
async def rollback(actor: Owner) -> LastSwitchOut:
    """Undo the last switch: settings and member subscriptions go back to how they were."""
    ctx = actor.ctx
    if not await mode_switch.rollback(
        ctx.session_factory, actor.tg_id, set_setting=_setter(actor), source="web", ip=actor.ip
    ):
        raise conflict("nothing_to_rollback", "There is no switch to roll back")
    kick_reconcile(ctx)
    return LastSwitchOut(available=False)
