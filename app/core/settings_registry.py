"""Declarations of runtime-editable settings (shown and validated by the bot and web admin).

Every :class:`~app.config.Settings` field is either declared here (editable at runtime, stored in the database)
or listed in :data:`ENV_ONLY` (bootstrap values read from the environment and shown read-only).
"""

from __future__ import annotations

import types
import typing
from dataclasses import dataclass
from datetime import time
from typing import Any, Literal, get_args, get_origin

from pydantic import SecretStr

from app.config import Settings
from app.core.permissions import Permission

SettingType = Literal["str", "int", "float", "bool", "time", "list[int]", "list[str]", "json", "choice", "secret"]

# Values that only make sense at process start (credentials, addresses, process layout) or only seed an empty
# database. They are never stored in the database.
ENV_ONLY: frozenset[str] = frozenset(
    {
        "bot_token",
        "database_url",
        "core_state_db",
        "redis_url",
        "secret_key",
        "public_base_url",
        "web_host",
        "web_port",
        "bot_mode",
        "telegram_webhook_secret",
        "admin_ids",
        "log_level",
        "log_json",
        "sentry_dsn",
        "role",
        "auto_migrate",
        "shutdown_timeout_sec",
        "dev_mode",
        "dev_tunnel_metrics_url",
        "default_plans",
        "backup_dir",
        "xui_host",
        "xui_username",
        "xui_password",
        "xui_api_token",
        "xui_tls_verify",
        "xui_inbound_ids",
        "xui_public_host",
        "xui_location_name",
        "xui_location_flag",
        "remnawave_url",
        "remnawave_api_token",
        "remnawave_squad_uuids",
        "remnawave_direct",
    }
)

GROUPS: dict[str, tuple[str, str]] = {
    "mode": ("Режим работы", "Product mode"),
    "branding": ("Оформление и ссылки", "Branding & links"),
    "subscription": ("Подписка", "Subscription"),
    "trial": ("Пробный период", "Trial"),
    "reminders": ("Напоминания", "Reminders"),
    "rendering": ("Ссылка подписки", "Subscription link"),
    "wallet": ("Баланс", "Wallet"),
    "referral": ("Реферальная программа", "Referral program"),
    "gifts": ("Подарки", "Gifts"),
    "payments": ("Платежи: общие", "Payments: common"),
    "payments_stars": ("Telegram Stars", "Telegram Stars"),
    "payments_cryptobot": ("CryptoBot", "CryptoBot"),
    "payments_platega": ("Platega", "Platega"),
    "payments_yookassa": ("ЮKassa", "YooKassa"),
    "payments_yoomoney": ("ЮMoney", "YooMoney"),
    "payments_freekassa": ("FreeKassa", "FreeKassa"),
    "payments_robokassa": ("Робокасса", "Robokassa"),
    "payments_heleket": ("Heleket", "Heleket"),
    "payments_nowpayments": ("NOWPayments", "NOWPayments"),
    "payments_plisio": ("Plisio", "Plisio"),
    "payments_cloudpayments": ("CloudPayments", "CloudPayments"),
    "payments_ton": ("Toncoin напрямую", "Toncoin (direct)"),
    "payments_usdt_trc20": ("USDT TRC-20 напрямую", "USDT TRC-20 (direct)"),
    "payments_crypto_rates": ("Курсы криптовалют", "Crypto exchange rates"),
    "payments_wata": ("WATA", "WATA"),
    "payments_rollypay": ("RollyPay", "RollyPay"),
    "payments_cispay": ("cisPay", "cisPay"),
    "payments_tabpay": ("TabPay", "TabPay"),
    "payments_severpay": ("SeverPay", "SeverPay"),
    "payments_cashera": ("Cashera", "Cashera"),
    "payments_paritypay": ("ParityPay", "ParityPay"),
    "payments_aurapay": ("Aurapay", "Aurapay"),
    "payments_antilopay": ("Antilopay", "Antilopay"),
    "payments_stripe": ("Stripe", "Stripe"),
    "payments_paypal": ("PayPal", "PayPal"),
    "payments_tribute": ("Tribute", "Tribute"),
    "payments_lavatop": ("lava.top", "lava.top"),
    "payments_custom": ("Свой платёжный шлюз", "Custom gateway"),
    "payments_manual": ("Ручной перевод", "Manual transfer"),
    "panels_sync": ("Серверы: синхронизация", "Servers: sync & health"),
    "monitoring": ("Мониторинг серверов", "Server monitoring"),
    "tg_only": ("Доступ к Telegram", "Telegram access"),
    "tg_proxy": ("Прокси для Telegram (устаревшее)", "Telegram proxy (legacy)"),
    "channel_gate": ("Обязательный канал", "Required channel"),
    "backups": ("Резервные копии", "Backups"),
    "cost_sharing": ("Складчина за сервер", "Cost sharing"),
    "webapp": ("Веб-админка и Mini App", "Web admin & Mini App"),
    "antispam": ("Антиспам", "Anti-spam"),
    "blacklist": ("Чёрный список", "Blacklist"),
    "i18n": ("Язык и время", "Language & time"),
    "maintenance": ("Техработы", "Maintenance"),
    "broadcasts": ("Рассылки", "Broadcasts"),
    "admin_notify": ("Уведомления администраторам", "Admin notifications"),
    "system": ("Система", "System"),
}
GROUPS["winback"] = ("Personal offers", "Personal offers")
GROUPS["privacy"] = ("Приватность", "Privacy")
GROUPS["cabinet"] = ("Веб-кабинет", "Web cabinet")
GROUPS["webhooks"] = ("Исходящие вебхуки", "Outgoing webhooks")
GROUPS["npd"] = ("Чеки «Мой налог»", "NPD receipts (Мой налог)")


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    group: str
    type: SettingType
    label_ru: str
    label_en: str
    help_ru: str = ""
    help_en: str = ""
    choices: tuple[str, ...] | None = None
    min: float | None = None
    max: float | None = None
    restart_required: bool = False
    owner_only: bool = False
    nullable: bool = False

    @property
    def secret(self) -> bool:
        return self.type == "secret"

    @property
    def permission(self) -> Permission:
        """Permission needed to view the value and change it."""
        return Permission.SETTINGS_SECRETS if self.secret or self.owner_only else Permission.SETTINGS

    def label(self, lang: str) -> str:
        return self.label_en if lang == "en" else self.label_ru

    def help(self, lang: str) -> str:
        return self.help_en if lang == "en" else self.help_ru


def _unwrap_optional(annotation: Any) -> tuple[Any, bool]:
    origin = get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _strip_annotated(annotation: Any) -> Any:
    while get_origin(annotation) is typing.Annotated:
        annotation = get_args(annotation)[0]
    return annotation


def infer_type(key: str) -> tuple[SettingType, tuple[str, ...] | None, bool]:
    """(type, choices, nullable) of a Settings field derived from its annotation."""
    annotation = _strip_annotated(Settings.model_fields[key].annotation)
    annotation, nullable = _unwrap_optional(annotation)
    annotation = _strip_annotated(annotation)
    origin = get_origin(annotation)
    if origin is Literal:
        return "choice", tuple(str(a) for a in get_args(annotation)), nullable
    if origin is list:
        (item,) = get_args(annotation)
        if item is int:
            return "list[int]", None, nullable
        if item is str:
            return "list[str]", None, nullable
        return "json", None, nullable
    simple: dict[Any, SettingType] = {
        bool: "bool",
        int: "int",
        float: "float",
        str: "str",
        time: "time",
        SecretStr: "secret",
    }
    return simple.get(annotation, "json"), None, nullable


def _spec(
    key: str,
    group: str,
    label_ru: str,
    label_en: str,
    help_ru: str = "",
    help_en: str = "",
    *,
    min: float | None = None,
    max: float | None = None,
    restart_required: bool = False,
    owner_only: bool = False,
) -> SettingSpec:
    if key not in Settings.model_fields:
        raise KeyError(f"unknown setting {key!r}")
    if group not in GROUPS:
        raise KeyError(f"unknown settings group {group!r}")
    type_, choices, nullable = infer_type(key)
    return SettingSpec(
        key=key,
        group=group,
        type=type_,
        label_ru=label_ru,
        label_en=label_en,
        help_ru=help_ru,
        help_en=help_en,
        choices=choices,
        min=min,
        max=max,
        restart_required=restart_required,
        owner_only=owner_only or type_ == "secret",
        nullable=nullable,
    )


