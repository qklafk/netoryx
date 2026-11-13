"""Application settings, loaded from environment variables / `.env`.

Every tunable value lives here; `.env.example` documents each key.
Money is configured in whole rubles and converted to kopecks (`*_kop`) for all internal arithmetic.
Plans, panels, locations, endpoints and promo codes live in the database and are managed from the bot /
web admin; `DEFAULT_PLANS`, `XUI_*` and `REMNAWAVE_*` only seed an empty database on first start.
"""

from __future__ import annotations

import json
import re
from datetime import time
from decimal import Decimal, InvalidOperation
from functools import cached_property, lru_cache
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.utils.xray_outbound import LinkError, outbound_from_link, validate_domains


def _split_ints(value: object) -> object:
    if isinstance(value, str):
        return [int(x) for x in value.replace(";", ",").split(",") if x.strip()]
    if isinstance(value, int):
        return [value]
    return value


def _valid_rate(value: str | None) -> bool:
    """A positive, finite decimal exchange rate ("95.5")."""
    try:
        rate = Decimal((value or "").strip().replace(",", "."))
    except InvalidOperation:
        return False
    return rate.is_finite() and rate > 0


def _valid_currency(value: str | None) -> bool:
    return re.fullmatch(r"[A-Za-z]{3}", (value or "").strip()) is not None


class PlanSeed(BaseModel):
    title: str
    days: int
    price_rub: int
    devices: int = 3
    traffic_gb: int = 0


