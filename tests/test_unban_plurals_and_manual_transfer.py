"""Regression tests: blacklist verdict cache after an unban, plural forms of fallback texts, manual transfers."""

from __future__ import annotations

from app.core.i18n import I18n
from app.db.models import User
from app.db.session import SessionFactory
from app.services import bans, blacklist
from tests.conftest import make_settings
from tests.test_blacklist import _cache, _settings, _user
from tests.test_mod_miniapp_payment_methods import USER_ID, Env, env, make_plans  # noqa: F401


async def test_unbanned_user_is_not_blocked_by_a_cached_verdict(session_factory: SessionFactory) -> None:
    settings = _settings()
    await _user(session_factory, 9100, settings)
    cache = _cache({9100: ""})
    blocked, _report = await blacklist.check_user(session_factory, settings, 9100, cache)
    assert blocked
    async with session_factory.begin() as s:
        user = await s.get(User, 9100)
        assert user is not None
        await bans.set_banned_manually(s, user, False)
    blocked, _report = await blacklist.check_user(session_factory, settings, 9100, cache)
    assert not blocked


def test_fallback_text_uses_plural_rules_of_the_language_that_supplies_it() -> None:
    i18n = I18n(None)
    i18n.load_files()
    i18n._files["kk"].pop("plural.days.one", None)
    i18n._files["kk"].pop("plural.days.other", None)
    assert i18n.plural(2, "kk", "plural.days") == i18n.plural(2, "ru", "plural.days")
    assert i18n.plural(5, "kk", "plural.days") == i18n.plural(5, "ru", "plural.days")


async def test_wallet_top_up_by_manual_transfer_is_refused(env: Env) -> None:  # noqa: F811
    refused = await env.post("/wallet/topup", {"amount_kop": 300_00, "provider": "manual"})
    assert refused.status_code == 400 and refused.json()["error"] == "unknown_provider"
    keys = {p["key"] for p in (await env.get("/wallet/options")).json()["providers"]}
    assert "manual" not in keys and "cryptobot" in keys
    ids = await make_plans(env)
    plans_keys = {p["key"] for p in (await env.get("/plans")).json()["providers"]}
    assert "manual" in plans_keys
    assert (await env.post(f"/plans/{ids['month']}/buy", {"provider": "manual"})).status_code == 200


def test_bot_payment_picker_offers_no_manual_transfer() -> None:
    from app.modules.payments import flow
    from app.services.payments.manual import ManualTransferGateway

    order = flow.Order(purpose=flow.Purpose.TOPUP, amount_kop=300_00, balance_kop=0)
    assert flow.method_for(order, ManualTransferGateway(), make_settings()) is None
