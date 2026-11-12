"""Preview and adopt existing panel accounts without changing their remote credentials or counters."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.models import KeyValue, Panel, PanelKind, Provision, ProvisionState, Subscription, SubscriptionStatus, User
from app.db.session import SessionFactory, read_session
from app.services import kv
from app.services.invites import UNLIMITED_EXPIRES_AT
from app.services.panels.base import PanelError
from app.services.panels.marzban import MarzbanBackend, parse_account, user_uuid
from app.utils.crypto import SecretBox

CLAIM_LIFETIME = timedelta(days=30)
GIB = 1024**3


class ImportError(ValueError):
    """An account cannot safely be adopted."""


@dataclass(frozen=True, slots=True)
class Selection:
    username: str
    user_id: int | None = None
    label: str | None = None


def record_key(panel_id: int, username: str) -> str:
    return f"import:{panel_id}:{hashlib.sha256(username.encode()).hexdigest()[:24]}"


def _claim_key(token: str) -> str:
    return "importc:" + hashlib.sha256(token.encode()).hexdigest()[:48]


def _reason(raw: dict[str, Any]) -> str | None:
    if not user_uuid(raw):
        return "Only accounts with a VLESS UUID can be imported"
    if raw.get("status") == "on_hold":
        return "On-hold accounts must be activated on the panel before importing"
    if raw.get("status") not in ("active", "expired", "disabled", "limited"):
        return "This account status is not supported"
    return None


async def _panel(session_factory: SessionFactory, registry: Any, panel_id: int) -> MarzbanBackend:
    async with read_session(session_factory) as session:
        panel = await session.get(Panel, panel_id)
    if panel is None or panel.kind != PanelKind.MARZBAN:
        raise ImportError("Choose a Marzban panel")
    backend = await registry.get(panel_id)
    if not isinstance(backend, MarzbanBackend) and not callable(getattr(backend, "list_users", None)):
        raise ImportError("This panel does not support user import")
    return backend


def _decode(value: str | None, secret_box: SecretBox) -> dict[str, Any]:
    return json.loads(secret_box.decrypt(value)) if value else {}


async def preview(session_factory: SessionFactory, registry: Any, panel_id: int) -> dict[str, Any]:
    backend = await _panel(session_factory, registry, panel_id)
    accounts = await backend.list_users()
    async with read_session(session_factory) as session:
        provisions = (
            await session.execute(
                select(Provision.remote_email, Subscription.user_id, Subscription.id)
                .join(Subscription, Subscription.id == Provision.subscription_id)
                .where(Provision.panel_id == panel_id)
            )
        ).all()
    adopted = {email: (user_id, sub_id) for email, user_id, sub_id in provisions}
    items = []
    for raw in accounts:
        account = parse_account(raw)
        owner, sub_id = adopted.get(account.email, (None, None))
        reason = _reason(raw)
        items.append(
            {
                "username": account.email,
                "status": str(raw.get("status") or "unknown"),
                "expires_at": account.expires_at.isoformat() if account.expires_at else None,
                "traffic_limit_bytes": account.traffic_limit_bytes,
                "used_bytes": account.used_bytes,
                "subscription_url": account.subscription_url,
                "user_id": owner,
                "subscription_id": sub_id,
                "supported": reason is None,
                "reason": reason,
            }
        )
    return {"items": items, "total": len(items)}


async def _adopt(
    session: AsyncSession, secret_box: SecretBox, panel_id: int, raw: dict[str, Any], user_id: int, label: str | None
) -> Subscription:
    user = await session.scalar(select(User).where(User.tg_id == user_id).with_for_update())
    if user is None:
        raise ImportError("The Telegram user must first register with the bot")
    if user.is_banned:
        raise ImportError("A banned user cannot claim an account")
    identity = user_uuid(raw)
    if identity is None:
        raise ImportError("The account has no VLESS identity")
    # A shared UUID may represent the same purchased subscription on several panels. Never reassign its owner.
    sub = await session.scalar(select(Subscription).where(Subscription.client_uuid == identity))
    if sub is not None:
        sub = await session.scalar(
            select(Subscription)
            .where(Subscription.id == sub.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if sub is not None and sub.user_id != user_id:
        raise ImportError("This VPN identity is already owned by another user")
    account = parse_account(raw)
    existing = await session.scalar(
        select(Provision).where(Provision.panel_id == panel_id, Provision.remote_email == account.email)
    )
    if existing is not None:
        if sub is None or existing.subscription_id != sub.id:
            raise ImportError("This panel account is already linked to a different subscription")
        return sub
    if sub is None:
        first = await session.scalar(select(Subscription.id).where(Subscription.user_id == user_id).limit(1)) is None
        expiry = account.expires_at or UNLIMITED_EXPIRES_AT
        status = SubscriptionStatus.ACTIVE
        if raw.get("status") == "disabled":
            status = SubscriptionStatus.DISABLED
        elif expiry <= datetime.now(UTC):
            status = SubscriptionStatus.EXPIRED
        sub = Subscription(
            user_id=user_id,
            label=(label or account.email)[:64],
            sub_token=user.sub_token if first else secrets.token_urlsafe(24),
            is_legacy=False,
            client_uuid=identity,
            status=status,
            expires_at=expiry,
            device_limit=0,
            traffic_limit_gb=(account.traffic_limit_bytes + GIB - 1) // GIB,
            traffic_limit_bytes=account.traffic_limit_bytes,
            auto_renew=False,
            is_trial=False,
            revision=1,
            traffic_epoch=0,
            traffic_exhausted_epoch=0 if raw.get("status") == "limited" else None,
        )
        session.add(sub)
        await session.flush()
    # The account is adopted at its current revision: importing never resets traffic or writes to the panel.
    enabled = ProvisionState.ENABLED if account.enabled else ProvisionState.DISABLED
    session.add(
        Provision(
            subscription_id=sub.id,
            panel_id=panel_id,
            inbound_id=None,
            inbound_key=0,
            remote_email=account.email,
            remote_id=account.remote_id,
            desired_state=enabled,
            actual_state=enabled,
            synced_revision=sub.revision,
            traffic_epoch=sub.traffic_epoch,
            links_cache=secret_box.encrypt(json.dumps({"uuid": sub.client_uuid, "links": raw.get("links") or []})),
        )
    )
    return sub


async def apply(
    session_factory: SessionFactory,
    registry: Any,
    settings: Settings,
    secret_box: SecretBox,
    panel_id: int,
    selections: list[Selection],
    request_id: str,
    *,
    bot_username: str = "",
) -> dict[str, Any]:
    request_key = "importreq:" + hashlib.sha256(f"{panel_id}:{request_id}".encode()).hexdigest()[:48]
    async with read_session(session_factory) as session:
        saved = await kv.get(session, request_key)
    if saved:
        return _decode(saved, secret_box)
    backend = await _panel(session_factory, registry, panel_id)
    accounts = {str(raw.get("username") or ""): raw for raw in await backend.list_users()}
    result: dict[str, Any] = {"imported": 0, "staged": 0, "items": [], "errors": []}
    async with session_factory.begin() as session:
        if not await kv.add(session, request_key, ""):
            stored = await session.scalar(select(KeyValue).where(KeyValue.key == request_key).with_for_update())
            if stored is not None and stored.value:
                return _decode(stored.value, secret_box)
        for choice in selections:
            try:
                async with session.begin_nested():
                    raw = accounts.get(choice.username)
                    if raw is None:
                        raise ImportError("The account no longer exists on the panel")
                    reason = _reason(raw)
                    if reason:
                        raise ImportError(reason)
                    key = record_key(panel_id, choice.username)
                    await kv.set(session, key.replace("import:", "importname:", 1), choice.username)
                    await kv.add(session, key, "")
                    row = await session.scalar(select(KeyValue).where(KeyValue.key == key).with_for_update())
                    assert row is not None
                    record = _decode(row.value, secret_box)
                    old_owner = record.get("user_id")
                    if old_owner is not None and choice.user_id not in (None, old_owner):
                        raise ImportError("This account was already imported for another user")
                    already = bool(record.get("subscription_id"))
                    claim_url = None
                    if old_owner is not None:
                        sub_id, owner = record["subscription_id"], old_owner
                    elif choice.user_id is not None:
                        sub = await _adopt(session, secret_box, panel_id, raw, choice.user_id, choice.label)
                        sub_id, owner = sub.id, choice.user_id
                        record.update(user_id=owner, subscription_id=sub_id)
                    else:
                        token = secrets.token_urlsafe(24)
                        # Invalidate any earlier unredeemed link when the owner explicitly issues another one.
                        if record.get("claim_key"):
                            await kv.delete(session, record["claim_key"])
                        claim_key = _claim_key(token)
                        expires = (datetime.now(UTC) + CLAIM_LIFETIME).isoformat()
                        await kv.set(
                            session,
                            claim_key,
                            secret_box.encrypt(json.dumps({"record_key": key, "expires_at": expires})),
                        )
                        record.update(
                            raw=raw, panel_id=panel_id, label=choice.label, claim_key=claim_key, expires_at=expires
                        )
                        claim_url = f"https://t.me/{bot_username}?start=import_{token}" if bot_username else token
                        sub_id, owner = None, None
                    row.value = secret_box.encrypt(json.dumps(record))
                    result["imported" if owner is not None else "staged"] += 1
                    result["items"].append(
                        {
                            "username": choice.username,
                            "user_id": owner,
                            "subscription_id": sub_id,
                            "claim_url": claim_url,
                            "already_imported": already,
                        }
                    )
            except (ImportError, PanelError) as exc:
                result["errors"].append({"username": choice.username, "message": str(exc)})
            except IntegrityError:
                result["errors"].append(
                    {
                        "username": choice.username,
                        "message": "This account was imported concurrently; refresh the preview",
                    }
                )
        await kv.set(session, request_key, secret_box.encrypt(json.dumps(result)))
    return result


async def claim(
    session_factory: SessionFactory,
    settings: Settings,
    secret_box: SecretBox,
    user_id: int,
    token: str,
    *,
    registry: Any | None = None,
) -> int:
    if not re_token(token):
        raise ImportError("Invalid claim link")
    current_raw: dict[str, Any] | None = None
    if registry is not None:
        # A claim may be redeemed weeks after staging: re-read expiry and traffic without holding a transaction.
        async with read_session(session_factory) as session:
            staged_claim = await kv.get(session, _claim_key(token))
            claim_data = _decode(staged_claim, secret_box)
            staged_record = await kv.get(session, claim_data["record_key"]) if claim_data else None
            staged = _decode(staged_record, secret_box)
        if staged.get("raw"):
            backend = await _panel(session_factory, registry, staged["panel_id"])
            remote = await backend.get_account(None, str(staged["raw"]["username"]))
            if remote is None or remote.uuid != user_uuid(staged["raw"]):
                raise ImportError("The source account changed; ask the operator for a new claim link")
            current_raw = dict(remote.raw)
    async with session_factory.begin() as session:
        claim_row = await session.scalar(select(KeyValue).where(KeyValue.key == _claim_key(token)).with_for_update())
        if claim_row is None:
            raise ImportError("This claim link is invalid or has already been used")
        data = _decode(claim_row.value, secret_box)
        if datetime.fromisoformat(data["expires_at"]) <= datetime.now(UTC):
            raise ImportError("This claim link has expired")
        row = await session.scalar(select(KeyValue).where(KeyValue.key == data["record_key"]).with_for_update())
        if row is None:
            raise ImportError("The staged account no longer exists")
        record = _decode(row.value, secret_box)
        if record.get("user_id") is not None:
            if record["user_id"] != user_id:
                raise ImportError("This account has already been claimed")
            return int(record["subscription_id"])
        panel = await session.get(Panel, record["panel_id"])
        if panel is None or panel.kind != PanelKind.MARZBAN:
            raise ImportError("The source panel no longer exists")
        raw = current_raw or record["raw"]
        if _reason(raw):
            raise ImportError(_reason(raw) or "This account cannot be imported")
        sub = await _adopt(session, secret_box, panel.id, raw, user_id, record.get("label"))
        record.update(user_id=user_id, subscription_id=sub.id)
        record.pop("raw", None)
        row.value = secret_box.encrypt(json.dumps(record))
        await session.delete(claim_row)
        return sub.id


def re_token(value: str) -> bool:
    return 16 <= len(value) <= 64 and all(c.isascii() and (c.isalnum() or c in "_-") for c in value)


async def protected_accounts(session: AsyncSession, panel_id: int, secret_box: SecretBox | None = None) -> set[str]:
    """Account names reserved by staging records; raw names are also stored in the key's companion row."""
    rows = await session.scalars(select(KeyValue.value).where(KeyValue.key.startswith(f"importname:{panel_id}:")))
    return set(rows)


async def purge_expired_claims(session_factory: SessionFactory, secret_box: SecretBox) -> int:
    """Remove expired claim links and their unclaimed encrypted snapshots in small transactions."""
    removed = 0
    now = datetime.now(UTC)
    async with session_factory.begin() as session:
        claims = list(await session.scalars(select(KeyValue).where(KeyValue.key.startswith("importc:")).limit(1000)))
        for row in claims:
            data = _decode(row.value, secret_box)
            if datetime.fromisoformat(data["expires_at"]) > now:
                continue
            source = await session.scalar(select(KeyValue).where(KeyValue.key == data["record_key"]).with_for_update())
            if source is not None:
                record = _decode(source.value, secret_box)
                if record.get("claim_key") == row.key:
                    record.pop("raw", None)
                    record.pop("claim_key", None)
                    source.value = secret_box.encrypt(json.dumps(record))
            await session.delete(row)
            removed += 1
    return removed
