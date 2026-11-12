from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.db.models import PromoActivation, PromoCode, PromoKind, Purchase, PurchaseKind
from app.services import plans
from tests.test_mod_webapi_support import Api, add_user, api, ctx

__all__ = ["api", "ctx"]


# ---------------------------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------------------------
async def test_plan_crud_reorder_and_stats(api: Api) -> None:
    created = await api.post(
        "/plans",
        json={
            "title": " 3 месяца ",
            "duration_days": 90,
            "price_kop": 49900,
            "is_hidden": True,
            "includes_tg_proxy": False,
            "availability_rule": "allowlist",
            "allowed_user_ids": [5, 6],
        },
    )
    assert created.status_code == 201
    item = created.json()
    plan_id = item["plan"]["id"]
    assert item["plan"]["title"] == "3 месяца" and item["plan"]["is_hidden"] is True
    assert item["monthly_price_kop"] == 16633 and item["effective_stars"] > 0
    assert item["plan"]["availability_rule"] == "allowlist" and item["plan"]["allowed_user_ids"] == [5, 6]
    payload = plans.link_payload(api.ctx.secret_box, plan_id)
    assert item["deep_link"] == f"https://t.me/test_vpn_bot?start={payload}"

    other = (await api.post("/plans", json={"title": "Месяц", "duration_days": 30, "price_kop": 19900})).json()
    patched = await api.patch(
        f"/plans/{plan_id}",
        json={"price_kop": 39900, "price_stars": 250, "is_hidden": False, "availability_rule": "invited"},
    )
    assert patched.json()["plan"]["price_kop"] == 39900 and patched.json()["effective_stars"] == 250
    assert patched.json()["plan"]["availability_rule"] == "invited"
    cleared = (await api.patch(f"/plans/{plan_id}", json={"clear_price_stars": True})).json()
    assert cleared["plan"]["price_stars"] is None
    assert (await api.patch(f"/plans/{plan_id}", json={})).status_code == 400
    assert (await api.post("/plans", json={"title": "x", "duration_days": 0, "price_kop": 1})).status_code == 422

    order = (await api.post("/plans/reorder", json={"plan_ids": [other["plan"]["id"], plan_id]})).json()
    assert [p["plan"]["id"] for p in order] == [other["plan"]["id"], plan_id]
    assert (await api.post("/plans/reorder", json={"plan_ids": [999]})).status_code == 400

    archived = (await api.post(f"/plans/{plan_id}/archive")).json()
    assert archived["plan"]["is_active"] is False
    listed = (await api.get("/plans", params={"include_archived": False})).json()
    assert [p["plan"]["id"] for p in listed] == [other["plan"]["id"]]
    assert (await api.post(f"/plans/{plan_id}/restore")).json()["plan"]["is_active"] is True

    await add_user(api.ctx, 5, sub_days=10, plan_id=plan_id)
    async with api.ctx.session_factory.begin() as s:
        s.add(
            Purchase(
                user_id=5,
                plan_id=plan_id,
                kind=PurchaseKind.PURCHASE,
                price_kop=39900,
                days=90,
                expires_after=datetime.now(UTC) + timedelta(days=90),
            )
        )
    stats = (await api.get(f"/plans/{plan_id}")).json()["stats"]
    assert stats == {"active_subscriptions": 1, "purchases": 1, "revenue_kop": 39900}
    in_use = await api.delete(f"/plans/{plan_id}")
    assert in_use.status_code == 409
    assert (await api.delete(f"/plans/{other['plan']['id']}")).status_code == 200
    assert (await api.get(f"/plans/{other['plan']['id']}")).status_code == 404


# ---------------------------------------------------------------------------------------------
# Promo codes, gifts, campaigns
# ---------------------------------------------------------------------------------------------
async def test_promo_codes(api: Api) -> None:
    created = await api.post("/promo", json={"code": "spring", "bonus_days": 7, "max_uses": 10})
    assert created.status_code == 201
    item = created.json()
    assert item["promo"]["code"] == "SPRING" and item["deep_link"].endswith("start=promo_SPRING")
    assert (await api.post("/promo", json={"code": "spring", "bonus_days": 1})).status_code == 409
    assert (await api.post("/promo", json={"bonus_kop": 0, "bonus_days": 0})).status_code == 400
    random = (await api.post("/promo", json={"bonus_kop": 10000})).json()
    assert len(random["promo"]["code"]) >= 6

    await add_user(api.ctx, 7, username="u7")
    async with api.ctx.session_factory.begin() as s:
        s.add(PromoActivation(promo_id=item["promo"]["id"], user_id=7))
        s.add(PromoCode(code="GIFTCODE1", kind=PromoKind.GIFT, bonus_days=30, created_by=7))
    listed = (await api.get("/promo", params={"q": "spr"})).json()
    assert listed["total"] == 1 and listed["items"][0]["activations"] == 1
    gifts = (await api.get("/promo", params={"kind": "gift"})).json()
    assert [g["promo"]["code"] for g in gifts["items"]] == ["GIFTCODE1"]
    who = (await api.get(f"/promo/{item['promo']['id']}/activations")).json()
    assert who["items"][0]["username"] == "u7"
    off = (await api.put(f"/promo/{item['promo']['id']}/active", json={"is_active": False})).json()
    assert off["promo"]["is_active"] is False
    async with api.ctx.session_factory() as s:
        assert (await s.scalar(select(PromoCode.is_active).where(PromoCode.code == "SPRING"))) is False


async def test_personal_discount_groups(api: Api) -> None:
    await add_user(api.ctx, 71, username="discounted")
    await add_user(api.ctx, 72, username="also_discounted")
    created = await api.post(
        "/promo-groups",
        json={"title": "VIP", "discount_percent": 25, "user_ids": [71]},
    )
    assert created.status_code == 201
    group = created.json()
    assert group["members"] == 1 and group["discount_percent"] == 25
    assert (await api.post("/promo-groups", json={"title": "VIP", "discount_percent": 10})).status_code == 409
    assert (
        await api.post("/promo-groups", json={"title": "bad", "discount_percent": 10, "user_ids": [999]})
    ).status_code == 400

    updated = await api.patch(f"/promo-groups/{group['id']}", json={"user_ids": [71, 72], "is_active": False})
    assert updated.status_code == 200
    assert updated.json()["members"] == 2 and updated.json()["is_active"] is False
    listed = (await api.get("/promo-groups")).json()
    assert [item["id"] for item in listed] == [group["id"]]
    assert (await api.delete(f"/promo-groups/{group['id']}")).status_code == 204


async def test_campaigns(api: Api) -> None:
    created = await api.post("/campaigns", json={"code": "tiktok", "name": "TikTok"})
    assert created.status_code == 201
    body = created.json()
    assert body["code"] == "TIKTOK" and body["deep_link"].endswith("start=c_TIKTOK") and body["registrations"] == 0
    assert (await api.post("/campaigns", json={"code": "tiktok"})).status_code == 409
    assert (await api.post("/campaigns", json={"code": "!"})).status_code == 422
    listed = (await api.get("/campaigns")).json()
    assert [c["code"] for c in listed] == ["TIKTOK"]
    paused = (await api.put("/campaigns/tiktok/active", json={"is_active": False})).json()
    assert paused["is_active"] is False
    assert (await api.put("/campaigns/none/active", json={"is_active": False})).status_code == 404