_SPECS: list[SettingSpec] = [
    # --- mode
    _spec(
        "app_mode",
        "mode",
        "Режим",
        "Mode",
        "shop — продажа подписок; personal — личный VPN по приглашениям, без платежей.",
        "shop - sell subscriptions; personal - private VPN by invitation, no payments.",
        owner_only=True,
    ),
    # --- branding
    _spec("brand_name", "branding", "Название сервиса", "Service name"),
    _spec(
        "brand_logo_url",
        "branding",
        "Логотип (https-ссылка)",
        "Logo (https URL)",
        "Квадратная картинка, показывается в шапке Mini App рядом с названием.",
        "Square image shown in the Mini App header next to the service name.",
    ),
    _spec(
        "bot_single_screen",
        "branding",
        "Бот в одном сообщении",
        "Single-screen bot",
        "В личном чате бот держит один экран: после ввода текста старый экран и сообщение пользователя удаляются.",
        "In private chats the bot keeps one screen: after a typed answer the old screen and the user's text "
        "are removed.",
    ),
    _spec("channel_url", "branding", "Ссылка на канал", "Channel link"),
    _spec("support_url", "branding", "Ссылка на поддержку", "Support link"),
    _spec(
        "menu_hidden_buttons",
        "branding",
        "Скрытые кнопки главного меню",
        "Hidden main menu buttons",
        "Через запятую: trial, subscription, connect, buy, balance, referral, gift, tg_proxy, support, instructions, "
        "language, mini_app.",
        "Comma-separated: trial, subscription, connect, buy, balance, referral, gift, tg_proxy, support, instructions, "
        "language, mini_app.",
    ),
    _spec("terms_url", "branding", "Пользовательское соглашение (оферта)", "Terms of service"),
    _spec("privacy_url", "branding", "Политика конфиденциальности", "Privacy policy"),
    _spec(
        "legal_consent_required",
        "branding",
        "Требовать согласие с правилами",
        "Require accepting the terms",
        "Новые пользователи должны принять соглашение и политику перед началом работы.",
        "New users must accept the terms and privacy policy before using the bot.",
    ),
    _spec(
        "legal_consent_version",
        "branding",
        "Версия правил",
        "Terms version",
        "Измените, чтобы снова запросить согласие у всех.",
        "Change it to ask everyone to accept again.",
    ),
    # --- subscription
    _spec(
        "auto_renew_hours_before",
        "subscription",
        "Автопродление: за сколько часов",
        "Auto-renew: hours before expiry",
        min=1,
        max=168,
    ),
    _spec(
        "expired_client_retention_days",
        "subscription",
        "Удалять с серверов через, дней после окончания",
        "Remove from servers after, days past expiry",
        min=0,
        max=3650,
    ),
    _spec(
        "device_reset_cooldown_min",
        "subscription",
        "Сброс устройств не чаще, мин",
        "Device reset cooldown, min",
        "0 — без ограничений.",
        "0 - no limit.",
        min=0,
        max=10080,
    ),
    _spec(
        "hwid_required",
        "subscription",
        "Требовать HWID от приложения",
        "Require HWID from the app",
        "Выкл. — приложения без HWID получают доступ без учёта устройств. Вкл. — им показывается "
        "уведомление «приложение не поддерживает лимит устройств».",
        "Off - apps that send no HWID are served without device counting. On - they get a notice that the "
        "app does not support device limits.",
    ),
    _spec(
        "hwid_device_ttl_days",
        "subscription",
        "Забывать устройство через, дней",
        "Forget a device after, days",
        "Устройство, не заходившее столько дней, освобождает место в лимите. 0 — не забывать.",
        "A device not seen for this many days frees its slot. 0 = never.",
        min=0,
        max=3650,
    ),
    _spec(
        "xui_device_limit",
        "subscription",
        "3x-ui: учёт устройств",
        "3x-ui: device limit mode",
        "hwid — лимит считает бот по заголовку x-hwid, IP-адреса клиентов не нужны (рекомендуется). "
        "ip — лимит limitIp на панели: требует включённый access-лог Xray, который записывает IP клиентов "
        "и посещённые сайты.",
        "hwid - the bot counts devices by the x-hwid header, no client IP addresses are needed (recommended). "
        "ip - the panel's limitIp: needs Xray's access log enabled, which records client IPs and visited sites.",
    ),
    _spec(
        "traffic_pack_gb",
        "subscription",
        "Трафик в пакете, ГБ (0 — выключен)",
        "Traffic pack, GB (0 = off)",
        min=0,
        max=1_000_000,
    ),
    _spec(
        "traffic_pack_price_kop",
        "subscription",
        "Цена пакета трафика, коп.",
        "Traffic-pack price, kop.",
        min=0,
    ),
    _spec(
        "traffic_reset_price_kop",
        "subscription",
        "Цена сброса трафика, коп. (0 — выключен)",
        "Traffic-reset price, kop. (0 = off)",
        min=0,
    ),
    _spec(
        "device_addon_count",
        "subscription",
        "Устройств в доп. пакете",
        "Devices in add-on",
        min=1,
        max=1000,
    ),
    _spec(
        "device_addon_price_kop",
        "subscription",
        "Цена доп. устройств, коп. (0 — выключен)",
        "Device add-on price, kop. (0 = off)",
        min=0,
    ),
    _spec(
        "reality_compact_hello",
        "rendering",
        "Компактное TLS-приветствие для Reality",
        "Compact TLS hello for Reality",
        "Ссылки Reality просят клиента использовать отпечаток Firefox без постквантового ключа: приветствие "
        "помещается в один TCP-пакет. Некоторые сети выбрасывают второй пакет большого приветствия, и VPN не "
        "подключается. Переопределения отдельного подключения важнее этой настройки.",
        "Reality links ask clients for a Firefox fingerprint without the post-quantum key share, so the hello fits "
        "into one TCP segment. Some networks drop the second segment of a large hello and the VPN never connects. "
        "Per-endpoint overrides take precedence.",
    ),
    # --- privacy
    _spec(
        "client_ip_logging",
        "privacy",
        "Запись IP клиентов",
        "Customer IP logging",
        "off — IP пользователей не записываются ни в журнал, ни в базу; masked — только сеть (203.0.113.x); "
        "full — полностью. Защита от перебора и проверка IP платёжных систем работают в любом режиме.",
        "off — customer IPs are kept neither in logs nor in the database; masked — the network only "
        "(203.0.113.x); full — as is. Rate limits and payment-provider IP checks work in every mode.",
        owner_only=True,
    ),
    # --- win-back offers
    _spec("winback_enabled", "winback", "Автоматические win-back предложения", "Automatic win-back offers"),
    _spec("winback_discount_percent", "winback", "Скидка win-back, %", "Win-back discount, %", min=1, max=99),
    _spec("winback_offer_days", "winback", "Срок предложения, дней", "Offer validity, days", min=1, max=90),
    _spec(
        "winback_usage",
        "winback",
        "Сколько покупок со скидкой",
        "Discounted purchases",
        "once — скидка на одну покупку, period — на все покупки до конца срока предложения.",
        "once — the discount covers one purchase, period — every purchase until the offer expires.",
    ),
    _spec(
        "winback_trial_wait_days",
        "winback",
        "Ожидание после регистрации, дней",
        "Wait after registration, days",
        min=0,
        max=365,
    ),
    _spec(
        "winback_churn_wait_days",
        "winback",
        "Ожидание после окончания, дней",
        "Wait after expiry, days",
        min=0,
        max=365,
    ),
    # --- outgoing webhooks
    _spec(
        "webhooks_retention_days",
        "webhooks",
        "Хранить историю доставок, дней",
        "Keep delivery history, days",
        min=1,
        max=365,
    ),
    _spec(
        "webhooks_auto_disable_after",
        "webhooks",
        "Отключать адрес после N неудачных доставок подряд (0 — никогда)",
        "Disable an endpoint after N failed deliveries in a row (0 = never)",
        min=0,
        max=1000,
    ),
    # --- trial
    _spec("trial_days", "trial", "Длительность, дней (0 — выключен)", "Length, days (0 = off)", min=0, max=365),
    _spec("trial_device_limit", "trial", "Лимит устройств", "Device limit", min=0, max=100),
    _spec("trial_traffic_gb", "trial", "Лимит трафика, ГБ (0 — без лимита)", "Traffic limit, GB (0 = none)", min=0),
    # --- reminders
    _spec(
        "expiry_reminder_days",
        "reminders",
        "Напоминать об окончании за N дней",
        "Remind N days before expiry",
        "Через запятую, например 3,1",
        "Comma-separated, e.g. 3,1",
    ),
    _spec(
        "reminder_quiet_start_hour",
        "reminders",
        "Тихие часы: начало",
        "Quiet hours: start",
        "Час по местному времени, с которого напоминания не отправляются. Одинаковые начало и конец — без тихих часов.",
        "Local hour from which reminders are not sent. Equal start and end - no quiet hours.",
        min=0,
        max=23,
    ),
    _spec(
        "reminder_quiet_end_hour",
        "reminders",
        "Тихие часы: конец",
        "Quiet hours: end",
        "Час по местному времени, с которого напоминания снова отправляются.",
        "Local hour from which reminders are sent again.",
        min=0,
        max=23,
    ),
    # --- rendering
    _spec("sub_show_server_profiles", "rendering", "Показывать отдельные профили серверов", "Show per-server profiles"),
    _spec(
        "sub_show_server_subscriptions", "rendering", "Показывать подписки по серверам", "Show per-server subscriptions"
    ),
    _spec("sub_profile_title", "rendering", "Название профиля в приложении", "Profile title in apps", max=25),
    _spec("sub_update_interval_hours", "rendering", "Интервал обновления, ч", "Update interval, hours", min=1, max=168),
    _spec("sub_announce", "rendering", "Объявление в приложении", "Announcement in apps", max=200),
    _spec(
        "sub_whitelist_separate_url",
        "rendering",
        "LTE-серверы отдельной ссылкой",
        "LTE servers in a separate link",
    ),
    _spec("sub_happ_network_tags", "rendering", "Метки «только Wi-Fi / моб.» (Happ)", "Wi-Fi / mobile tags (Happ)"),
    _spec(
        "sub_routing_ru_direct",
        "rendering",
        "Российские сайты напрямую",
        "Russian sites go direct",
    ),
    _spec(
        "sub_json_enabled",
        "rendering",
        "JSON-подписка (Happ, v2RayTun)",
        "JSON subscription (Happ, v2RayTun)",
        "Эти приложения получают готовые конфиги Xray с маршрутизацией; остальные - обычный список ссылок.",
        "These apps get ready-made Xray configs with routing; other apps get the plain link list.",
    ),
    _spec(
        "sub_json_auto",
        "rendering",
        "JSON: конфиг «Авто»",
        "JSON: Auto config",
        "При двух и более серверах добавляет конфиг с автоматическим выбором самого быстрого сервера.",
        "With two or more servers adds a config that picks the fastest server automatically.",
    ),
    _spec(
        "sub_json_direct_ru",
        "rendering",
        "JSON: российские сайты напрямую",
        "JSON: Russian sites go direct",
        "Домены .ru, .su, .рф и крупные российские сервисы идут мимо VPN.",
        "Domains under .ru, .su, .rf and major Russian services bypass the VPN.",
    ),
    _spec(
        "sub_json_direct_domains",
        "rendering",
        "JSON: дополнительные прямые домены",
        "JSON: extra direct domains",
        "Через запятую или с новой строки: example.com, domain:example.org, full:host.example.",
        "Comma or newline separated: example.com, domain:example.org, full:host.example.",
    ),
    _spec(
        "sub_json_proxy_domains",
        "rendering",
        "JSON: домены только через VPN",
        "JSON: domains always through the VPN",
        "Имеют приоритет над прямыми правилами, например российский сайт, недоступный из России.",
        "Win over the direct rules, e.g. a Russian site that is unreachable from Russia.",
    ),
    _spec("sub_happ_crypto_links", "rendering", "Зашифрованные ссылки Happ", "Encrypted Happ links"),
    _spec("sub_incy_crypto_links", "rendering", "Зашифрованные ссылки INCY", "Encrypted INCY links"),
    # --- wallet
    _spec("min_topup_rub", "wallet", "Минимальное пополнение, ₽", "Minimum top-up, RUB", min=1),
    _spec("max_topup_rub", "wallet", "Максимальное пополнение, ₽", "Maximum top-up, RUB", min=1),
    _spec("topup_quick_amounts_rub", "wallet", "Быстрые суммы пополнения, ₽", "Quick top-up amounts, RUB"),
    _spec("welcome_bonus_rub", "wallet", "Приветственный бонус, ₽", "Welcome bonus, RUB", min=0),
    # --- referral
    _spec("referral_enabled", "referral", "Реферальная программа включена", "Referral program enabled"),
    _spec(
        "referral_reward_mode",
        "referral",
        "Вид вознаграждения",
        "Reward type",
        "days — дни подписки; balance — % на баланс; partner — % на партнёрский баланс с выводом.",
        "days - subscription days; balance - % to the wallet; partner - % to a withdrawable partner balance.",
    ),
    _spec(
        "referral_levels_percent",
        "referral",
        "Проценты по уровням",
        "Percent per level",
        "Например 20,10,5 — 20% с прямых рефералов, 10% со второго уровня, 5% с третьего.",
        "E.g. 20,10,5 - 20% from direct referrals, 10% from level 2, 5% from level 3.",
    ),
    _spec("referral_invitee_bonus_rub", "referral", "Бонус приглашённому, ₽", "Invitee bonus, RUB", min=0),
    _spec("referral_inviter_bonus_rub", "referral", "Бонус пригласившему, ₽", "Inviter bonus, RUB", min=0),
    _spec(
        "referral_inviter_requires_payment",
        "referral",
        "Бонус пригласившему после первой оплаты",
        "Inviter bonus after the first payment",
    ),
    _spec(
        "referral_invitee_requires_payment",
        "referral",
        "Бонус приглашённому (₽) после его первой оплаты",
        "Invitee money bonus after their first payment",
    ),
    _spec(
        "referral_inviter_percent",
        "referral",
        "Процент пригласившему (устар.)",
        "Inviter percent (legacy)",
        min=0,
        max=99,
    ),
    _spec(
        "referral_invitee_bonus_days", "referral", "Бонус приглашённому, дней", "Invitee bonus, days", min=0, max=365
    ),
    _spec(
        "referral_inviter_bonus_days", "referral", "Бонус пригласившему, дней", "Inviter bonus, days", min=0, max=365
    ),
    _spec("withdrawals_enabled", "referral", "Вывод партнёрских средств", "Partner withdrawals"),
    _spec("min_withdrawal_rub", "referral", "Минимальная сумма вывода, ₽", "Minimum withdrawal, RUB", min=1),
    _spec("withdrawal_methods", "referral", "Способы вывода", "Withdrawal methods"),
    # --- gifts
    _spec("gifts_enabled", "gifts", "Подарочные подписки", "Gift subscriptions"),
    # --- payments: common
    _spec(
        "payment_reconcile_interval_sec",
        "payments",
        "Проверка платежей, сек",
        "Payment check interval, sec",
        min=10,
        max=3600,
    ),
    _spec(
        "payment_max_pending_hours",
        "payments",
        "Срок жизни счёта, ч",
        "Invoice lifetime, hours",
        min=1,
        max=720,
    ),
    _spec("payment_description", "payments", "Назначение платежа", "Payment description"),
    _spec(
        "saved_cards_enabled",
        "payments",
        "Сохранение карт для автопродления",
        "Saved cards for auto-renewal",
        "С согласия пользователя карта сохраняется у ЮKassa или CloudPayments и списывается, если на балансе не "
        "хватает денег на автопродление. Автоплатежи должны быть подключены у провайдера.",
        "With the user's consent the card is kept by YooKassa or CloudPayments and charged when the balance cannot "
        "cover an auto-renewal. Recurring payments must be enabled by the provider.",
    ),
    # --- payments: Stars
    _spec("stars_enabled", "payments_stars", "Включено", "Enabled"),
    _spec("stars_rub_per_star", "payments_stars", "Рублей за 1 звезду", "Rubles per Star", min=0.01),
    _spec("donations_enabled", "payments_stars", "Донаты звёздами", "Donations in Stars"),
    # --- payments: CryptoBot
    _spec("cryptobot_enabled", "payments_cryptobot", "Включено", "Enabled"),
    _spec("cryptobot_token", "payments_cryptobot", "API-токен", "API token"),
    _spec("cryptobot_testnet", "payments_cryptobot", "Тестовая сеть", "Testnet"),
    _spec("cryptobot_accepted_assets", "payments_cryptobot", "Принимаемые валюты", "Accepted assets"),
    # --- payments: Platega
    _spec("platega_enabled", "payments_platega", "Включено", "Enabled"),
    _spec("platega_merchant_id", "payments_platega", "Merchant ID", "Merchant ID", owner_only=True),
    _spec("platega_secret", "payments_platega", "Секретный ключ", "Secret"),
    _spec(
        "platega_payment_method",
        "payments_platega",
        "Способ оплаты",
        "Payment method",
        "Пусто — пользователь выбирает на странице оплаты.",
        "Empty - the user picks on the payment page.",
    ),
    # --- payments: YooKassa
    _spec("yookassa_enabled", "payments_yookassa", "Включено", "Enabled"),
    _spec("yookassa_shop_id", "payments_yookassa", "Shop ID", "Shop ID", owner_only=True),
    _spec("yookassa_secret_key", "payments_yookassa", "Секретный ключ", "Secret key"),
    _spec("yookassa_return_url", "payments_yookassa", "Адрес возврата", "Return URL"),
    _spec("yookassa_receipt_enabled", "payments_yookassa", "Чеки 54-ФЗ", "54-FZ receipts"),
    _spec("yookassa_receipt_email", "payments_yookassa", "E-mail для чеков", "Receipt e-mail"),
    _spec("yookassa_vat_code", "payments_yookassa", "Код НДС", "VAT code", min=1, max=12),
    _spec("yookassa_check_ip", "payments_yookassa", "Проверять IP уведомлений", "Check notification IPs"),
    # --- payments: YooMoney
    _spec("yoomoney_enabled", "payments_yoomoney", "Включено", "Enabled"),
    _spec("yoomoney_wallet", "payments_yoomoney", "Номер кошелька", "Wallet number", owner_only=True),
    _spec("yoomoney_notification_secret", "payments_yoomoney", "Секрет уведомлений", "Notification secret"),
    _spec("yoomoney_payment_type", "payments_yoomoney", "Способ оплаты", "Payment type"),
    # --- payments: FreeKassa
    _spec("freekassa_enabled", "payments_freekassa", "Включено", "Enabled"),
    _spec("freekassa_shop_id", "payments_freekassa", "ID магазина", "Shop ID", owner_only=True),
    _spec("freekassa_secret1", "payments_freekassa", "Секретное слово 1", "Secret word 1"),
    _spec("freekassa_secret2", "payments_freekassa", "Секретное слово 2", "Secret word 2"),
    _spec("freekassa_api_key", "payments_freekassa", "API-ключ", "API key"),
    _spec("freekassa_check_ip", "payments_freekassa", "Проверять IP уведомлений", "Check notification IPs"),
    # --- payments: Robokassa
    _spec("robokassa_enabled", "payments_robokassa", "Включено", "Enabled"),
    _spec("robokassa_merchant_login", "payments_robokassa", "Логин магазина", "Merchant login", owner_only=True),
    _spec("robokassa_password1", "payments_robokassa", "Пароль №1", "Password #1"),
    _spec("robokassa_password2", "payments_robokassa", "Пароль №2", "Password #2"),
    _spec("robokassa_hash_algorithm", "payments_robokassa", "Алгоритм хэша", "Hash algorithm"),
    _spec("robokassa_test_mode", "payments_robokassa", "Тестовый режим", "Test mode"),
    _spec("robokassa_check_ip", "payments_robokassa", "Проверять IP уведомлений", "Check notification IPs"),
    # --- payments: Heleket
    _spec("heleket_enabled", "payments_heleket", "Включено", "Enabled"),
    _spec("heleket_merchant_id", "payments_heleket", "Merchant ID", "Merchant ID", owner_only=True),
    _spec("heleket_api_key", "payments_heleket", "API-ключ", "API key"),
    _spec("heleket_currency", "payments_heleket", "Валюта счёта", "Invoice currency"),
    _spec("heleket_to_currency", "payments_heleket", "Валюта к оплате", "Pay currency"),
    # --- payments: NOWPayments
    _spec("nowpayments_enabled", "payments_nowpayments", "Включено", "Enabled"),
    _spec("nowpayments_api_key", "payments_nowpayments", "API-ключ", "API key"),
    _spec("nowpayments_ipn_secret", "payments_nowpayments", "IPN-секрет", "IPN secret"),
    _spec("nowpayments_pay_currency", "payments_nowpayments", "Валюта к оплате", "Pay currency"),
    _spec("plisio_enabled", "payments_plisio", "Включено", "Enabled"),
    _spec("plisio_api_key", "payments_plisio", "API-ключ", "API key"),
    _spec("plisio_currency", "payments_plisio", "Криптовалюта", "Cryptocurrency"),
    # --- payments: CloudPayments
    _spec("cloudpayments_enabled", "payments_cloudpayments", "Включено", "Enabled"),
    _spec("cloudpayments_public_id", "payments_cloudpayments", "Public ID", "Public ID", owner_only=True),
    _spec("cloudpayments_api_secret", "payments_cloudpayments", "Пароль для API", "API secret"),
    # --- payments: direct blockchain transfers
    _spec("ton_enabled", "payments_ton", "Включено", "Enabled"),
    _spec(
        "ton_wallet_address",
        "payments_ton",
        "Адрес кошелька",
        "Wallet address",
        "Сюда покупатели переводят TON; платёж находится по комментарию",
        "Buyers send TON here; payments are matched by their comment",
        owner_only=True,
    ),
    _spec("ton_api_key", "payments_ton", "API-ключ toncenter", "toncenter API key"),
    _spec("ton_api_url", "payments_ton", "Адрес API toncenter", "toncenter API URL", owner_only=True),
    _spec("ton_rate_rub", "payments_ton", "Курс, ₽ за 1 TON", "Rate, RUB per 1 TON", min=0),
    _spec("usdt_trc20_enabled", "payments_usdt_trc20", "Включено", "Enabled"),
    _spec(
        "usdt_trc20_address",
        "payments_usdt_trc20",
        "Адрес TRON",
        "TRON address",
        "Сюда покупатели переводят USDT; платёж находится по уникальной сумме",
        "Buyers send USDT here; payments are matched by a unique amount",
        owner_only=True,
    ),
    _spec("usdt_trc20_api_key", "payments_usdt_trc20", "API-ключ TronGrid", "TronGrid API key"),
    _spec("usdt_trc20_api_url", "payments_usdt_trc20", "Адрес API TronGrid", "TronGrid API URL", owner_only=True),
    _spec("usdt_trc20_rate_rub", "payments_usdt_trc20", "Курс, ₽ за 1 USDT", "Rate, RUB per 1 USDT", min=0),
    _spec(
        "crypto_rate_auto",
        "payments_crypto_rates",
        "Автоматический курс (CoinGecko)",
        "Automatic rate (CoinGecko)",
        "Если курс недоступен, используется фиксированный",
        "The fixed rate is used when the automatic one is unavailable",
    ),
    _spec("crypto_rate_cache_sec", "payments_crypto_rates", "Кэш курса, сек", "Rate cache, seconds", min=10, max=86400),
    _spec("wata_enabled", "payments_wata", "Включено", "Enabled"),
    _spec("wata_token", "payments_wata", "Токен доступа терминала", "Terminal access token"),
    _spec("wata_sandbox", "payments_wata", "Тестовая среда (sandbox)", "Sandbox"),
    _spec(
        "wata_public_key",
        "payments_wata",
        "Публичный ключ вебхуков (PEM)",
        "Webhook public key (PEM)",
        "Необязательно: по умолчанию ключ загружается из API WATA",
        "Optional: by default the key is downloaded from the WATA API",
    ),
    _spec("rollypay_enabled", "payments_rollypay", "Включено", "Enabled"),
    _spec(
        "rollypay_api_key",
        "payments_rollypay",
        "API-ключ кассы",
        "Cashier API key",
        "Выдаётся при создании кассы в RollyPay.",
        "Issued when the cashier is created in RollyPay.",
    ),
    _spec(
        "rollypay_signing_secret",
        "payments_rollypay",
        "Секрет подписи (signing secret)",
        "Signing secret",
        "Выдаётся вместе с ключом. В настройках кассы RollyPay укажите callback_url: https://<домен>/webhooks/rollypay",
        "Issued with the key. Set the cashier's callback_url in RollyPay to https://<domain>/webhooks/rollypay",
    ),
    _spec(
        "rollypay_payment_method",
        "payments_rollypay",
        "Способ оплаты",
        "Payment method",
        "sbp или card. Пусто — покупатель выбирает на странице оплаты.",
        "sbp or card. Empty - the payer picks on the payment page.",
    ),
    _spec("cispay_enabled", "payments_cispay", "Включено", "Enabled"),
    _spec(
        "cispay_shop_id",
        "payments_cispay",
        "ID магазина",
        "Shop ID",
        "UUID из раздела «Настройки магазина» личного кабинета cisPay.",
        "UUID from the shop settings in the cisPay cabinet.",
    ),
    _spec(
        "cispay_api_key",
        "payments_cispay",
        "Секретный ключ (cis_sec_...)",
        "Secret key (cis_sec_...)",
        "Им же подписываются вебхуки. В настройках магазина укажите webhook_url: https://<домен>/webhooks/cispay",
        "It also signs webhooks. Set the shop's webhook_url to https://<domain>/webhooks/cispay",
    ),
    _spec(
        "cispay_payment_method",
        "payments_cispay",
        "Способ оплаты",
        "Payment method",
        "sbp или card. Пусто — покупатель выбирает на странице оплаты.",
        "sbp or card. Empty - the payer picks on the payment page.",
    ),
    _spec("tabpay_enabled", "payments_tabpay", "Включено", "Enabled"),
    _spec(
        "tabpay_api_key",
        "payments_tabpay",
        "API-ключ (tp_...)",
        "API key (tp_...)",
        "Выпускается в карточке магазина в личном кабинете TabPay.",
        "Issued in the shop card in the TabPay cabinet.",
    ),
    _spec(
        "tabpay_webhook_secret",
        "payments_tabpay",
        "Секрет подписи вебхуков",
        "Webhook signing secret",
        "Вкладка «Webhook» магазина. Там же укажите адрес: https://<домен>/webhooks/tabpay",
        "On the shop's Webhook tab, where you also set the URL: https://<domain>/webhooks/tabpay",
    ),
    _spec(
        "tabpay_payment_method",
        "payments_tabpay",
        "Способ оплаты",
        "Payment method",
        "sbp или card. Пусто — покупатель выбирает на странице оплаты.",
        "sbp or card. Empty - the payer picks on the payment page.",
    ),
    _spec("severpay_enabled", "payments_severpay", "Включено", "Enabled"),
    _spec("severpay_mid", "payments_severpay", "Merchant ID (mid)", "Merchant ID (mid)"),
    _spec(
        "severpay_token",
        "payments_severpay",
        "Секретный токен",
        "Secret token",
        "Подписывает запросы и вебхуки. WEBHOOK_URL терминала: https://<домен>/webhooks/severpay",
        "Signs requests and webhooks. The terminal's WEBHOOK_URL is https://<domain>/webhooks/severpay",
    ),
    _spec(
        "severpay_client_email",
        "payments_severpay",
        "E-mail покупателя",
        "Customer e-mail",
        "API требует e-mail плательщика. {tg_id} заменяется на Telegram ID.",
        "The API requires a customer e-mail. {tg_id} is replaced with the Telegram ID.",
    ),
    _spec("cashera_enabled", "payments_cashera", "Включено", "Enabled"),
    _spec(
        "cashera_api_key",
        "payments_cashera",
        "API-ключ (pk_...)",
        "API key (pk_...)",
        "Ключ из личного кабинета Cashera.",
        "The key from the Cashera cabinet.",
    ),
    _spec(
        "cashera_secret",
        "payments_cashera",
        "Секрет (sk_...)",
        "Secret (sk_...)",
        "Приходит в заголовке X-Secret вебхуков. Включите уведомления в кабинете; адрес передаётся с каждым счётом.",
        "Arrives in the X-Secret webhook header. Enable notifications in the cabinet; the URL goes with each invoice.",
    ),
    _spec(
        "cashera_payment_method",
        "payments_cashera",
        "Способ оплаты",
        "Payment method",
        "sbp или card. Пусто — покупатель выбирает на странице оплаты.",
        "sbp or card. Empty - the payer picks on the payment page.",
    ),
    _spec("paritypay_enabled", "payments_paritypay", "Включено", "Enabled"),
    _spec("paritypay_shop_id", "payments_paritypay", "ID кассы", "Shop ID"),
    _spec(
        "paritypay_secret_key",
        "payments_paritypay",
        "Секретный ключ №1",
        "Secret key no. 1",
        "Доступ к API.",
        "API access.",
    ),
    _spec(
        "paritypay_secret_key2",
        "payments_paritypay",
        "Секретный ключ №2",
        "Secret key no. 2",
        "Подпись вебхуков. Адрес уведомлений передаётся с каждым счётом: https://<домен>/webhooks/paritypay",
        "Webhook signature. The notification URL is sent with each invoice: https://<domain>/webhooks/paritypay",
    ),
    _spec(
        "paritypay_service",
        "payments_paritypay",
        "Способ оплаты",
        "Payment method",
        "sbp или card. Пусто — покупатель выбирает на странице оплаты.",
        "sbp or card. Empty - the payer picks on the payment page.",
    ),
    _spec("aurapay_enabled", "payments_aurapay", "Включено", "Enabled"),
    _spec("aurapay_shop_id", "payments_aurapay", "ID кассы", "Shop ID"),
    _spec("aurapay_api_key", "payments_aurapay", "API-ключ", "API key"),
    _spec(
        "aurapay_secret_key2",
        "payments_aurapay",
        "Секретный ключ №2",
        "Secret key no. 2",
        "Подпись вебхуков. Адрес уведомлений передаётся с каждым счётом: https://<домен>/webhooks/aurapay",
        "Webhook signature. The notification URL is sent with each invoice: https://<domain>/webhooks/aurapay",
    ),
    _spec(
        "aurapay_service",
        "payments_aurapay",
        "Способ оплаты",
        "Payment method",
        "sbp или card. Пусто — покупатель выбирает на странице оплаты.",
        "sbp or card. Empty - the payer picks on the payment page.",
    ),
    _spec("antilopay_enabled", "payments_antilopay", "Включено", "Enabled"),
    _spec("antilopay_project_id", "payments_antilopay", "Идентификатор проекта", "Project ID", owner_only=True),
    _spec("antilopay_secret_id", "payments_antilopay", "Secret ID", "Secret ID"),
    _spec("antilopay_secret_key", "payments_antilopay", "Секретный ключ (RSA)", "Secret key (RSA)"),
    _spec(
        "antilopay_callback_key",
        "payments_antilopay",
        "Публичный ключ callback (RSA)",
        "Callback public key (RSA)",
        owner_only=True,
    ),
    _spec("antilopay_product_type", "payments_antilopay", "Тип продукта", "Product type"),
    _spec("antilopay_vat", "payments_antilopay", "Ставка НДС", "VAT rate", "10 или 22; пусто — без НДС", "10 or 22"),
    _spec(
        "antilopay_prefer_methods",
        "payments_antilopay",
        "Предпочтительные способы оплаты",
        "Preferred payment methods",
        "Через запятую: CARD_RU, SBP, SBER_PAY",
        "Comma-separated: CARD_RU, SBP, SBER_PAY",
    ),
    _spec("antilopay_customer_email", "payments_antilopay", "E-mail покупателя", "Customer e-mail"),
    _spec("antilopay_check_ip", "payments_antilopay", "Проверять IP уведомлений", "Check notification IPs"),
    # --- payments: Stripe
    _spec("stripe_enabled", "payments_stripe", "Включено", "Enabled"),
    _spec("stripe_secret_key", "payments_stripe", "Секретный ключ API", "Secret API key"),
    _spec(
        "stripe_webhook_secret",
        "payments_stripe",
        "Секрет подписи вебхука",
        "Webhook signing secret",
        "whsec_… эндпоинта /webhooks/stripe. Без него статус проверяется только опросом.",
        "whsec_… of the /webhooks/stripe endpoint. Without it the status is only polled.",
    ),
    _spec("stripe_currency", "payments_stripe", "Валюта списания", "Charge currency"),
    _spec(
        "stripe_rub_per_unit",
        "payments_stripe",
        "Курс: рублей за 1 единицу валюты",
        "Rate: rubles per 1 currency unit",
        "Сумма в валюте округляется вверх; на баланс зачисляется исходная сумма в рублях.",
        "The foreign amount is rounded up; the wallet receives the original ruble amount.",
    ),
    # --- payments: PayPal
    _spec("paypal_enabled", "payments_paypal", "Включено", "Enabled"),
    _spec("paypal_client_id", "payments_paypal", "Client ID", "Client ID", owner_only=True),
    _spec("paypal_client_secret", "payments_paypal", "Secret", "Secret"),
    _spec(
        "paypal_webhook_id",
        "payments_paypal",
        "ID вебхука",
        "Webhook ID",
        "ID вебхука /webhooks/paypal из настроек приложения PayPal. Без него статус проверяется только опросом.",
        "ID of the /webhooks/paypal webhook from the PayPal app settings. Without it the status is only polled.",
        owner_only=True,
    ),
    _spec("paypal_sandbox", "payments_paypal", "Песочница (тестовый режим)", "Sandbox (test mode)"),
    _spec("paypal_currency", "payments_paypal", "Валюта списания", "Charge currency"),
    _spec(
        "paypal_rub_per_unit",
        "payments_paypal",
        "Курс: рублей за 1 единицу валюты",
        "Rate: rubles per 1 currency unit",
        "Сумма в валюте округляется вверх; на баланс зачисляется исходная сумма в рублях.",
        "The foreign amount is rounded up; the wallet receives the original ruble amount.",
    ),
    # --- payments: Tribute
    _spec("tribute_enabled", "payments_tribute", "Включено", "Enabled"),
    _spec(
        "tribute_api_key",
        "payments_tribute",
        "API-ключ",
        "API key",
        "Им же подписываются вебхуки; URL вебхука укажите в настройках API Tribute",
        "Also signs webhooks; set the webhook URL in Tribute's API settings",
    ),
    _spec("tribute_shop_id", "payments_tribute", "ID магазина", "Shop ID", owner_only=True),
    # --- payments: lava.top
    _spec("lavatop_enabled", "payments_lavatop", "Включено", "Enabled"),
    _spec("lavatop_api_key", "payments_lavatop", "API-ключ", "API key"),
    _spec(
        "lavatop_offer_id",
        "payments_lavatop",
        "ID оффера",
        "Offer ID",
        "Оффер продукта с динамической ценой",
        "Offer of a product with a dynamic price",
        owner_only=True,
    ),
    _spec("lavatop_webhook_secret", "payments_lavatop", "Ключ вебхука", "Webhook key"),
    _spec("lavatop_payment_method", "payments_lavatop", "Способ оплаты (card/sbp)", "Payment method (card/sbp)"),
    _spec(
        "lavatop_email_domain",
        "payments_lavatop",
        "Домен e-mail покупателя",
        "Buyer e-mail domain",
        "Для пользователей без e-mail: tg<id>@<домен>",
        "For users without an e-mail: tg<id>@<domain>",
    ),
    # --- payments: custom HTTP gateway
    _spec("custom_enabled", "payments_custom", "Включено", "Enabled"),
    _spec("custom_title", "payments_custom", "Текст кнопки", "Button title"),
    _spec("custom_currency", "payments_custom", "Валюта", "Currency"),
    _spec("custom_api_key", "payments_custom", "API-ключ ({api_key})", "API key ({api_key})"),
    _spec("custom_create_method", "payments_custom", "Метод создания платежа", "Create request method"),
    _spec(
        "custom_create_url",
        "payments_custom",
        "URL создания платежа",
        "Create request URL",
        "Только http(s). Подстановки кодируются автоматически; {api_key} в URL запрещён.",
        "http(s) only. Placeholders are encoded automatically; {api_key} is not allowed in URLs.",
        owner_only=True,
    ),
    _spec(
        "custom_headers",
        "payments_custom",
        "Заголовки (JSON)",
        "Headers (JSON)",
        'Например {"Authorization": "Bearer {api_key}"}. Используются и для запроса статуса.',
        'For example {"Authorization": "Bearer {api_key}"}. Also sent with the status request.',
        owner_only=True,
    ),
    _spec("custom_body_format", "payments_custom", "Формат тела запроса", "Request body format"),
    _spec(
        "custom_create_body",
        "payments_custom",
        "Шаблон тела запроса",
        "Request body template",
        "JSON или key=value&key2={description}. Доступно: {payment_id} {amount} {amount_kop} {amount_number} "
        "{currency} {description} {user_id} {return_url} {callback_url} {api_key}.",
        "JSON or key=value&key2={description}. Available: {payment_id} {amount} {amount_kop} {amount_number} "
        "{currency} {description} {user_id} {return_url} {callback_url} {api_key}.",
        owner_only=True,
    ),
    _spec(
        "custom_response_url_path",
        "payments_custom",
        "Путь к ссылке на оплату в ответе",
        "Payment URL path in the response",
        "Через точку, например data.payment_url",
        "Dotted path, e.g. data.payment_url",
    ),
    _spec("custom_response_id_path", "payments_custom", "Путь к ID платежа в ответе", "Provider id path in response"),
    _spec("custom_status_method", "payments_custom", "Метод запроса статуса", "Status request method"),
    _spec(
        "custom_status_url",
        "payments_custom",
        "URL запроса статуса",
        "Status request URL",
        "Необязательно. Доступны {provider_id} и {payment_id}. Пусто — статус берётся из подписанного вебхука.",
        "Optional. {provider_id} and {payment_id} are available. Empty: the signed webhook status is trusted.",
        owner_only=True,
    ),
    _spec(
        "custom_status_body", "payments_custom", "Шаблон тела запроса статуса", "Status request body", owner_only=True
    ),
    _spec("custom_status_path", "payments_custom", "Путь к статусу в ответе", "Status field path"),
    _spec(
        "custom_status_values",
        "payments_custom",
        "Значения статусов (JSON)",
        "Status values (JSON)",
        'Например {"paid": "succeeded", "failed": "canceled", "new": "pending"}',
        'For example {"paid": "succeeded", "failed": "canceled", "new": "pending"}',
    ),
    _spec("custom_status_amount_path", "payments_custom", "Путь к оплаченной сумме, ₽", "Paid amount path, RUB"),
    _spec("custom_webhook_secret", "payments_custom", "Секрет HMAC вебхука", "Webhook HMAC secret"),
    _spec(
        "custom_webhook_signature_header",
        "payments_custom",
        "Заголовок с подписью",
        "Signature header",
        "HMAC считается от сырого тела вебхука.",
        "The HMAC is computed over the raw webhook body.",
    ),
    _spec("custom_webhook_hash", "payments_custom", "Алгоритм HMAC", "HMAC algorithm"),
    _spec("custom_webhook_signature_encoding", "payments_custom", "Кодировка подписи", "Signature encoding"),
    _spec(
        "custom_webhook_allowed_ips",
        "payments_custom",
        "Разрешённые IP вебхуков",
        "Allowed webhook IPs",
        "IP или подсети через запятую; пусто — без проверки IP.",
        "Comma-separated IPs or CIDRs; empty disables the IP check.",
    ),
    _spec("custom_webhook_payment_id_path", "payments_custom", "Путь к нашему ID в вебхуке", "Our payment id path"),
    _spec("custom_webhook_provider_id_path", "payments_custom", "Путь к ID провайдера в вебхуке", "Provider id path"),
    _spec("custom_webhook_status_path", "payments_custom", "Путь к статусу в вебхуке", "Webhook status path"),
    _spec("custom_webhook_amount_path", "payments_custom", "Путь к сумме в вебхуке, ₽", "Webhook amount path, RUB"),
    _spec("manual_transfer_enabled", "payments_manual", "Включено", "Enabled"),
    _spec("manual_transfer_details", "payments_manual", "Реквизиты и инструкция", "Payment instructions"),
    # --- "Мой налог" receipts
    _spec(
        "npd_enabled",
        "npd",
        "Регистрировать доход в «Мой налог»",
        "Register income in Мой налог",
        "Каждый зачисленный платёж через выбранных провайдеров регистрируется как доход самозанятого. "
        "Ответственность за правильность чеков несёт владелец.",
        "Every credited payment through the selected providers is registered as self-employed income. "
        "The owner is responsible for the receipts being correct.",
        owner_only=True,
    ),
    _spec("npd_inn", "npd", "ИНН самозанятого", "Taxpayer INN", owner_only=True),
    _spec(
        "npd_password",
        "npd",
        "Пароль от lknpd.nalog.ru",
        "lknpd.nalog.ru password",
        "Не нужен, если вход выполнен по SMS из веб-админки.",
        "Not needed after an SMS login from the web admin.",
    ),
    _spec("npd_refresh_token", "npd", "Refresh-токен (необязательно)", "Refresh token (optional)"),
    _spec(
        "npd_service_name",
        "npd",
        "Название услуги при покупке тарифа",
        "Service name for a plan purchase",
        "{plan} — название тарифа.",
        "{plan} - the plan title.",
        max=256,
    ),
    _spec(
        "npd_topup_service_name",
        "npd",
        "Название услуги для прочих платежей",
        "Service name for other payments",
        max=256,
    ),
    _spec(
        "npd_providers",
        "npd",
        "Провайдеры, по которым выдаются чеки",
        "Providers that get receipts",
        "Ключи провайдеров через запятую (yookassa, platega, ...). Stars и криптовалюта по умолчанию не включены.",
        "Comma-separated provider keys (yookassa, platega, ...). Stars and crypto are not included by default.",
        owner_only=True,
    ),
    _spec("npd_send_to_user", "npd", "Отправлять чек покупателю", "Send the receipt to the buyer"),
    _spec("npd_max_attempts", "npd", "Попыток до остановки", "Attempts before giving up", min=1, max=100),
    # --- panels
    _spec(
        "panel_request_timeout_sec",
        "panels_sync",
        "Таймаут запросов к панелям, сек",
        "Panel request timeout, sec",
        min=3,
        max=120,
        restart_required=True,
    ),
    _spec("reconcile_interval_min", "panels_sync", "Синхронизация, мин", "Sync interval, min", min=1, max=1440),
    _spec(
        "health_check_interval_min",
        "panels_sync",
        "Проверка доступности, мин",
        "Health check interval, min",
        min=1,
        max=1440,
    ),
    _spec(
        "health_failures_before_alert",
        "panels_sync",
        "Ошибок подряд до оповещения",
        "Failures before alert",
        min=1,
        max=100,
    ),
    _spec(
        "tls_watch_hosts",
        "monitoring",
        "Проверка сертификатов: имена",
        "Certificate watch: host names",
        "Имена через запятую (vpn.example.com, panel.example.com). Адрес из PUBLIC_BASE_URL проверяется всегда.",
        "Comma-separated names (vpn.example.com, panel.example.com). The PUBLIC_BASE_URL host is always checked.",
    ),
    _spec(
        "tls_watch_connect",
        "monitoring",
        "Проверка сертификатов: адрес подключения",
        "Certificate watch: connect to",
        "host:port, куда подключаться вместо самого имени (например edge:443); SNI остаётся именем сайта.",
        "host:port to connect to instead of the name itself (for example edge:443); SNI stays the watched name.",
    ),
    _spec(
        "tls_warn_days",
        "monitoring",
        "Предупреждать за дней до окончания сертификата",
        "Warn this many days before a certificate expires",
        min=3,
        max=60,
    ),
    _spec(
        "delete_orphan_clients",
        "panels_sync",
        "Удалять чужие аккаунты с панелей",
        "Delete unknown panel accounts",
        owner_only=True,
    ),
    _spec("remnawave_webhook_secret", "panels_sync", "Секрет вебхука Remnawave", "Remnawave webhook secret"),
    _spec(
        "adblock_enabled",
        "panels_sync",
        "Блокировка рекламы и трекеров",
        "Block ads and trackers",
        "Блокирует рекламные и трекерные домены для всех пользователей на серверах 3x-ui (правило маршрутизации "
        "Xray). Реклама внутри видео YouTube так не блокируется: она приходит с тех же адресов, что и видео. "
        "При изменении Xray перезапускается, активные подключения переподключатся. Remnawave, Marzban и другие "
        "панели управляют конфигом Xray сами и не затрагиваются.",
        "Blocks ad and tracker domains for everyone on 3x-ui servers (an Xray routing rule). YouTube in-video ads "
        "cannot be blocked this way because they come from the same hosts as the videos. Xray restarts when the "
        "setting changes and active connections reconnect. Remnawave, Marzban and other panels manage their own "
        "Xray config and are not affected.",
    ),
    _spec(
        "youtube_exit_enabled",
        "panels_sync",
        "Выход для YouTube через отдельный сервер",
        "YouTube through a separate exit server",
        "Трафик YouTube на серверах 3x-ui уходит через отдельный выходной сервер (в стране, где YouTube не "
        "показывает рекламу), остальное выходит как раньше. Какие страны без рекламы - меняется со временем, "
        "способ может перестать работать. Если выходной сервер недоступен, YouTube идёт обычным маршрутом. "
        "Нужна ссылка выходного сервера. При изменении Xray перезапускается один раз.",
        "Sends YouTube traffic from 3x-ui servers through a separate exit server (in a country where YouTube shows "
        "no ads); everything else exits as before. Which countries get no ads changes over time, so the method may "
        "stop working. If the exit server is down, YouTube falls back to the normal route. Needs the exit server "
        "link. Xray restarts once when this changes.",
    ),
    _spec(
        "youtube_exit_link",
        "panels_sync",
        "Ссылка выходного сервера YouTube",
        "YouTube exit server link",
        "vless:// (Reality или TLS; tcp, ws, grpc, httpupgrade, xhttp), socks5://user:pass@host:port или ss:// "
        "(Shadowsocks).",
        "vless:// (Reality or TLS; tcp, ws, grpc, httpupgrade, xhttp), socks5://user:pass@host:port or ss:// "
        "(Shadowsocks).",
    ),
    _spec(
        "youtube_exit_domains",
        "panels_sync",
        "Домены для выхода YouTube",
        "Domains for the YouTube exit",
        "По умолчанию geosite:youtube. Можно добавить, например, domain:googlevideo.com. Маршрутизация по доменам "
        "работает, если на входящих VLESS включено определение домена (бот включает его сам).",
        "geosite:youtube by default. Add more such as domain:googlevideo.com. Domain routing needs sniffing on the "
        "VLESS inbounds (the bot switches it on).",
    ),
    _spec(
        "tg_only_enabled",
        "tg_only",
        "Публичная ссылка «Telegram»",
        "Public Telegram link",
        "Одна общая ссылка для всех: через VPN идёт только Telegram, остальное работает напрямую. Её можно "
        "публиковать и пересылать, чтобы людям было как открыть Telegram (и бота). Сервер не пускает этого клиента "
        "никуда, кроме Telegram. Работает на серверах 3x-ui.",
        "One shared link for everyone: only Telegram goes through the VPN, the rest connects directly. You can "
        "publish and forward it so people can reach Telegram (and your bot). The server lets that client reach "
        "nothing but Telegram. Works on 3x-ui servers.",
    ),
    _spec(
        "tg_only_excluded_countries",
        "tg_only",
        "Страны, не подходящие для ссылки «Telegram»",
        "Countries not offered for the Telegram link",
        "Коды стран через запятую (по флагу локации). Серверы в этих странах не попадают в подписку, потому что "
        "там Telegram ограничен или заблокирован.",
        "Comma-separated country codes (taken from the location flag). Servers there are left out because "
        "Telegram is restricted or blocked in those countries.",
    ),
    _spec(
        "tg_only_allow_unknown_country",
        "tg_only",
        "Серверы без флага страны тоже подходят",
        "Also offer servers without a country flag",
        "По умолчанию сервер, у локации которого нет флага страны, в подписку не попадает: неизвестно, работает ли "
        "из его страны Telegram.",
        "Off by default: a server whose location has no country flag is left out because it is unknown whether "
        "Telegram works from there.",
    ),
    _spec(
        "tg_only_monthly_limit_gb",
        "tg_only",
        "Лимит трафика ссылки «Telegram», ГБ в месяц",
        "Telegram link traffic limit, GB per month",
        "Общий трафик общего клиента на каждом сервере, сбрасывается раз в месяц. 0 - без лимита. Применяется без "
        "перезапуска Xray.",
        "Total traffic of the shared client on each server, reset once a month. 0 means unlimited. Applied without "
        "restarting Xray.",
        min=0,
    ),
    _spec(
        "tg_only_daily_alert_gb",
        "tg_only",
        "Оповещение о трафике ссылки «Telegram», ГБ в сутки",
        "Telegram link traffic alert, GB per day",
        "Сообщить администраторам, если общий клиент за сутки передал больше этого объёма (не чаще раза в сутки). "
        "0 - выключено.",
        "Tell admins when the shared client moves more than this in a day (at most once a day). 0 turns it off.",
        min=0,
    ),
    # --- server monitoring
    _spec(
        "monitor_alerts_enabled",
        "monitoring",
        "Оповещения о нагрузке",
        "Load alerts",
        "Сообщать администраторам о высокой нагрузке, остановке Xray и отключении нод.",
        "Tell admins about high load, stopped Xray and offline nodes.",
    ),
    _spec("monitor_interval_min", "monitoring", "Проверка серверов, мин", "Server check, min", min=1, max=1440),
    _spec("monitor_cpu_alert_percent", "monitoring", "Порог CPU, %", "CPU threshold, %", min=1, max=100),
    _spec("monitor_ram_alert_percent", "monitoring", "Порог RAM, %", "RAM threshold, %", min=1, max=100),
    _spec("monitor_disk_alert_percent", "monitoring", "Порог диска, %", "Disk threshold, %", min=1, max=100),
    # --- Telegram proxy
    _spec(
        "tg_proxy_access",
        "tg_proxy",
        "Кому доступен прокси",
        "Who gets the proxy",
        "everyone — всем; subscribers — подписчикам; off — выключен (по умолчанию: прокси MTProto не работают "
        "в некоторых странах).",
        "everyone; subscribers - users with a subscription; off (the default: MTProto proxies do not work in "
        "some countries).",
    ),
    _spec(
        "tg_proxy_free_rate_limit_mbps",
        "tg_proxy",
        "Ограничение скорости без подписки, Мбит/с",
        "Speed cap without a subscription, Mbit/s",
        min=0,
    ),
    _spec("tg_proxy_free_ad_tag", "tg_proxy", "Рекламный тег без подписки", "Sponsored tag without a subscription"),
    _spec(
        "tg_proxy_max_unique_ips",
        "tg_proxy",
        "Макс. IP на пользователя",
        "Max IPs per user",
        "Сколько разных IP могут одновременно пользоваться одной ссылкой. 0 — без ограничения.",
        "How many different IPs may use one link at a time. 0 - no cap.",
        min=0,
        max=100,
    ),
    _spec(
        "tg_proxy_auto_rotate_on_abuse",
        "tg_proxy",
        "Менять ссылку при раздаче",
        "Rotate the link on sharing",
    ),
    _spec(
        "tg_proxy_max_servers",
        "tg_proxy",
        "Серверов на пользователя",
        "Servers per user",
        "Основной + запасные: если один сервер недоступен, у пользователя остаётся ссылка на другой.",
        "Primary + backups: if one server is down, the user still has a link to another.",
        min=1,
        max=10,
    ),
    _spec(
        "tg_proxy_rotate_cooldown_min",
        "tg_proxy",
        "Пауза между сменой ссылки, мин",
        "Link change cooldown, min",
        "Как часто пользователь может получить новую ссылку. 0 — без ограничения.",
        "How often a user may get a new link. 0 - no limit.",
        min=0,
        max=10080,
    ),
    _spec(
        "tg_proxy_abuse_strikes",
        "tg_proxy",
        "Проверок до признания раздачи",
        "Scans before a link counts as shared",
        "Ссылка считается розданной, если упирается в лимит IP столько проверок подряд.",
        "A link counts as shared after hitting the IP cap this many scans in a row.",
        min=1,
        max=100,
    ),
    _spec(
        "tg_proxy_abuse_ip_threshold",
        "tg_proxy",
        "Порог IP без лимита",
        "IP threshold without a cap",
        "Сколько разных IP считать раздачей, когда лимит IP на пользователя выключен.",
        "Unique IPs that count as sharing when the per-user IP cap is off.",
        min=1,
        max=1000,
    ),
    _spec(
        "tg_proxy_abuse_rotate_cooldown_hours",
        "tg_proxy",
        "Автосмена ссылки не чаще, ч",
        "Automatic link change at most every, h",
        min=0,
        max=720,
    ),
    _spec(
        "tg_proxy_scan_interval_min",
        "tg_proxy",
        "Проверка прокси-серверов, мин",
        "Proxy servers scan, min",
        "Как часто собирать трафик и искать розданные ссылки.",
        "How often to collect traffic and look for shared links.",
        min=1,
        max=1440,
    ),
    _spec(
        "tg_socks_enabled",
        "tg_proxy",
        "SOCKS5-прокси для Telegram",
        "Telegram SOCKS5 proxy",
        "Личный SOCKS5-аккаунт рядом с MTProto-ссылкой. Нужен inbound «mixed» с паролем, выбранный в панели; "
        "порт inbound должен быть открыт в файрволе и не может совпадать с 443, если его занимает TLS-сервис.",
        'A personal SOCKS5 account next to the MTProto link. Needs a password-protected "mixed" inbound selected in '
        "the panel; open its port in the firewall - it cannot share port 443 with a TLS service.",
    ),
    # --- required channel
    _spec("required_channel_id", "channel_gate", "ID канала", "Channel ID", "Например -1001234567890.", "-100..."),
    _spec("required_channel_url", "channel_gate", "Ссылка на канал", "Channel link"),
    # --- backups
    _spec("backup_enabled", "backups", "Ежедневные копии", "Daily backups"),
    _spec("backup_time", "backups", "Время копирования", "Backup time"),
    _spec("backup_chat_id", "backups", "Чат для копий", "Backup chat", owner_only=True),
    _spec("backup_keep", "backups", "Хранить копий на диске", "Local copies to keep", min=1, max=365),
    _spec(
        "backup_password",
        "backups",
        "Пароль шифрования копий",
        "Backup encryption password",
        "Без пароля копии не шифруются. Потерянный пароль восстановить нельзя.",
        "Without a password backups are not encrypted. A lost password cannot be recovered.",
    ),
    # --- cost sharing
    _spec(
        "cost_sharing_enabled",
        "cost_sharing",
        "Складчина включена",
        "Cost sharing on",
        "Только в личном режиме: раз в месяц участники получают напоминание со своей долей.",
        "Personal mode only: members get a monthly reminder with their share.",
    ),
    _spec("cost_sharing_total_rub", "cost_sharing", "Сумма в месяц, ₽", "Monthly total, RUB", min=0),
    _spec("cost_sharing_day", "cost_sharing", "День месяца", "Day of month", min=1, max=28),
    _spec(
        "cost_sharing_details",
        "cost_sharing",
        "Как оплатить",
        "How to pay",
        "Например номер карты или телефона для перевода.",
        "E.g. a card or phone number for the transfer.",
    ),
    _spec("cost_sharing_include_owner", "cost_sharing", "Владелец тоже платит долю", "The owner pays a share too"),
    # --- web
    _spec("web_admin_enabled", "webapp", "Веб-админка", "Web admin"),
    _spec("miniapp_enabled", "webapp", "Mini App", "Mini App"),
    _spec("web_session_hours", "webapp", "Сессия веб-админки, ч", "Web admin session, hours", min=1, max=720),
    # --- web cabinet
    _spec(
        "cabinet_enabled",
        "cabinet",
        "Веб-кабинет",
        "Web cabinet",
        "Личный кабинет клиентов в браузере (/cabinet) для случаев, когда Telegram недоступен. Нужен HTTPS-адрес "
        "PUBLIC_BASE_URL. Адрес возврата после оплаты меняется после перезапуска.",
        "Customer account in a browser (/cabinet) for when Telegram is unavailable. Requires an HTTPS "
        "PUBLIC_BASE_URL. The post-payment return address changes after a restart.",
    ),
    _spec(
        "cabinet_login_bot",
        "cabinet",
        "Вход по ссылке из бота",
        "Login link from the bot",
        "Кнопка «Веб-кабинет» в боте выдаёт одноразовую ссылку.",
        "The bot's Web cabinet button issues a one-time link.",
    ),
    _spec(
        "cabinet_login_telegram",
        "cabinet",
        "Вход через Telegram Login Widget",
        "Telegram Login Widget",
        "Домен нужно привязать командой /setdomain в @BotFather.",
        "Link the domain with /setdomain in @BotFather.",
    ),
    _spec(
        "cabinet_login_email",
        "cabinet",
        "Вход по ссылке на e-mail",
        "E-mail magic link",
        "Требует настроенный SMTP; адрес нужно подтвердить в кабинете.",
        "Needs SMTP; the address is confirmed in the cabinet.",
    ),
    _spec("cabinet_session_days", "cabinet", "Срок сессии, дней", "Session lifetime, days", min=1, max=365),
    _spec("smtp_host", "cabinet", "SMTP: сервер", "SMTP host", owner_only=True),
    _spec("smtp_port", "cabinet", "SMTP: порт", "SMTP port", min=1, max=65535),
    _spec(
        "smtp_security",
        "cabinet",
        "SMTP: шифрование",
        "SMTP security",
        "starttls (порт 587), ssl (порт 465) или none.",
        "starttls (port 587), ssl (port 465) or none.",
    ),
    _spec("smtp_username", "cabinet", "SMTP: логин", "SMTP username", owner_only=True),
    _spec("smtp_password", "cabinet", "SMTP: пароль", "SMTP password"),
    _spec(
        "smtp_from",
        "cabinet",
        "SMTP: адрес отправителя",
        "SMTP sender",
        "Например: VPN <no-reply@example.com>.",
        "For example: VPN <no-reply@example.com>.",
    ),
    _spec(
        "trusted_proxies",
        "webapp",
        "Доверенные прокси",
        "Trusted proxies",
        "IP или подсети обратного прокси, которым можно доверять X-Forwarded-For.",
        "Reverse proxy IPs/CIDRs whose X-Forwarded-For is trusted.",
        owner_only=True,
    ),
    # --- anti-spam
    _spec("throttle_rate_limit", "antispam", "Действий за период", "Actions per period", min=1, max=1000),
    _spec("throttle_period_sec", "antispam", "Период, сек", "Period, sec", min=1, max=3600),
    # --- external blacklist
    _spec(
        "blacklist_enabled",
        "blacklist",
        "Проверять по чёрному списку",
        "Check against the blacklist",
        "Сверять пользователей со списками Telegram ID ниже.",
        "Match users against the Telegram ID lists below.",
    ),
    _spec(
        "blacklist_urls",
        "blacklist",
        "Внешние списки (URL)",
        "External lists (URLs)",
        "Текстовые файлы: один ID в строке, можно «ID причина», строки с # — комментарии. До 20 адресов.",
        'Plain text: one ID per line, optionally "ID reason"; lines starting with # are comments. Up to 20 URLs.',
        owner_only=True,
    ),
    _spec(
        "blacklist_refresh_minutes",
        "blacklist",
        "Обновлять каждые, мин",
        "Refresh every, min",
        min=5,
        max=10080,
    ),
    _spec(
        "blacklist_action",
        "blacklist",
        "Действие",
        "Action",
        "flag — только отметить и сообщить администраторам; block — заблокировать доступ к боту и VPN.",
        "flag - mark and notify administrators only; block - ban the user from the bot and VPN.",
    ),
    _spec(
        "blacklist_ban_paying",
        "blacklist",
        "Блокировать и платящих",
        "Ban paying users too",
        "Выключено — пользователей с активной платной подпиской только отмечаем.",
        "Off - users with an active paid subscription are only flagged.",
    ),
    _spec(
        "blacklist_manual_ids",
        "blacklist",
        "Свой список ID",
        "Own ID list",
        "Telegram ID через запятую.",
        "Comma-separated Telegram IDs.",
        min=1,
    ),
    # --- i18n
    _spec("default_language", "i18n", "Язык по умолчанию", "Default language"),
    _spec(
        "enabled_languages",
        "i18n",
        "Доступные языки",
        "Available languages",
        "Коды через запятую: ru, en, uk, kk, uz, fa, zh. Язык по умолчанию доступен всегда.",
        "Comma-separated codes: ru, en, uk, kk, uz, fa, zh. The default language is always available.",
    ),
    _spec(
        "tz",
        "i18n",
        "Часовой пояс",
        "Time zone",
        "Например Europe/Moscow.",
        "E.g. Europe/Moscow.",
        restart_required=True,
    ),
    # --- maintenance
    _spec(
        "maintenance_notify_on_finish",
        "maintenance",
        "Сообщать об окончании техработ",
        "Notify when maintenance ends",
    ),
    _spec(
        "maintenance_announce_minutes",
        "maintenance",
        "Предупреждать о плановых работах за, мин",
        "Announce planned maintenance, minutes before",
        min=0,
        max=10080,
    ),
    # --- broadcasts
    _spec(
        "broadcast_rate_per_sec",
        "broadcasts",
        "Скорость рассылки, сообщений/с",
        "Broadcast speed, messages/s",
        min=1,
        max=30,
    ),
    # --- admin notifications
    _spec(
        "admin_chat_id",
        "admin_notify",
        "Чат администраторов",
        "Admin chat",
        "Группа для уведомлений (вместо чатов для отчётов). Бот должен быть её участником.",
        "Group for notifications (instead of report chats). The bot must be a member.",
        owner_only=True,
    ),
    _spec("admin_topic_payments", "admin_notify", "Тема: платежи", "Topic: payments"),
    _spec("admin_topic_users", "admin_notify", "Тема: пользователи", "Topic: users"),
    _spec("admin_topic_tickets", "admin_notify", "Тема: обращения", "Topic: tickets"),
    _spec("admin_topic_withdrawals", "admin_notify", "Тема: выводы", "Topic: withdrawals"),
    _spec("admin_topic_backups", "admin_notify", "Тема: резервные копии", "Topic: backups"),
    _spec("admin_topic_alerts", "admin_notify", "Тема: сбои", "Topic: alerts"),
    _spec(
        "admin_notify_mute",
        "admin_notify",
        "Не присылать",
        "Muted categories",
        "Через запятую: payments, users, tickets, withdrawals, backups, alerts.",
        "Comma-separated: payments, users, tickets, withdrawals, backups, alerts.",
    ),
    # --- system
    _spec(
        "disabled_modules",
        "system",
        "Отключённые разделы",
        "Disabled features",
        "Названия модулей через запятую.",
        "Comma-separated module names.",
        owner_only=True,
    ),
    _spec(
        "admin_report_chat_ids",
        "system",
        "Чаты для отчётов",
        "Report chats",
        "Пусто — владельцам из ADMIN_IDS.",
        "Empty - owners from ADMIN_IDS.",
        owner_only=True,
    ),
    _spec(
        "bot_proxy_url",
        "system",
        "Прокси для бота",
        "Bot proxy",
        "socks5://user:pass@host:port — если Telegram недоступен с сервера.",
        "socks5://user:pass@host:port - when Telegram is blocked on the server.",
        restart_required=True,
        owner_only=True,
    ),
    _spec(
        "update_check_repo",
        "system",
        "Проверка обновлений",
        "Update check",
        "Репозиторий на GitHub в виде owner/name. Пусто — не проверять.",
        "GitHub repository as owner/name. Empty - do not check.",
        owner_only=True,
    ),
    _spec(
        "telegram_api_url",
        "system",
        "Свой сервер Bot API",
        "Self-hosted Bot API server",
        restart_required=True,
        owner_only=True,
    ),
]

