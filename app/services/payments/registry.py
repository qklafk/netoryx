"""Build the enabled payment gateways from settings."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from app.config import Settings
from app.services.payments.antilopay import AntilopayGateway
from app.services.payments.aurapay import AuraPayGateway
from app.services.payments.base import PaymentGateway
from app.services.payments.cashera import CasheraGateway
from app.services.payments.cispay import CisPayGateway
from app.services.payments.cloudpayments import CloudPaymentsGateway
from app.services.payments.cryptobot import CryptoBotGateway
from app.services.payments.custom_http import CustomHttpConfig, CustomHttpGateway
from app.services.payments.freekassa import FreeKassaGateway
from app.services.payments.heleket import HeleketGateway
from app.services.payments.lavatop import LavaTopGateway
from app.services.payments.manual import ManualTransferGateway
from app.services.payments.nowpayments import NowPaymentsGateway
from app.services.payments.paritypay import ParityPayGateway
from app.services.payments.paypal import PayPalGateway
from app.services.payments.platega import PlategaGateway
from app.services.payments.plisio import PlisioGateway
from app.services.payments.robokassa import RobokassaGateway
from app.services.payments.rollypay import RollyPayGateway
from app.services.payments.severpay import SeverPayGateway
from app.services.payments.stars import StarsGateway
from app.services.payments.stripe import StripeGateway
from app.services.payments.tabpay import TabPayGateway
from app.services.payments.ton import TonGateway
from app.services.payments.tribute import TributeGateway
from app.services.payments.tron import UsdtTrc20Gateway
from app.services.payments.wata import WataGateway
from app.services.payments.yookassa import YooKassaGateway
from app.services.payments.yoomoney import YooMoneyGateway

if TYPE_CHECKING:
    from app.db.session import SessionFactory

log = logging.getLogger(__name__)


def _secret(value: object) -> str:
    """Plain value of a SecretStr setting ("" when unset)."""
    getter = getattr(value, "get_secret_value", None)
    return str(getter()) if getter else ""


def _callback_url(settings: Settings, provider: str) -> str | None:
    """This installation's webhook URL for ``provider`` (sent with invoices by providers that accept one)."""
    base = (settings.public_base_url or "").rstrip("/")
    return f"{base}/webhooks/{provider}" if base else None


def _onchain_gateways(
    settings: Settings, http_timeout: float, session_factory: SessionFactory | None
) -> list[PaymentGateway]:
    """Direct TON / USDT TRC-20 gateways. USDT needs the database to reserve unique invoice amounts."""
    gateways: list[PaymentGateway] = []
    auto_rate, rate_cache_sec = settings.crypto_rate_auto, settings.crypto_rate_cache_sec
    if settings.ton_enabled:
        gateways.append(
            TonGateway(
                settings.ton_wallet_address or "",
                api_key=_secret(settings.ton_api_key) or None,
                rate_rub=settings.ton_rate_rub,
                base_url=settings.ton_api_url,
                timeout=http_timeout,
                auto_rate=auto_rate,
                rate_cache_sec=rate_cache_sec,
            )
        )
    if settings.usdt_trc20_enabled:
        if session_factory is None:
            log.warning("USDT TRC-20 payments need a database session factory; the method is disabled")
        else:
            gateways.append(
                UsdtTrc20Gateway(
                    settings.usdt_trc20_address or "",
                    session_factory,
                    api_key=_secret(settings.usdt_trc20_api_key) or None,
                    rate_rub=settings.usdt_trc20_rate_rub,
                    base_url=settings.usdt_trc20_api_url,
                    timeout=http_timeout,
                    auto_rate=auto_rate,
                    rate_cache_sec=rate_cache_sec,
                )
            )
    return gateways


