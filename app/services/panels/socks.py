"""Telegram SOCKS5 proxy: account types, passwords and share links.

Personal SOCKS5 accounts live on a 3x-ui ``mixed`` inbound (SOCKS5 + HTTP on one port; panels before the rename call
the protocol ``socks``) with password authentication. Its ``settings.accounts`` list holds ``{"user", "pass"}``
pairs; unlike VLESS/MTProto users these are not panel "clients", so the backend edits the list through the inbound
update API (:class:`SocksProxyBackend`, implemented by :mod:`app.services.panels.xui`).

Links (https://core.telegram.org/api/links)::

    tg://socks?server=<host>&port=<port>&user=<user>&pass=<password>
    https://t.me/socks?server=<host>&port=<port>&user=<user>&pass=<password>

SOCKS5 itself is not encrypted; Telegram's own traffic stays end-to-end encrypted by MTProto. Links carry the
password: never log them.
"""

from __future__ import annotations

import re
import secrets
import string
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import quote, urlencode

MIXED: Final = "mixed"
SOCKS: Final = "socks"  # older panels / Xray builds
SOCKS_PROTOCOLS: Final = frozenset({MIXED, SOCKS})

PASSWORD_LENGTH: Final = 20
_ALPHABET: Final = string.ascii_letters + string.digits
USERNAME_PREFIX: Final = "tg"
_USERNAME_RE: Final = re.compile(r"tg[0-9]+")


def new_password() -> str:
    """A random alphanumeric password (~119 bits)."""
    return "".join(secrets.choice(_ALPHABET) for _ in range(PASSWORD_LENGTH))


def username_for(user_id: int) -> str:
    return f"{USERNAME_PREFIX}{user_id}"


def is_managed_username(name: str) -> bool:
    """True for names created by :func:`username_for` (other accounts of the inbound are never touched)."""
    return _USERNAME_RE.fullmatch(name) is not None


@dataclass(frozen=True, slots=True)
class SocksLink:
    """One way to reach a SOCKS5 proxy: host, port and credentials."""

    server: str
    port: int
    user: str
    password: str

    def _query(self) -> str:
        # quote_via=quote: percent-encoding for every reserved character (and %20 for spaces, not "+").
        return urlencode(
            {"server": self.server, "port": str(self.port), "user": self.user, "pass": self.password},
            quote_via=quote,
        )

    @property
    def tg_url(self) -> str:
        """``tg://socks?...`` for copy-paste."""
        return f"tg://socks?{self._query()}"

    @property
    def tme_url(self) -> str:
        """``https://t.me/socks?...`` for inline buttons and ``Telegram.WebApp.openTelegramLink``."""
        return f"https://t.me/socks?{self._query()}"


@dataclass(frozen=True, slots=True)
class SocksAccount:
    """An account of the inbound's list."""

    user: str
    password: str


def parse_accounts(settings: Mapping[str, Any]) -> list[SocksAccount]:
    """The valid entries of an inbound's ``accounts`` list (malformed ones are skipped)."""
    accounts = []
    for item in settings.get("accounts") or []:
        if isinstance(item, dict) and isinstance(item.get("user"), str) and item["user"]:
            accounts.append(SocksAccount(item["user"], item["pass"] if isinstance(item.get("pass"), str) else ""))
    return accounts


@runtime_checkable
class SocksProxyBackend(Protocol):
    """Account operations on a ``mixed`` inbound. ``target`` is the inbound's remote id."""

    async def list_socks_users(self, target: int | None) -> list[SocksAccount]: ...

    async def apply_socks_users(
        self, target: int | None, put: Mapping[str, str], remove: Collection[str] = ()
    ) -> list[SocksAccount]:
        """Add/replace the accounts in ``put`` (name -> password) and delete ``remove`` in one inbound update.

        Other accounts are kept untouched. Returns the resulting list. Idempotent: nothing is written when the
        inbound already looks as requested.
        """
        ...

    async def delete_socks_user(self, target: int | None, name: str) -> None:
        """Delete; "not found" is success."""
        ...