REGISTRY: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}


def register(specs: list[SettingSpec]) -> None:
    """Add settings declared by a feature module (keys must be Settings fields and not yet declared)."""
    for spec in specs:
        if spec.key not in Settings.model_fields:
            raise KeyError(f"unknown setting {spec.key!r}")
        if spec.key in ENV_ONLY:
            raise ValueError(f"setting {spec.key!r} is environment-only")
        existing = REGISTRY.get(spec.key)
        if existing is not None and existing != spec:
            raise ValueError(f"setting {spec.key!r} is already declared")
        REGISTRY[spec.key] = spec


def get_spec(key: str) -> SettingSpec:
    """The declaration of an editable setting; KeyError for unknown or environment-only keys."""
    try:
        return REGISTRY[key]
    except KeyError:
        raise KeyError(f"setting {key!r} is not editable at runtime") from None


def by_group() -> dict[str, list[SettingSpec]]:
    """Editable settings grouped in display order."""
    grouped: dict[str, list[SettingSpec]] = {group: [] for group in GROUPS}
    for spec in REGISTRY.values():
        grouped[spec.group].append(spec)
    return {group: specs for group, specs in grouped.items() if specs}


def undeclared_fields() -> set[str]:
    """Settings fields that are neither editable nor environment-only (should be empty)."""
    return set(Settings.model_fields) - set(REGISTRY) - ENV_ONLY