def build_gateways(
    settings: Settings,
    http_timeout: float = 15.0,
    *,
    return_url: str | None = None,
    session_factory: SessionFactory | None = None,
) -> dict[str, PaymentGateway]:
    """Enabled gateways keyed by provider, in the order they are offered to users.

    ``return_url`` is where providers send the user after paying (usually ``https://t.me/<bot>``);
    ``YOOKASSA_RETURN_URL`` overrides it for YooKassa. ``session_factory`` is needed by gateways that keep
    state in the database (USDT TRC-20 amount reservations); without it they are not offered.
    """
    gateways: list[PaymentGateway] = []
    if settings.platega_enabled:
        gateways.append(
            PlategaGateway(
                settings.platega_merchant_id or "",
                _secret(settings.platega_secret),
                payment_method=settings.platega_payment_method,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.yookassa_enabled:
        gateways.append(
            YooKassaGateway(
                settings.yookassa_shop_id or "",
                _secret(settings.yookassa_secret_key),
                return_url=settings.yookassa_return_url or return_url,
                receipt_enabled=settings.yookassa_receipt_enabled,
                receipt_email=settings.yookassa_receipt_email,
                vat_code=settings.yookassa_vat_code,
                check_ip=settings.yookassa_check_ip,
                trusted_proxies=settings.trusted_proxies,
                timeout=http_timeout,
            )
        )
    if settings.cloudpayments_enabled:
        gateways.append(
            CloudPaymentsGateway(
                settings.cloudpayments_public_id or "",
                _secret(settings.cloudpayments_api_secret),
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.yoomoney_enabled:
        gateways.append(
            YooMoneyGateway(
                settings.yoomoney_wallet or "",
                _secret(settings.yoomoney_notification_secret),
                payment_type=settings.yoomoney_payment_type,
                return_url=return_url,
            )
        )
    if settings.freekassa_enabled:
        gateways.append(
            FreeKassaGateway(
                settings.freekassa_shop_id or "",
                _secret(settings.freekassa_secret1),
                _secret(settings.freekassa_secret2),
                api_key=_secret(settings.freekassa_api_key) or None,
                check_ip=settings.freekassa_check_ip,
                trusted_proxies=settings.trusted_proxies,
                timeout=http_timeout,
            )
        )
    if settings.robokassa_enabled:
        gateways.append(
            RobokassaGateway(
                settings.robokassa_merchant_login or "",
                _secret(settings.robokassa_password1),
                _secret(settings.robokassa_password2),
                hash_algorithm=settings.robokassa_hash_algorithm,
                test_mode=settings.robokassa_test_mode,
                check_ip=settings.robokassa_check_ip,
                trusted_proxies=settings.trusted_proxies,
            )
        )
    if settings.cryptobot_enabled:
        gateways.append(
            CryptoBotGateway(
                _secret(settings.cryptobot_token),
                testnet=settings.cryptobot_testnet,
                accepted_assets=settings.cryptobot_accepted_assets,
                expires_in_sec=settings.payment_max_pending_hours * 3600,
                timeout=http_timeout,
            )
        )
    if settings.heleket_enabled:
        gateways.append(
            HeleketGateway(
                settings.heleket_merchant_id or "",
                _secret(settings.heleket_api_key),
                currency=settings.heleket_currency,
                to_currency=settings.heleket_to_currency,
                return_url=return_url,
                callback_url=f"{settings.public_base_url.rstrip('/')}/webhooks/heleket"
                if settings.public_base_url
                else None,
                timeout=http_timeout,
            )
        )
    if settings.nowpayments_enabled:
        gateways.append(
            NowPaymentsGateway(
                _secret(settings.nowpayments_api_key),
                _secret(settings.nowpayments_ipn_secret),
                pay_currency=settings.nowpayments_pay_currency,
                return_url=return_url,
                callback_url=f"{settings.public_base_url.rstrip('/')}/webhooks/nowpayments"
                if settings.public_base_url
                else None,
                timeout=http_timeout,
            )
        )
    if settings.plisio_enabled:
        gateways.append(
            PlisioGateway(
                _secret(settings.plisio_api_key),
                currency=settings.plisio_currency,
                return_url=return_url,
                callback_url=f"{settings.public_base_url.rstrip('/')}/webhooks/plisio"
                if settings.public_base_url
                else None,
                timeout=http_timeout,
            )
        )
    gateways.extend(_onchain_gateways(settings, http_timeout, session_factory))
    if settings.wata_enabled:
        gateways.append(
            WataGateway(
                _secret(settings.wata_token),
                sandbox=settings.wata_sandbox,
                public_key=settings.wata_public_key or None,
                return_url=return_url,
                link_ttl=timedelta(hours=settings.payment_max_pending_hours),
                timeout=http_timeout,
            )
        )
    if settings.rollypay_enabled:
        gateways.append(
            RollyPayGateway(
                _secret(settings.rollypay_api_key),
                _secret(settings.rollypay_signing_secret),
                payment_method=settings.rollypay_payment_method or None,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.cispay_enabled:
        gateways.append(
            CisPayGateway(
                settings.cispay_shop_id or "",
                _secret(settings.cispay_api_key),
                payment_method=settings.cispay_payment_method or None,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.tabpay_enabled:
        gateways.append(
            TabPayGateway(
                _secret(settings.tabpay_api_key),
                _secret(settings.tabpay_webhook_secret),
                payment_method=settings.tabpay_payment_method or None,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.severpay_enabled:
        gateways.append(
            SeverPayGateway(
                settings.severpay_mid or 0,
                _secret(settings.severpay_token),
                email_template=settings.severpay_client_email,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.cashera_enabled:
        gateways.append(
            CasheraGateway(
                _secret(settings.cashera_api_key),
                _secret(settings.cashera_secret),
                payment_method=settings.cashera_payment_method or None,
                callback_url=_callback_url(settings, "cashera"),
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.paritypay_enabled:
        gateways.append(
            ParityPayGateway(
                settings.paritypay_shop_id or "",
                _secret(settings.paritypay_secret_key),
                _secret(settings.paritypay_secret_key2),
                service=settings.paritypay_service or None,
                callback_url=_callback_url(settings, "paritypay"),
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.aurapay_enabled:
        gateways.append(
            AuraPayGateway(
                settings.aurapay_shop_id or "",
                _secret(settings.aurapay_api_key),
                _secret(settings.aurapay_secret_key2),
                service=settings.aurapay_service or None,
                callback_url=_callback_url(settings, "aurapay"),
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.antilopay_enabled:
        gateways.append(
            AntilopayGateway(
                settings.antilopay_project_id or "",
                _secret(settings.antilopay_secret_id),
                _secret(settings.antilopay_secret_key),
                settings.antilopay_callback_key or "",
                product_type=settings.antilopay_product_type,
                vat=settings.antilopay_vat,
                prefer_methods=(settings.antilopay_prefer_methods or "").split(","),
                customer_email=settings.antilopay_customer_email,
                return_url=return_url if return_url and return_url.startswith("https://") else None,
                check_ip=settings.antilopay_check_ip,
                trusted_proxies=settings.trusted_proxies,
                timeout=http_timeout,
            )
        )
    if settings.stripe_enabled:
        gateways.append(
            StripeGateway(
                _secret(settings.stripe_secret_key),
                webhook_secret=_secret(settings.stripe_webhook_secret) or None,
                currency=settings.stripe_currency,
                rub_per_unit=settings.stripe_rub_per_unit,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.paypal_enabled:
        gateways.append(
            PayPalGateway(
                settings.paypal_client_id or "",
                _secret(settings.paypal_client_secret),
                webhook_id=settings.paypal_webhook_id,
                sandbox=settings.paypal_sandbox,
                currency=settings.paypal_currency,
                rub_per_unit=settings.paypal_rub_per_unit,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.tribute_enabled:
        gateways.append(
            TributeGateway(
                _secret(settings.tribute_api_key),
                shop_id=settings.tribute_shop_id,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.lavatop_enabled:
        gateways.append(
            LavaTopGateway(
                _secret(settings.lavatop_api_key),
                settings.lavatop_offer_id or "",
                _secret(settings.lavatop_webhook_secret),
                email_domain=settings.lavatop_email_domain or urlsplit(settings.public_base_url or "").hostname,
                payment_method=settings.lavatop_payment_method,
                return_url=return_url,
                timeout=http_timeout,
            )
        )
    if settings.custom_enabled:
        gateways.append(
            CustomHttpGateway(
                CustomHttpConfig.from_settings(settings),
                return_url=return_url,
                callback_url=f"{settings.public_base_url.rstrip('/')}/webhooks/custom"
                if settings.public_base_url
                else None,
                trusted_proxies=settings.trusted_proxies,
                timeout=http_timeout,
            )
        )
    if settings.manual_transfer_enabled:
        gateways.append(ManualTransferGateway())
    if settings.stars_enabled:
        gateways.append(StarsGateway(settings))
    return {gateway.key: gateway for gateway in gateways}


async def close_gateways(gateways: dict[str, PaymentGateway]) -> None:
    """Close every gateway's HTTP session."""
    for gateway in gateways.values():
        await gateway.close()
