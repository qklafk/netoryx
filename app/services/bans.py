"""Banning users: one code path for manual bans, bulk bans and the external blacklist.

A ban bumps every subscription's revision so the reconciler disables the panel accounts, and marks the user's
Telegram proxy accounts for the next sync. Everything happens in the caller's transaction; network I/O is left to
the reconcilers.

The external blacklist remembers the users it banned under ``blacklist:ban:<tg_id>`` in the ``kv`` table
(``banned``), so it can lift exactly those bans when an ID leaves the list. A manual unban turns the marker into
``exempt`` (the blacklist must not ban that user again); a manual ban drops it (the ban is now the admin's).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import User
from app.services import kv, provisioning, subscriptions, tg_proxy, tg_socks

BLACKLIST_MARK_PREFIX = "blacklist:ban:"
MARK_BANNED = "banned"
MARK_EXEMPT = "exempt"


def blacklist_mark_key(tg_id: int) -> str:
    return f"{BLACKLIST_MARK_PREFIX}{tg_id}"


async def set_banned(session: AsyncSession, user: User, banned: bool) -> bool:
    """Ban or unban ``user`` (row should be locked by the caller); True if the state changed."""
    if user.is_banned == banned:
        return False
    user.is_banned = banned
    for sub in await subscriptions.list_for_user(session, user.tg_id):
        sub.revision += 1
        await provisioning.ensure_provisions(session, sub)
    await tg_proxy.mark_user_dirty(session, user.tg_id)
    await tg_socks.mark_user_dirty(session, user.tg_id)
    return True


async def set_banned_manually(session: AsyncSession, user: User, banned: bool) -> bool:
    """An administrator's ban / unban: like :func:`set_banned`, and takes the decision over from the blacklist."""
    changed = await set_banned(session, user, banned)
    key = blacklist_mark_key(user.tg_id)
    mark = await kv.get(session, key)
    if banned:
        if mark is not None:
            await kv.delete(session, key)
    elif mark != MARK_EXEMPT and (changed or mark is not None):
        # An explicit unban always wins over the blacklist, whoever imposed the ban.
        await kv.set(session, key, MARK_EXEMPT)
    return changed
