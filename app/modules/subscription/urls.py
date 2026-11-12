"""Public URLs of a user's subscription: the subscription itself and the one-tap import pages for client apps."""

from __future__ import annotations

from app.config import Settings


def _base(settings: Settings) -> str | None:
    base = (settings.public_base_url or "").rstrip("/")
    return base or None


def subscription_url(settings: Settings, sub_token: str) -> str | None:
    """``<PUBLIC_BASE_URL>/sub/<token>``; ``None`` while the public address is not configured."""
    base = _base(settings)
    return f"{base}/sub/{sub_token}" if base else None


def whitelist_url(settings: Settings, sub_token: str) -> str | None:
    """The separate URL with the LTE ("white list") entries (``SUB_WHITELIST_SEPARATE_URL``)."""
    url = subscription_url(settings, sub_token)
    return f"{url}/wl" if url else None


def open_url(settings: Settings, app_key: str, sub_token: str) -> str | None:
    """HTTPS page that opens ``app_key`` and imports the subscription (Telegram buttons cannot use app schemes)."""
    base = _base(settings)
    return f"{base}/open/{app_key}/{sub_token}" if base else None
