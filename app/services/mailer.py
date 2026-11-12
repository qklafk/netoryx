"""Plain-text e-mail over SMTP (stdlib ``smtplib`` in a worker thread, so the event loop never blocks)."""

from __future__ import annotations

import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from app.config import Settings

log = logging.getLogger(__name__)

SMTP_TIMEOUT_SEC = 15.0
MAX_SUBJECT_LENGTH = 200


class MailError(Exception):
    """The message could not be delivered to the SMTP server (details are logged, never shown to users)."""


def _sender(settings: Settings) -> str | None:
    raw = (settings.smtp_from or "").strip()
    name, address = parseaddr(raw)
    if not address or "@" not in address or any(c in raw for c in "\r\n"):
        return None
    return formataddr((name, address)) if name else address


def smtp_configured(settings: Settings) -> bool:
    """SMTP host and a sender address are set."""
    return bool(settings.smtp_host and _sender(settings))


def build_message(settings: Settings, to: str, subject: str, body: str) -> EmailMessage:
    """The message to send; raises :class:`MailError` for an unusable sender or recipient."""
    sender = _sender(settings)
    if sender is None:
        raise MailError("SMTP sender address is not configured")
    if any(c in to for c in "\r\n,;<>") or "@" not in to:
        raise MailError("invalid recipient address")
    message = EmailMessage()
    message["From"] = sender
    message["To"] = to
    message["Subject"] = " ".join(subject.split())[:MAX_SUBJECT_LENGTH]
    message.set_content(body)
    return message


def _send_blocking(settings: Settings, message: EmailMessage) -> None:
    host = settings.smtp_host or ""
    port = settings.smtp_port
    context = ssl.create_default_context()
    client: smtplib.SMTP
    if settings.smtp_security == "ssl":
        client = smtplib.SMTP_SSL(host, port, timeout=SMTP_TIMEOUT_SEC, context=context)
    else:
        client = smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SEC)
    with client:
        if settings.smtp_security == "starttls":
            client.starttls(context=context)
        if settings.smtp_username:
            password = settings.smtp_password.get_secret_value() if settings.smtp_password else ""
            client.login(settings.smtp_username, password)
        client.send_message(message)


async def send_mail(settings: Settings, to: str, subject: str, body: str) -> None:
    """Send one message; raises :class:`MailError` on any failure."""
    if not smtp_configured(settings):
        raise MailError("SMTP is not configured")
    message = build_message(settings, to, subject, body)
    try:
        await asyncio.to_thread(_send_blocking, settings, message)
    except (OSError, smtplib.SMTPException, ssl.SSLError) as exc:
        log.warning("SMTP delivery failed: %s", type(exc).__name__)
        raise MailError("SMTP delivery failed") from None