DEFAULT_PLANS = [
    PlanSeed(title="1 месяц", days=30, price_rub=150),
    PlanSeed(title="3 месяца", days=90, price_rub=400),
    PlanSeed(title="6 месяцев", days=180, price_rub=750),
    PlanSeed(title="12 месяцев", days=365, price_rub=1400),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Product mode ----------------------------------------------------------------------------
    # shop     - sell subscriptions (plans, payments, referral program, promo, gifts ...)
    # personal - private VPN for yourself, family and friends: access by invitation only, no payments
    app_mode: Literal["shop", "personal"] = "shop"
    # Local development helpers (fake "test" payment provider, demo data, tunnel URL discovery). Never in production.
    dev_mode: bool = False
    dev_tunnel_metrics_url: str | None = None  # cloudflared metrics endpoint to discover the quick-tunnel URL

    # --- Process layout ----------------------------------------------------------------------------
    # all    - bot + web server + background jobs in one process (default)
    # bot    - Telegram updates only (polling; in webhook mode the web server is started as well)
    # web    - web server only (subscription pages, payment webhooks, admin, Mini App, Telegram webhook)
    # worker - background jobs only (one worker per deployment; jobs are guarded by locks)
    role: Literal["all", "bot", "web", "worker"] = "all"
    auto_migrate: bool = True  # apply database migrations on start
    shutdown_timeout_sec: int = 20
    # Feature modules switched off by the owner (e.g. "gifts,promo"). Core modules cannot be disabled.
    disabled_modules: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Telegram ------------------------------------------------------------------------------
    bot_token: SecretStr | None = None
    # Optional outbound proxy for the bot itself (e.g. when the server is in Russia): socks5://user:pass@host:port
    bot_proxy_url: str | None = None
    # Optional self-hosted Telegram Bot API server, e.g. http://telegram-bot-api:8081
    telegram_api_url: str | None = None
    admin_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # Chats that receive reports and alerts. Defaults to ADMIN_IDS.
    admin_report_chat_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    # Optional admin group for notifications (overrides ADMIN_REPORT_CHAT_IDS). In a forum supergroup each category
    # can go to its own topic: ADMIN_TOPIC_<CATEGORY> = message thread id.
    admin_chat_id: int | None = None
    admin_topic_payments: int | None = None
    admin_topic_users: int | None = None
    admin_topic_tickets: int | None = None
    admin_topic_withdrawals: int | None = None
    admin_topic_backups: int | None = None
    admin_topic_alerts: int | None = None
    # Notification categories nobody should receive, e.g. "users,tickets"
    # (payments, users, tickets, withdrawals, backups, alerts).
    admin_notify_mute: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # "polling" (default, no public URL needed) or "webhook" (requires PUBLIC_BASE_URL with HTTPS).
    bot_mode: Literal["polling", "webhook"] = "polling"
    telegram_webhook_secret: SecretStr | None = None  # X-Telegram-Bot-Api-Secret-Token; generated if empty

    # --- Public HTTP server -----------------------------------------------------------------------
    # Public HTTPS base URL of this bot's web server (reverse proxy -> WEB_HOST:WEB_PORT). Needed for
    # subscription links, payment webhooks, web admin, Mini App and Telegram webhook mode.
    public_base_url: str | None = None
    web_host: str = "0.0.0.0"
    web_port: int = 8080
    # Trust X-Forwarded-For from these proxy IPs/CIDRs (for webhook IP allowlists).
    trusted_proxies: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["127.0.0.1", "::1", "172.16.0.0/12", "10.0.0.0/8", "192.168.0.0/16"]
    )

    # --- Storage ---------------------------------------------------------------------------------
    # sqlite+aiosqlite:///data/bot.db | postgresql+asyncpg://user:pass@host/db | mysql+aiomysql://user:pass@host/db
    database_url: str = "sqlite+aiosqlite:///data/bot.db"
    core_state_db: str = "data/core.db"  # independent plugin lifecycle state, separate from business data
    redis_url: str | None = None  # optional: FSM storage, throttling, locks. e.g. redis://redis:6379/0
    # Key for encrypting panel credentials and signing web sessions. Generated and stored in the DB if empty.
    secret_key: SecretStr | None = None

    # --- Branding / links ------------------------------------------------------------------------
    brand_name: str = "VPN"
    brand_logo_url: str = ""  # optional https:// logo shown in the Mini App header
    # In private chats the bot keeps one screen: typed answers and the previous screen are removed once the next
    # screen is shown, so the chat doesn't fill up with old menus.
    bot_single_screen: bool = True
    channel_url: str | None = None
    support_url: str | None = None
    # Built-in main menu buttons to hide, e.g. "referral,gift" (see app.services.menu_buttons.BUILTIN_KEYS).
    menu_hidden_buttons: Annotated[list[str], NoDecode] = Field(default_factory=list)
    terms_url: str | None = None  # public offer / terms of service (/terms)
    privacy_url: str | None = None
    # Ask users to accept TERMS_URL / PRIVACY_URL before using the bot. Change the version to ask everyone again.
    legal_consent_required: bool = False
    legal_consent_version: str = "1"

    # Customer IP addresses in logs and the database: "off" keeps none, "masked" keeps the network only
    # (203.0.113.x), "full" keeps them as is. Rate limits and provider IP checks work in memory either way.
    client_ip_logging: Literal["off", "masked", "full"] = "off"
    # REALITY links ask clients for a Firefox fingerprint without the post-quantum key share, so the TLS ClientHello
    # fits into one TCP segment (networks that drop the second segment otherwise break the connection).
    reality_compact_hello: bool = True

    # --- Time / i18n -----------------------------------------------------------------------------
    tz: str = "Europe/Moscow"
    default_language: Literal["ru", "en", "uk", "kk", "uz", "fa", "zh"] = "ru"
    # Languages users can pick (the default one is always available): ru, en, uk, kk, uz, fa, zh.
    enabled_languages: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["ru", "en", "uk", "kk", "uz", "fa", "zh"]
    )

    # --- Subscription ------------------------------------------------------------------------------
    trial_days: int = 3  # 0 disables the trial
    trial_device_limit: int = 1
    trial_traffic_gb: int = 0
    expiry_reminder_days: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [3, 1])
    # No reminders between these local hours (TZ); equal values turn quiet hours off.
    reminder_quiet_start_hour: int = 22
    reminder_quiet_end_hour: int = 9
    auto_renew_hours_before: int = 24
    expired_client_retention_days: int = 30  # remove expired accounts from panels after N days
    default_plans: Annotated[list[PlanSeed], NoDecode] = Field(default_factory=lambda: list(DEFAULT_PLANS))
    device_reset_cooldown_min: int = 30  # how often a user may reset their devices (0 = no limit)
    # Device limit enforced by the bot from the client's x-hwid header (no customer IP addresses are involved).
    hwid_required: bool = False  # apps that send no HWID: False = serve them, True = show a notice instead
    hwid_device_ttl_days: int = 30  # forget devices not seen for this long (0 = never)
    # 3x-ui: "hwid" = the bot counts devices and the panel gets no limit; "ip" = limitIp (needs Xray's access log).
    xui_device_limit: Literal["hwid", "ip"] = "hwid"
    # Paid extras. A zero price disables the corresponding offer.
    traffic_pack_gb: int = 0
    traffic_pack_price_kop: int = 0
    traffic_reset_price_kop: int = 0
    device_addon_count: int = 1
    device_addon_price_kop: int = 0
    # Temporary personal offers for users who did not convert after registration or whose paid access ended.
    winback_enabled: bool = False
    winback_discount_percent: int = 20
    winback_offer_days: int = 7
    winback_trial_wait_days: int = 3
    winback_churn_wait_days: int = 3
    # "once": the offer is used up by the first discounted purchase; "period": it applies until it expires.
    winback_usage: Literal["once", "period"] = "once"
    # Outgoing webhooks: how long delivery history is kept and after how many deliveries in a row that were given up
    # an endpoint is switched off (0 = never).
    webhooks_retention_days: int = 14
    webhooks_auto_disable_after: int = 3

    # --- Subscription URL rendering (Happ / INCY / v2RayTun / Hiddify ...) ------------------------
    sub_profile_title: str | None = None  # defaults to BRAND_NAME (max 25 chars)
    sub_update_interval_hours: int = 12
    sub_announce: str | None = None  # max 200 chars
    sub_whitelist_separate_url: bool = False  # LTE entries only at /sub/<token>/wl
    sub_happ_network_tags: bool = False  # append "only Mobile"/"only WiFi" (Happ premium filter)
    sub_routing_ru_direct: bool = False  # routing profile: Russian sites/IPs go DIRECT
    # JSON subscription (array of Xray configs) for apps that support it (Happ, v2RayTun); others get the link list.
    sub_json_enabled: bool = True
    sub_json_auto: bool = True  # an "Auto" config (least-ping balancer) when there are 2+ servers
    sub_json_direct_ru: bool = True  # Russian domains connect directly
    sub_json_direct_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)  # extra direct domains
    sub_json_proxy_domains: Annotated[list[str], NoDecode] = Field(default_factory=list)  # always through the VPN
    sub_happ_crypto_links: bool = True  # offer happ://crypt5 links (crypto.happ.su API)
    sub_incy_crypto_links: bool = True  # offer incy://crypt1 links (local AES-GCM)
    sub_show_server_profiles: bool = True
    sub_show_server_subscriptions: bool = True

    # --- Wallet & top-ups -------------------------------------------------------------------------
    min_topup_rub: int = 50
    max_topup_rub: int = 100_000
    topup_quick_amounts_rub: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [150, 300, 500, 1000])
    welcome_bonus_rub: int = 0

    # --- Referral program -------------------------------------------------------------------------
    referral_enabled: bool = True
    referral_invitee_bonus_rub: int = 50
    referral_inviter_bonus_rub: int = 50
    referral_inviter_requires_payment: bool = True  # reward inviter after invitee's first payment
    # The invitee's money bonus also waits for their first payment: otherwise new accounts could collect it and move it
    # to one account (gifts). Bonus days in "days" mode are always granted at once (like a trial).
    referral_invitee_requires_payment: bool = True
    referral_inviter_percent: int = 0  # legacy single-level % (prefer REFERRAL_LEVELS_PERCENT)
    # How recurring referral rewards are paid:
    #   "days"    - inviter gets subscription days: % of the days the referral bought (no money involved)
    #   "balance" - % of the referral's payments credited to the main wallet; spendable, NOT withdrawable
    #   "partner" - % credited to a separate partner balance; transferable to the wallet or withdrawable
    referral_reward_mode: Literal["days", "balance", "partner"] = "balance"
    # % per level up the referral chain, e.g. "20,10,5" (20% from direct referrals, 10% from level 2, 5% from
    # level 3); "20" = classic single-level. Applies to money (balance/partner) or days (days mode). Empty = off.
    # Money is a share of every credited real payment (never donations or wallet-funded purchases, so bonus/promo/
    # admin/commission money earns nothing); days are a share of paid plan purchases (never trials, promo, gifts).
    referral_levels_percent: Annotated[list[int], NoDecode] = Field(default_factory=lambda: [10])
    # One-time bonus days instead of rubles in "days" mode.
    referral_invitee_bonus_days: int = 3
    referral_inviter_bonus_days: int = 7
    withdrawals_enabled: bool = False  # only meaningful in "partner" mode
    min_withdrawal_rub: int = 500
    withdrawal_methods: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["card", "sbp", "usdt_trc20"])

    # --- Gifts ------------------------------------------------------------------------------------
    gifts_enabled: bool = True

    # --- Payments: common -------------------------------------------------------------------------
    payment_reconcile_interval_sec: int = 30
    payment_max_pending_hours: int = 24
    payment_description: str = "Пополнение баланса"
    # Users may keep a card (YooKassa autopayments / CloudPayments tokens) to pay auto-renewals they cannot cover
    # from the balance. Needs the provider's recurring payments to be enabled for the shop.
    saved_cards_enabled: bool = False

    # --- Payments: Telegram Stars ----------------------------------------------------------------
    stars_enabled: bool = False
    stars_rub_per_star: float = 1.5  # wallet rubles credited per Star
    donations_enabled: bool = True  # /donate (Stars)

    # --- Payments: CryptoBot (Crypto Pay API) ----------------------------------------------------
    cryptobot_enabled: bool = False
    cryptobot_token: SecretStr | None = None
    cryptobot_testnet: bool = False
    cryptobot_accepted_assets: str = "USDT,TON,BTC,ETH,LTC,TRX,USDC"

    # --- Payments: Platega (SBP / cards) ----------------------------------------------------------
    platega_enabled: bool = False
    platega_merchant_id: str | None = None
    platega_secret: SecretStr | None = None
    platega_payment_method: int | None = None  # None = the user picks on the payment page (v2 API)

    # --- Payments: YooKassa ------------------------------------------------------------------------
    yookassa_enabled: bool = False
    yookassa_shop_id: str | None = None
    yookassa_secret_key: SecretStr | None = None
    yookassa_return_url: str | None = None  # defaults to https://t.me/<bot_username>
    yookassa_receipt_enabled: bool = False  # 54-FZ receipts
    yookassa_receipt_email: str | None = None  # fixed receipt email; otherwise asked from the user
    yookassa_vat_code: int = 1
    yookassa_check_ip: bool = True

    # --- Payments: YooMoney quickpay (personal wallet) -------------------------------------------
    yoomoney_enabled: bool = False
    yoomoney_wallet: str | None = None
    yoomoney_notification_secret: SecretStr | None = None
    yoomoney_payment_type: Literal["AC", "PC"] = "AC"  # AC = bank card, PC = YooMoney wallet

    # --- Payments: FreeKassa ----------------------------------------------------------------------
    freekassa_enabled: bool = False
    freekassa_shop_id: str | None = None
    freekassa_secret1: SecretStr | None = None
    freekassa_secret2: SecretStr | None = None
    freekassa_api_key: SecretStr | None = None
    freekassa_check_ip: bool = True

    # --- Robokassa hosted payment form ------------------------------------------------------------
    robokassa_enabled: bool = False
    robokassa_merchant_login: str | None = None
    robokassa_password1: SecretStr | None = None
    robokassa_password2: SecretStr | None = None
    robokassa_hash_algorithm: Literal["md5", "sha256"] = "md5"
    robokassa_test_mode: bool = False
    robokassa_check_ip: bool = True

    # --- Heleket cryptocurrency invoices -----------------------------------------------------------
    heleket_enabled: bool = False
    heleket_merchant_id: str | None = None
    heleket_api_key: SecretStr | None = None
    heleket_currency: str = "RUB"
    heleket_to_currency: str | None = None

    # --- NOWPayments cryptocurrency invoices ------------------------------------------------------
    nowpayments_enabled: bool = False
    nowpayments_api_key: SecretStr | None = None
    nowpayments_ipn_secret: SecretStr | None = None
    nowpayments_pay_currency: str = "usdttrc20"

    # --- Plisio cryptocurrency invoices -----------------------------------------------------------
    plisio_enabled: bool = False
    plisio_api_key: SecretStr | None = None
    plisio_currency: str | None = None  # optional concrete crypto, otherwise Plisio's payment chooser

    # --- Direct blockchain transfers (no intermediary) ----------------------------------------------
    # Toncoin straight to the owner's wallet; payments are matched by a unique comment via toncenter.
    ton_enabled: bool = False
    ton_wallet_address: str | None = None  # user-friendly (UQ.../EQ...) or raw 0:hex address
    ton_api_key: SecretStr | None = None  # toncenter.com API key (optional, raises the request limit)
    ton_api_url: str = "https://toncenter.com/api/v3"
    ton_rate_rub: float | None = None  # fixed RUB per 1 TON; fallback when the automatic rate is unavailable
    # USDT TRC-20 straight to the owner's TRON address; payments are matched by a unique amount via TronGrid.
    usdt_trc20_enabled: bool = False
    usdt_trc20_address: str | None = None  # base58 address starting with T
    usdt_trc20_api_key: SecretStr | None = None  # TronGrid TRON-PRO-API-KEY (optional, recommended)
    usdt_trc20_api_url: str = "https://api.trongrid.io"
    usdt_trc20_rate_rub: float | None = None  # fixed RUB per 1 USDT; fallback for the automatic rate
    # Automatic exchange rates from CoinGecko (public /simple/price), cached; the fixed rates are the fallback.
    crypto_rate_auto: bool = False
    crypto_rate_cache_sec: int = 300

    # --- WATA acquiring (cards, SBP) ----------------------------------------------------------------
    wata_enabled: bool = False
    wata_token: SecretStr | None = None  # terminal access token from the merchant account
    wata_sandbox: bool = False
    wata_public_key: str | None = None  # optional pinned webhook key (PEM); otherwise downloaded from WATA

    # --- RollyPay (SBP, cards; paid out in USDT) ------------------------------------------------------
    rollypay_enabled: bool = False
    rollypay_api_key: SecretStr | None = None  # issued with the cashier
    rollypay_signing_secret: SecretStr | None = None  # verifies callbacks
    rollypay_payment_method: str | None = None  # sbp | card; empty = the payer chooses on the payment page

    # --- cisPay (cards, SBP) -----------------------------------------------------------------------
    cispay_enabled: bool = False
    cispay_shop_id: str | None = None  # shop UUID from the cabinet
    cispay_api_key: SecretStr | None = None  # cis_sec_...; also signs webhooks
    cispay_payment_method: str | None = None  # card | sbp; empty = the payer chooses on the payment page

    # --- TabPay (cards, SBP) -----------------------------------------------------------------------
    tabpay_enabled: bool = False
    tabpay_api_key: SecretStr | None = None  # tp_...
    tabpay_webhook_secret: SecretStr | None = None  # the shop's signing secret (not the API key)
    tabpay_payment_method: str | None = None  # card | sbp; empty = the payer chooses on the payment page

    # --- SeverPay (cards, SBP) ---------------------------------------------------------------------
    severpay_enabled: bool = False
    severpay_mid: int | None = None  # merchant id
    severpay_token: SecretStr | None = None  # secret token: signs requests and verifies webhooks
    severpay_client_email: str = "{tg_id}@telegram.org"  # the API requires a customer e-mail; {tg_id} is replaced

    # --- Cashera (SBP, cards; settled in USD) ----------------------------------------------------
    cashera_enabled: bool = False
    cashera_api_key: SecretStr | None = None  # pk_...
    cashera_secret: SecretStr | None = None  # sk_...; compared with the X-Secret webhook header
    cashera_payment_method: str | None = None  # sbp | card; empty = the payer chooses on the payment page

    # --- ParityPay (SBP, cards) --------------------------------------------------------------------
    paritypay_enabled: bool = False
    paritypay_shop_id: str | None = None
    paritypay_secret_key: SecretStr | None = None  # key no. 1: API access
    paritypay_secret_key2: SecretStr | None = None  # key no. 2: webhook signature
    paritypay_service: str | None = None  # sbp | card; empty = the payer chooses on the payment page

    # --- Aurapay (cards, SBP) ----------------------------------------------------------------------
    aurapay_enabled: bool = False
    aurapay_shop_id: str | None = None
    aurapay_api_key: SecretStr | None = None
    aurapay_secret_key2: SecretStr | None = None  # key no. 2: webhook signature
    aurapay_service: str | None = None  # sbp | card; empty = the payer chooses on the payment page

    # --- Antilopay acquiring (cards, SBP, SberPay) --------------------------------------------------
    antilopay_enabled: bool = False
    antilopay_project_id: str | None = None  # project_identificator
    antilopay_secret_id: SecretStr | None = None
    antilopay_secret_key: SecretStr | None = None  # RSA private key (PEM or Base64), signs API requests
    antilopay_callback_key: str | None = None  # project's RSA public key (PEM or Base64), verifies callbacks
    antilopay_product_type: Literal["goods", "services"] = "services"
    antilopay_vat: int | None = None  # 10 or 22; required by the provider for merchants on OSNO
    antilopay_prefer_methods: str | None = None  # comma-separated, e.g. "SBP,CARD_RU"
    antilopay_customer_email: str | None = None  # buyer e-mail sent with payments; per-user default otherwise
    antilopay_check_ip: bool = True

    # --- Stripe Checkout (charged in a foreign currency at a fixed rate) ----------------------------
    stripe_enabled: bool = False
    stripe_secret_key: SecretStr | None = None
    stripe_webhook_secret: SecretStr | None = None  # whsec_... of the webhook endpoint; without it only polling
    stripe_currency: str = "USD"
    stripe_rub_per_unit: str | None = None  # rubles per 1 unit of STRIPE_CURRENCY, e.g. "95.5"

    # --- PayPal Orders v2 (charged in a foreign currency at a fixed rate) ---------------------------
    paypal_enabled: bool = False
    paypal_client_id: str | None = None
    paypal_client_secret: SecretStr | None = None
    paypal_webhook_id: str | None = None  # id of the webhook registered in the PayPal app; without it only polling
    paypal_sandbox: bool = False
    paypal_currency: str = "USD"
    paypal_rub_per_unit: str | None = None  # rubles per 1 unit of PAYPAL_CURRENCY, e.g. "95.5"

    # --- Tribute shop orders (tribute.tg) ---------------------------------------------------------
    tribute_enabled: bool = False
    tribute_api_key: SecretStr | None = None  # also signs webhooks; set the webhook URL in Tribute's API settings
    tribute_shop_id: int | None = None  # optional; Tribute uses the oldest shop when unset

    # --- lava.top invoices ------------------------------------------------------------------------
    lavatop_enabled: bool = False
    lavatop_api_key: SecretStr | None = None
    lavatop_offer_id: str | None = None  # offer of a product with a dynamic price
    lavatop_webhook_secret: SecretStr | None = None  # X-Api-Key value (or "login:password" for Basic auth)
    lavatop_payment_method: str = "card"  # card | sbp
    lavatop_email_domain: str | None = None  # buyer email for users without one: tg<id>@<domain>

    # --- Custom HTTP gateway (any provider with a JSON API and HMAC-signed webhooks) ----------------
    # Templates take placeholders {payment_id} {amount} {amount_kop} {amount_number} {currency} {description}
    # {user_id} {return_url} {callback_url} {api_key} (not in URLs); the status request also {provider_id}.
    custom_enabled: bool = False
    custom_title: str = "💳 Оплата"
    custom_currency: str = "RUB"
    custom_api_key: SecretStr | None = None  # referenced as {api_key} in headers / bodies
    custom_create_method: Literal["POST", "GET", "PUT"] = "POST"
    custom_create_url: str | None = None
    custom_headers: str | None = None  # JSON object, e.g. {"Authorization": "Bearer {api_key}"}
    custom_body_format: Literal["json", "form"] = "json"
    custom_create_body: str | None = None  # JSON template or key=value&key2={placeholder}
    custom_response_url_path: str | None = None  # dotted path, e.g. data.payment_url
    custom_response_id_path: str | None = None
    custom_status_method: Literal["GET", "POST"] = "GET"
    custom_status_url: str | None = None  # empty = no polling: signed webhooks are trusted
    custom_status_body: str | None = None
    custom_status_path: str | None = None
    custom_status_values: str | None = None  # JSON object: {"paid": "succeeded", "failed": "canceled"}
    custom_status_amount_path: str | None = None  # paid amount in rubles (optional)
    custom_webhook_secret: SecretStr | None = None  # HMAC key of the raw webhook body
    custom_webhook_signature_header: str | None = None
    custom_webhook_hash: Literal["sha256", "sha512"] = "sha256"
    custom_webhook_signature_encoding: Literal["hex", "base64"] = "hex"
    custom_webhook_allowed_ips: Annotated[list[str], NoDecode] = Field(default_factory=list)
    custom_webhook_payment_id_path: str | None = None
    custom_webhook_provider_id_path: str | None = None
    custom_webhook_status_path: str | None = None
    custom_webhook_amount_path: str | None = None

    # --- CloudPayments (hosted payment page, saved cards) -----------------------------------------------
    # Set the Check / Pay / Fail notification URL to <PUBLIC_BASE_URL>/webhooks/cloudpayments in the dashboard.
    cloudpayments_enabled: bool = False
    cloudpayments_public_id: str | None = None
    cloudpayments_api_secret: SecretStr | None = None  # also signs notifications (Content-HMAC)

    # --- Manual card / SBP transfer ---------------------------------------------------------------
    manual_transfer_enabled: bool = False
    manual_transfer_details: str | None = None  # card / phone / bank instructions displayed to the buyer

    # --- "Мой налог" receipts (self-employed / NPD) -----------------------------------------------
    # Every credited payment through one of NPD_PROVIDERS is registered as income in lknpd.nalog.ru.
    npd_enabled: bool = False
    npd_inn: str | None = None  # the taxpayer's INN (login of the "Мой налог" web cabinet)
    npd_password: SecretStr | None = None  # cabinet password; not needed after an SMS login from the web admin
    npd_refresh_token: SecretStr | None = None  # optional: a refresh token obtained elsewhere
    npd_service_name: str = "Подписка VPN {plan}"  # receipt line for a plan purchase; {plan} = plan title
    npd_topup_service_name: str = "Пополнение баланса VPN-сервиса"  # receipt line for other payments
    npd_providers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "platega",
            "yookassa",
            "yoomoney",
            "freekassa",
            "robokassa",
            "cloudpayments",
            "wata",
            "rollypay",
            "cispay",
            "tabpay",
            "severpay",
            "cashera",
            "paritypay",
            "aurapay",
            "antilopay",
            "lavatop",
            "manual",
        ]
    )
    npd_send_to_user: bool = True  # send the receipt link to the buyer in the bot
    npd_max_attempts: int = 12  # failed registrations before a receipt needs an admin's retry

    # --- Panels: bootstrap on first start -----------------------------------------------------------
    # 3x-ui
    xui_host: str | None = None  # full panel URL incl. web base path, e.g. https://1.2.3.4:2053/secret
    xui_username: str | None = None
    xui_password: SecretStr | None = None
    xui_api_token: SecretStr | None = None  # 3x-ui v3.1+ Bearer token (optional, preferred)
    xui_tls_verify: bool = True
    xui_inbound_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)  # empty = all VLESS
    xui_public_host: str | None = None  # address clients connect to; defaults to the panel host
    xui_location_name: str = "Main"
    xui_location_flag: str = "🌍"
    # Remnawave
    remnawave_url: str | None = None  # http://remnawave:3000 (direct) or https://panel.example.com
    remnawave_api_token: SecretStr | None = None
    remnawave_squad_uuids: Annotated[list[str], NoDecode] = Field(default_factory=list)  # empty = all
    remnawave_direct: bool = True  # add X-Forwarded-* headers when calling the container directly
    remnawave_webhook_secret: SecretStr | None = None  # panel's WEBHOOK_SECRET_HEADER (optional)

    # --- Panels: sync & health ---------------------------------------------------------------------
    panel_request_timeout_sec: int = 15
    reconcile_interval_min: int = 5
    health_check_interval_min: int = 2
    health_failures_before_alert: int = 3
    tls_watch_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)  # extra HTTPS names to watch
    tls_watch_connect: str | None = None  # host:port to connect to instead of the watched name (SNI is kept)
    tls_warn_days: int = 14  # alert when a certificate expires in fewer days
    delete_orphan_clients: bool = False
    adblock_enabled: bool = False  # block ad / tracker domains in the Xray routing of every 3x-ui panel
    youtube_exit_enabled: bool = False  # send YouTube through a separate exit server (3x-ui panels)
    youtube_exit_link: SecretStr | None = None  # vless:// / socks5:// / ss:// share link of the exit server
    youtube_exit_domains: Annotated[list[str], NoDecode] = Field(default_factory=lambda: ["geosite:youtube"])

    # Shared Telegram-only link and its /tg page (3x-ui servers; see docs/telegram-only.md).
    tg_only_enabled: bool = True
    # Servers in these countries (ISO 3166-1 alpha-2, taken from the location flag) are not offered: Telegram is
    # restricted or blocked there, so a Telegram-only exit would not work.
    tg_only_excluded_countries: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["RU", "BY", "CN", "IR", "KP", "TM", "VN"]
    )
    tg_only_allow_unknown_country: bool = False  # offer servers whose location has no recognisable flag
    tg_only_monthly_limit_gb: int = 0  # total traffic of the shared client per month, reset monthly (0 = unlimited)
    tg_only_daily_alert_gb: int = 50  # alert admins when the shared client moves more than this in a day (0 = off)

    # --- Server monitoring --------------------------------------------------------------------------
    monitor_alerts_enabled: bool = True  # alert admins on high load, stopped Xray, offline nodes
    monitor_interval_min: int = 5
    monitor_cpu_alert_percent: int = 90
    monitor_ram_alert_percent: int = 90
    monitor_disk_alert_percent: int = 90

    # --- Cost sharing (personal mode) ---------------------------------------------------------------
    cost_sharing_enabled: bool = False  # monthly reminder with each member's share of the server bill
    cost_sharing_total_rub: int = 0  # monthly total to split
    cost_sharing_day: int = 1  # day of month for the reminder (1..28)
    cost_sharing_details: str | None = None  # how to pay (card number, phone ...)
    cost_sharing_include_owner: bool = True  # the owner pays a share too

    # --- Telegram MTProto proxy for users -------------------------------------------------------
    # everyone    - every user gets a personal proxy (non-subscribers optionally with limits/ad tag below)
    # subscribers - only users with an active subscription whose plan includes it (Plan.includes_tg_proxy)
    # off         - feature hidden (the default: MTProto proxies are blocked in some countries)
    tg_proxy_access: Literal["everyone", "subscribers", "off"] = "off"
    tg_proxy_free_rate_limit_mbps: int = 0  # speed cap for users without a subscription (0 = none)
    tg_proxy_free_ad_tag: str | None = None  # sponsored channel tag (@MTProxybot) for users without a subscription
    tg_proxy_max_unique_ips: int = 3  # per-user simultaneous IPs before the link is considered shared (0 = no cap)
    tg_proxy_auto_rotate_on_abuse: bool = True
    tg_proxy_max_servers: int = 2  # proxy servers per user (primary + backups)
    tg_proxy_rotate_cooldown_min: int = 60  # how often a user may get a new proxy link (0 = no limit)
    # Link sharing detection: an account sitting at the unique-IP cap for this many scans in a row counts as shared.
    tg_proxy_abuse_strikes: int = 3
    tg_proxy_abuse_ip_threshold: int = 10  # unique IPs that count as sharing when TG_PROXY_MAX_UNIQUE_IPS is 0
    tg_proxy_abuse_rotate_cooldown_hours: int = 24  # at most one automatic link change per account in this period
    tg_proxy_scan_interval_min: int = 5  # usage / drift / sharing scan of the proxy servers
    # Personal SOCKS5 proxy (3x-ui mixed inbound with password auth) offered next to the MTProto proxy; the same
    # access policy, server count and link cooldown apply. Without a selected inbound nothing is shown.
    tg_socks_enabled: bool = False

    # --- Required channel subscription --------------------------------------------------------------
    required_channel_id: int | None = None  # e.g. -1001234567890; the bot must be an admin there
    required_channel_url: str | None = None

    # --- Backups ------------------------------------------------------------------------------------
    backup_enabled: bool = True
    backup_time: time = time(4, 0)
    backup_chat_id: int | None = None  # defaults to the first admin
    backup_dir: str = "data/backups"
    backup_keep: int = 7  # local backup files to keep
    backup_password: SecretStr | None = None  # encrypt backup files (AES-GCM); keep the password safe

    # --- Web admin & Mini App -----------------------------------------------------------------------
    web_admin_enabled: bool = True
    miniapp_enabled: bool = True
    web_session_hours: int = 24

    # --- Web cabinet (customers log in from a browser; the Mini App interface served at /cabinet) ---
    cabinet_enabled: bool = False
    cabinet_login_bot: bool = True  # one-time link issued by the bot
    cabinet_login_telegram: bool = False  # Telegram Login Widget (needs /setdomain in BotFather)
    cabinet_login_email: bool = False  # e-mail magic link (needs SMTP)
    cabinet_session_days: int = 30
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None  # sender address, e.g. "VPN <no-reply@example.com>"

    # --- Broadcasts ----------------------------------------------------------------------------------
    broadcast_rate_per_sec: int = 25  # Telegram allows about 30 messages per second

    # --- Anti-spam ----------------------------------------------------------------------------------
    throttle_rate_limit: int = 5
    throttle_period_sec: int = 10

    # --- External blacklist of Telegram user IDs -------------------------------------------------------
    blacklist_enabled: bool = False
    blacklist_urls: Annotated[list[str], NoDecode] = Field(default_factory=list)  # plain text, one ID per line
    blacklist_refresh_minutes: int = 360
    blacklist_action: Literal["flag", "block"] = "flag"  # flag = notify admins only; block = ban the user
    blacklist_ban_paying: bool = False  # also ban users with an active paid subscription
    blacklist_manual_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)

    # --- Maintenance -----------------------------------------------------------------------------------
    maintenance_notify_on_finish: bool = True  # tell users who hit maintenance that the bot works again
    maintenance_announce_minutes: int = 60  # default lead time for announcing a scheduled maintenance window

    # --- Logging / monitoring -------------------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False  # one JSON object per line (for log collectors)
    sentry_dsn: str | None = None
    update_check_repo: str | None = None  # "owner/name" on GitHub: show when a newer release is out

    # -------------------------------------------------------------------------------------------------
    @field_validator(
        "admin_ids",
        "admin_report_chat_ids",
        "topup_quick_amounts_rub",
        "expiry_reminder_days",
        "xui_inbound_ids",
        "referral_levels_percent",
        mode="before",
    )
    @classmethod
    def _split(cls, v: object) -> object:
        return _split_ints(v)

    @field_validator(
        "remnawave_squad_uuids",
        "trusted_proxies",
        "withdrawal_methods",
        "disabled_modules",
        "menu_hidden_buttons",
        "admin_notify_mute",
        "custom_webhook_allowed_ips",
        "enabled_languages",
        "npd_providers",
        "youtube_exit_domains",
        "tls_watch_hosts",
        mode="before",
    )
    @classmethod
    def _split_str(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.strip() for x in v.split(",") if x.strip()]
        return v

    @field_validator("sub_json_direct_domains", "sub_json_proxy_domains", mode="before")
    @classmethod
    def _split_domains(cls, v: object) -> object:
        if isinstance(v, str):
            return [x for x in re.split(r"[\s,;]+", v) if x]
        return v

    @field_validator("tg_only_excluded_countries", mode="before")
    @classmethod
    def _split_countries(cls, v: object) -> object:
        if isinstance(v, str):
            return [x.upper() for x in re.split(r"[\s,;]+", v) if x]
        if isinstance(v, list):
            return [str(x).strip().upper() for x in v if str(x).strip()]
        return v

    @field_validator("blacklist_urls", mode="before")
    @classmethod
    def _split_urls(cls, v: object) -> object:
        if isinstance(v, str):
            return [x for x in re.split(r"[\s,;]+", v) if x]
        return v

    @field_validator("blacklist_manual_ids", mode="before")
    @classmethod
    def _split_ids(cls, v: object) -> object:
        if isinstance(v, str):
            return [int(x) for x in re.split(r"[\s,;]+", v) if x]
        if isinstance(v, int):
            return [v]
        return v

    @field_validator("backup_time", mode="before")
    @classmethod
    def _parse_time(cls, v: object) -> object:
        if isinstance(v, str) and v.count(":") == 1:
            return v + ":00"
        return v

    @field_validator("default_plans", mode="before")
    @classmethod
    def _parse_plans(cls, v: object) -> object:
        if isinstance(v, str):
            return json.loads(v) if v.strip() else []
        return v

    @field_validator("tz")
    @classmethod
    def _check_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:  # KeyError subclass: pydantic would not catch it
            raise ValueError(f"unknown time zone {v!r}") from exc
        return v

    @field_validator("brand_logo_url")
    @classmethod
    def _check_logo_url(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not (
            v.lower().startswith("https://") and len(v) > len("https://") and not any(c.isspace() for c in v)
        ):
            raise ValueError("BRAND_LOGO_URL must be an https:// URL")
        return v

    @field_validator("channel_url", "support_url", "terms_url", "privacy_url", "required_channel_url")
    @classmethod
    def _check_public_link(cls, v: str | None) -> str | None:
        """These links end up in hrefs and buttons: only web and Telegram schemes (never ``javascript:``).

        A link written without a scheme keeps working: ``t.me/x`` becomes ``https://t.me/x`` and ``@x`` a t.me link.
        """
        if v is None or not v.strip():
            return None if v is None else v
        value = v.strip()
        if value.startswith("@") and len(value) > 1:
            value = f"https://t.me/{value[1:]}"
        elif not value.startswith("//") and ":" not in value.split("/", 1)[0]:
            value = f"https://{value}"
        scheme, sep, rest = value.partition(":")
        if scheme.lower() not in ("http", "https", "tg") or not sep or not rest.removeprefix("//"):
            raise ValueError("use an http(s):// or tg:// link")
        return value

    @field_validator("public_base_url", "xui_host", "remnawave_url")
    @classmethod
    def _strip_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    @model_validator(mode="after")
    def _check(self) -> Settings:
        def need(cond: bool, msg: str) -> None:
            if not cond:
                raise ValueError(msg)

        if self.youtube_exit_link is not None and self.youtube_exit_link.get_secret_value().strip():
            try:
                outbound_from_link(self.youtube_exit_link.get_secret_value(), "youtube-exit")
            except LinkError as exc:
                raise ValueError(f"YOUTUBE_EXIT_LINK: {exc}") from None
        validate_domains(self.youtube_exit_domains)
        need(
            all(re.fullmatch(r"[A-Z]{2}", c) for c in self.tg_only_excluded_countries),
            "TG_ONLY_EXCLUDED_COUNTRIES: use two-letter country codes such as RU,BY",
        )
        need(self.tg_only_monthly_limit_gb >= 0, "TG_ONLY_MONTHLY_LIMIT_GB must not be negative")
        need(self.tg_only_daily_alert_gb >= 0, "TG_ONLY_DAILY_ALERT_GB must not be negative")
        need(0 < self.min_topup_rub <= self.max_topup_rub, "invalid MIN_TOPUP_RUB / MAX_TOPUP_RUB")
        need(self.stars_rub_per_star > 0, "STARS_RUB_PER_STAR must be positive")
        need(self.shutdown_timeout_sec > 0, "SHUTDOWN_TIMEOUT_SEC must be positive")
        need(self.maintenance_announce_minutes >= 0, "MAINTENANCE_ANNOUNCE_MINUTES must not be negative")
        need(self.device_reset_cooldown_min >= 0, "DEVICE_RESET_COOLDOWN_MIN must not be negative")
        need(self.hwid_device_ttl_days >= 0, "HWID_DEVICE_TTL_DAYS must not be negative")
        need(self.traffic_pack_gb >= 0, "TRAFFIC_PACK_GB must not be negative")
        need(self.traffic_pack_price_kop >= 0, "TRAFFIC_PACK_PRICE_KOP must not be negative")
        need(self.traffic_reset_price_kop >= 0, "TRAFFIC_RESET_PRICE_KOP must not be negative")
        need(self.device_addon_count >= 1, "DEVICE_ADDON_COUNT must be at least 1")
        need(self.device_addon_price_kop >= 0, "DEVICE_ADDON_PRICE_KOP must not be negative")
        need(self.backup_keep >= 1, "BACKUP_KEEP must be at least 1")
        need(bool(self.backup_dir.strip()), "BACKUP_DIR must not be empty")
        need(1 <= self.broadcast_rate_per_sec <= 30, "BROADCAST_RATE_PER_SEC must be 1..30")
        need(self.tg_proxy_max_unique_ips >= 0, "TG_PROXY_MAX_UNIQUE_IPS must not be negative")
        need(1 <= self.tg_proxy_max_servers <= 10, "TG_PROXY_MAX_SERVERS must be 1..10")
        need(self.tg_proxy_rotate_cooldown_min >= 0, "TG_PROXY_ROTATE_COOLDOWN_MIN must not be negative")
        need(self.tg_proxy_abuse_strikes >= 1, "TG_PROXY_ABUSE_STRIKES must be at least 1")
        need(self.tg_proxy_abuse_ip_threshold >= 1, "TG_PROXY_ABUSE_IP_THRESHOLD must be at least 1")
        need(
            self.tg_proxy_abuse_rotate_cooldown_hours >= 0, "TG_PROXY_ABUSE_ROTATE_COOLDOWN_HOURS must not be negative"
        )
        need(1 <= self.tg_proxy_scan_interval_min <= 1440, "TG_PROXY_SCAN_INTERVAL_MIN must be 1..1440")
        need(
            0 <= self.reminder_quiet_start_hour <= 23 and 0 <= self.reminder_quiet_end_hour <= 23,
            "REMINDER_QUIET_START_HOUR / REMINDER_QUIET_END_HOUR must be 0..23",
        )
        need(1 <= self.monitor_interval_min <= 1440, "MONITOR_INTERVAL_MIN must be 1..1440")
        need(5 <= self.blacklist_refresh_minutes <= 10080, "BLACKLIST_REFRESH_MINUTES must be 5..10080")
        need(len(self.blacklist_urls) <= 20, "BLACKLIST_URLS: at most 20 sources")
        need(
            all(u.startswith(("https://", "http://")) for u in self.blacklist_urls),
            "BLACKLIST_URLS must be http(s) URLs",
        )
        for name in ("monitor_cpu_alert_percent", "monitor_ram_alert_percent", "monitor_disk_alert_percent"):
            need(1 <= getattr(self, name) <= 100, f"{name.upper()} must be 1..100")
        need(self.cost_sharing_total_rub >= 0, "COST_SHARING_TOTAL_RUB must not be negative")
        need(1 <= self.cost_sharing_day <= 28, "COST_SHARING_DAY must be 1..28")
        if self.update_check_repo:
            need(
                re.fullmatch(r"[\w.-]+/[\w.-]+", self.update_check_repo) is not None,
                "UPDATE_CHECK_REPO must look like owner/name",
            )
        need(bool(self.legal_consent_version.strip()), "LEGAL_CONSENT_VERSION must not be empty")
        need(
            all(0 <= p <= 100 for p in self.referral_levels_percent) and sum(self.referral_levels_percent) < 100,
            "REFERRAL_LEVELS_PERCENT: each 0..100 and total < 100",
        )
        if self.bot_mode == "webhook":
            need(
                (self.public_base_url or "").startswith("https://"),
                "BOT_MODE=webhook requires PUBLIC_BASE_URL starting with https://",
            )
        if self.yookassa_enabled:
            need(
                bool(self.yookassa_shop_id and self.yookassa_secret_key),
                "YooKassa needs SHOP_ID and SECRET_KEY",
            )
        if self.cryptobot_enabled:
            need(bool(self.cryptobot_token), "CRYPTOBOT_ENABLED requires CRYPTOBOT_TOKEN")
        if self.platega_enabled:
            need(bool(self.platega_merchant_id and self.platega_secret), "Platega needs MERCHANT_ID and SECRET")
        if self.yoomoney_enabled:
            need(
                bool(self.yoomoney_wallet and self.yoomoney_notification_secret),
                "YooMoney needs YOOMONEY_WALLET and YOOMONEY_NOTIFICATION_SECRET",
            )
        if self.freekassa_enabled:
            need(
                bool(self.freekassa_shop_id and self.freekassa_secret1 and self.freekassa_secret2),
                "FreeKassa needs FREEKASSA_SHOP_ID, FREEKASSA_SECRET1, FREEKASSA_SECRET2",
            )
        if self.robokassa_enabled:
            need(
                bool(self.robokassa_merchant_login and self.robokassa_password1 and self.robokassa_password2),
                "ROBOKASSA_ENABLED requires MERCHANT_LOGIN, PASSWORD1, PASSWORD2",
            )
        if self.heleket_enabled:
            need(
                bool(self.heleket_merchant_id and self.heleket_api_key),
                "HELEKET_ENABLED requires MERCHANT_ID and API_KEY",
            )
        if self.nowpayments_enabled:
            need(
                bool(self.nowpayments_api_key and self.nowpayments_ipn_secret),
                "NOWPAYMENTS_ENABLED requires API_KEY and IPN_SECRET",
            )
        if self.plisio_enabled:
            need(bool(self.plisio_api_key), "PLISIO_ENABLED requires API_KEY")
        if self.cloudpayments_enabled:
            need(
                bool(self.cloudpayments_public_id and self.cloudpayments_api_secret),
                "CLOUDPAYMENTS_ENABLED requires PUBLIC_ID and API_SECRET",
            )
        need(10 <= self.crypto_rate_cache_sec <= 86400, "CRYPTO_RATE_CACHE_SEC must be 10..86400")
        if self.ton_enabled:
            need(
                bool(
                    re.fullmatch(
                        r"-?\d{1,3}:[0-9a-fA-F]{64}|[A-Za-z0-9_+/-]{48}", (self.ton_wallet_address or "").strip()
                    )
                ),
                "TON_ENABLED requires a valid TON_WALLET_ADDRESS",
            )
            need(
                self.crypto_rate_auto or (self.ton_rate_rub or 0) > 0,
                "TON_ENABLED requires TON_RATE_RUB or CRYPTO_RATE_AUTO",
            )
        if self.usdt_trc20_enabled:
            need(
                bool(re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", (self.usdt_trc20_address or "").strip())),
                "USDT_TRC20_ENABLED requires a valid USDT_TRC20_ADDRESS",
            )
            need(
                self.crypto_rate_auto or (self.usdt_trc20_rate_rub or 0) > 0,
                "USDT_TRC20_ENABLED requires USDT_TRC20_RATE_RUB or CRYPTO_RATE_AUTO",
            )
        if self.wata_enabled:
            need(bool(self.wata_token), "WATA_ENABLED requires WATA_TOKEN")
        if self.cispay_enabled:
            need(
                bool(self.cispay_shop_id and self.cispay_api_key),
                "CISPAY_ENABLED requires CISPAY_SHOP_ID and CISPAY_API_KEY",
            )
            need(
                not self.cispay_payment_method or self.cispay_payment_method.lower() in ("card", "sbp"),
                "CISPAY_PAYMENT_METHOD must be card or sbp",
            )
        if self.tabpay_enabled:
            need(
                bool(self.tabpay_api_key and self.tabpay_webhook_secret),
                "TABPAY_ENABLED requires TABPAY_API_KEY and TABPAY_WEBHOOK_SECRET",
            )
            need(
                not self.tabpay_payment_method or self.tabpay_payment_method.lower() in ("card", "sbp"),
                "TABPAY_PAYMENT_METHOD must be card or sbp",
            )
        if self.severpay_enabled:
            need(
                bool(self.severpay_mid and self.severpay_token),
                "SEVERPAY_ENABLED requires SEVERPAY_MID and SEVERPAY_TOKEN",
            )
            need(
                "{" not in self.severpay_client_email.replace("{tg_id}", ""),
                "SEVERPAY_CLIENT_EMAIL may only use {tg_id}",
            )
        if self.cashera_enabled:
            need(
                bool(self.cashera_api_key and self.cashera_secret),
                "CASHERA_ENABLED requires CASHERA_API_KEY and CASHERA_SECRET",
            )
            need(
                not self.cashera_payment_method or self.cashera_payment_method.lower() in ("sbp", "card"),
                "CASHERA_PAYMENT_METHOD must be sbp or card",
            )
        if self.paritypay_enabled:
            need(
                bool(self.paritypay_shop_id and self.paritypay_secret_key and self.paritypay_secret_key2),
                "PARITYPAY_ENABLED requires PARITYPAY_SHOP_ID, PARITYPAY_SECRET_KEY and PARITYPAY_SECRET_KEY2",
            )
            need(
                not self.paritypay_service or self.paritypay_service.lower() in ("sbp", "card"),
                "PARITYPAY_SERVICE must be sbp or card",
            )
        if self.aurapay_enabled:
            need(
                bool(self.aurapay_shop_id and self.aurapay_api_key and self.aurapay_secret_key2),
                "AURAPAY_ENABLED requires AURAPAY_SHOP_ID, AURAPAY_API_KEY and AURAPAY_SECRET_KEY2",
            )
            need(
                not self.aurapay_service or self.aurapay_service.lower() in ("sbp", "card"),
                "AURAPAY_SERVICE must be sbp or card",
            )
        if self.rollypay_enabled:
            need(
                bool(self.rollypay_api_key and self.rollypay_signing_secret),
                "ROLLYPAY_ENABLED requires ROLLYPAY_API_KEY and ROLLYPAY_SIGNING_SECRET",
            )
            need(
                not self.rollypay_payment_method or self.rollypay_payment_method in ("sbp", "card"),
                "ROLLYPAY_PAYMENT_METHOD must be sbp or card",
            )
        if self.antilopay_enabled:
            need(
                bool(
                    self.antilopay_project_id
                    and self.antilopay_secret_id
                    and self.antilopay_secret_key
                    and self.antilopay_callback_key
                ),
                "ANTILOPAY_ENABLED requires PROJECT_ID, SECRET_ID, SECRET_KEY and CALLBACK_KEY",
            )
        if self.stripe_enabled:
            need(bool(self.stripe_secret_key), "STRIPE_ENABLED requires STRIPE_SECRET_KEY")
            need(_valid_rate(self.stripe_rub_per_unit), "STRIPE_RUB_PER_UNIT must be a positive number")
            need(_valid_currency(self.stripe_currency), "STRIPE_CURRENCY must be a 3-letter ISO code")
        if self.paypal_enabled:
            need(
                bool(self.paypal_client_id and self.paypal_client_secret),
                "PAYPAL_ENABLED requires PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET",
            )
            need(_valid_rate(self.paypal_rub_per_unit), "PAYPAL_RUB_PER_UNIT must be a positive number")
            need(_valid_currency(self.paypal_currency), "PAYPAL_CURRENCY must be a 3-letter ISO code")
        if self.tribute_enabled:
            need(bool(self.tribute_api_key), "TRIBUTE_ENABLED requires API_KEY")
        if self.lavatop_enabled:
            need(
                bool(self.lavatop_api_key and self.lavatop_offer_id and self.lavatop_webhook_secret),
                "LAVATOP_ENABLED requires API_KEY, OFFER_ID and WEBHOOK_SECRET",
            )
            need(self.lavatop_payment_method.lower() in {"card", "sbp"}, "LAVATOP_PAYMENT_METHOD must be card or sbp")
        if self.custom_enabled:
            from app.services.payments.custom_http import CustomHttpConfig, config_errors

            problems = config_errors(CustomHttpConfig.from_settings(self))
            need(not problems, "CUSTOM_ENABLED: " + "; ".join(problems))
        if self.manual_transfer_enabled:
            need(bool((self.manual_transfer_details or "").strip()), "MANUAL_TRANSFER_ENABLED requires DETAILS")
        if (
            self.yoomoney_enabled
            or self.freekassa_enabled
            or self.robokassa_enabled
            or self.heleket_enabled
            or self.nowpayments_enabled
            or self.plisio_enabled
            or self.cloudpayments_enabled
            or self.wata_enabled
            or self.rollypay_enabled
            or self.cispay_enabled
            or self.tabpay_enabled
            or self.severpay_enabled
            or self.cashera_enabled
            or self.paritypay_enabled
            or self.aurapay_enabled
            or self.antilopay_enabled
            or self.stripe_enabled
            or self.paypal_enabled
            or self.tribute_enabled
            or self.lavatop_enabled
            or self.custom_enabled
        ):
            need(bool(self.public_base_url), "enabled callback payment providers need PUBLIC_BASE_URL")
        if self.xui_host:
            need(
                bool((self.xui_username and self.xui_password) or self.xui_api_token),
                "XUI_HOST requires XUI_USERNAME + XUI_PASSWORD or XUI_API_TOKEN",
            )
        if self.remnawave_url:
            need(bool(self.remnawave_api_token), "REMNAWAVE_URL requires REMNAWAVE_API_TOKEN")
        return self

    # --- Derived values ------------------------------------------------------------------------------
    @cached_property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)

    @property
    def report_chat_ids(self) -> list[int]:
        return self.admin_report_chat_ids or self.admin_ids

    @property
    def backup_target_chat(self) -> int | None:
        return self.backup_chat_id or (self.admin_ids[0] if self.admin_ids else None)

    @property
    def profile_title(self) -> str:
        return (self.sub_profile_title or self.brand_name)[:25]

    @property
    def min_topup_kop(self) -> int:
        return self.min_topup_rub * 100

    @property
    def max_topup_kop(self) -> int:
        return self.max_topup_rub * 100

    @property
    def referral_invitee_bonus_kop(self) -> int:
        return self.referral_invitee_bonus_rub * 100

    @property
    def referral_inviter_bonus_kop(self) -> int:
        return self.referral_inviter_bonus_rub * 100

    @property
    def min_withdrawal_kop(self) -> int:
        return self.min_withdrawal_rub * 100

    @property
    def welcome_bonus_kop(self) -> int:
        return self.welcome_bonus_rub * 100

    @property
    def web_enabled(self) -> bool:
        """Subscription URLs, webhooks, web admin and Mini App need a public HTTPS endpoint."""
        return bool(self.public_base_url)

    @property
    def any_payment_enabled(self) -> bool:
        return any(
            (
                self.stars_enabled,
                self.cryptobot_enabled,
                self.platega_enabled,
                self.yookassa_enabled,
                self.yoomoney_enabled,
                self.freekassa_enabled,
            )
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
