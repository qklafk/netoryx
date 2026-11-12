"""Owner-configured HTTP gateway for payment providers without a dedicated integration.

The operator describes the provider's API in settings: the "create invoice" request (method, URL,
headers, JSON or form body template), where the payment link and the provider's id are in the
response, an optional status request, and how webhooks are authenticated (HMAC of the raw body,
optionally plus a source-IP allow-list) and where the ids and status are in their bodies.

Templates use placeholders such as ``{payment_id}`` and ``{amount}``. Values are always substituted
after the template has been parsed and are encoded for their context (JSON strings, percent-encoded URL
parts, form fields), so a value can never change the structure of a request.

When a status request is configured a webhook only says which payment changed and the status is
fetched from the provider; otherwise the HMAC-verified webhook body is trusted.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, quote, urlencode, urlsplit

from app.db.models import PaymentStatus
from app.services.payments.base import (
    GatewayError,
    HttpGateway,
    Invoice,
    IPNetwork,
    ProviderStatus,
    WebhookRejected,
    WebhookResult,
    amount_to_kop,
    client_ip,
    get_header,
    ip_in,
    kop_to_number,
    kop_to_str,
    parse_networks,
)
from app.utils.security import const_eq

if TYPE_CHECKING:
    from app.db.models import Payment, User

KEY = "custom"

PLACEHOLDER = re.compile(r"\{([A-Za-z_]+)\}")
CREATE_FIELDS = frozenset(
    {
        "payment_id",
        "amount",
        "amount_kop",
        "amount_number",
        "currency",
        "description",
        "user_id",
        "return_url",
        "callback_url",
        "api_key",
    }
)
STATUS_FIELDS = CREATE_FIELDS | {"provider_id"}
# Never placed into URLs: error messages and proxy logs show them.
URL_FORBIDDEN = frozenset({"api_key"})
STATUS_NAMES = {
    "succeeded": PaymentStatus.SUCCEEDED,
    "canceled": PaymentStatus.CANCELED,
    "pending": PaymentStatus.PENDING,
}
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_MISSING = object()
MAX_PROVIDER_ID = 128


class CustomGatewayConfigError(ValueError):
    """The gateway settings are incomplete or invalid."""


@dataclass(frozen=True, slots=True)
class CustomHttpConfig:
    """Raw gateway settings as the operator entered them."""

    title: str = "💳 Оплата"
    currency: str = "RUB"
    api_key: str = ""
    create_method: str = "POST"
    create_url: str | None = None
    headers: str | None = None  # JSON object; shared by the create and status requests
    body_format: str = "json"  # "json" | "form"
    create_body: str | None = None
    response_url_path: str | None = None
    response_id_path: str | None = None
    status_method: str = "GET"
    status_url: str | None = None
    status_body: str | None = None
    status_path: str | None = None
    status_values: str | None = None  # JSON object: provider value -> succeeded | canceled | pending
    status_amount_path: str | None = None
    webhook_secret: str = ""
    webhook_signature_header: str | None = None
    webhook_hash: str = "sha256"
    webhook_signature_encoding: str = "hex"
    webhook_allowed_ips: tuple[str, ...] = ()
    webhook_payment_id_path: str | None = None
    webhook_provider_id_path: str | None = None
    webhook_status_path: str | None = None
    webhook_amount_path: str | None = None

    @classmethod
    def from_settings(cls, settings: Any) -> CustomHttpConfig:
        """Collect the ``custom_*`` settings."""

        def secret(value: Any) -> str:
            return str(value.get_secret_value()) if value is not None else ""

        return cls(
            title=settings.custom_title,
            currency=settings.custom_currency,
            api_key=secret(settings.custom_api_key),
            create_method=settings.custom_create_method,
            create_url=settings.custom_create_url,
            headers=settings.custom_headers,
            body_format=settings.custom_body_format,
            create_body=settings.custom_create_body,
            response_url_path=settings.custom_response_url_path,
            response_id_path=settings.custom_response_id_path,
            status_method=settings.custom_status_method,
            status_url=settings.custom_status_url,
            status_body=settings.custom_status_body,
            status_path=settings.custom_status_path,
            status_values=settings.custom_status_values,
            status_amount_path=settings.custom_status_amount_path,
            webhook_secret=secret(settings.custom_webhook_secret),
            webhook_signature_header=settings.custom_webhook_signature_header,
            webhook_hash=settings.custom_webhook_hash,
            webhook_signature_encoding=settings.custom_webhook_signature_encoding,
            webhook_allowed_ips=tuple(settings.custom_webhook_allowed_ips),
            webhook_payment_id_path=settings.custom_webhook_payment_id_path,
            webhook_provider_id_path=settings.custom_webhook_provider_id_path,
            webhook_status_path=settings.custom_webhook_status_path,
            webhook_amount_path=settings.custom_webhook_amount_path,
        )


# ---------------------------------------------------------------------------------------------
# Compiled templates
# ---------------------------------------------------------------------------------------------
Path = tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Request:
    method: str
    url: str
    body_format: str
    json_body: Any = None
    form_body: tuple[tuple[str, str], ...] | None = None

    def body_fields(self) -> set[str]:
        names: set[str] = set()
        if self.json_body is not None:
            names |= _json_placeholders(self.json_body)
        for _, value in self.form_body or ():
            names |= set(_placeholders(value))
        return names


@dataclass(slots=True)
class _Compiled:
    headers: dict[str, str] = field(default_factory=dict)
    create: _Request | None = None
    status: _Request | None = None
    response_url: Path = ()
    response_id: Path | None = None
    status_path: Path | None = None
    status_amount: Path | None = None
    status_values: dict[str, PaymentStatus] = field(default_factory=dict)
    allowed_ips: list[IPNetwork] = field(default_factory=list)
    webhook_payment_id: Path | None = None
    webhook_provider_id: Path | None = None
    webhook_status: Path | None = None
    webhook_amount: Path | None = None


def _placeholders(text: str) -> list[str]:
    return PLACEHOLDER.findall(text)


def _json_placeholders(node: Any) -> set[str]:
    if isinstance(node, str):
        return set(_placeholders(node))
    if isinstance(node, dict):
        return set().union(*(_json_placeholders(v) for v in node.values())) if node else set()
    if isinstance(node, list):
        return set().union(*(_json_placeholders(v) for v in node)) if node else set()
    return set()


def _request_fields(request: _Request, headers: Mapping[str, str]) -> set[str]:
    """Every placeholder a request uses: URL, body and the shared headers."""
    names = set(_placeholders(request.url)) | request.body_fields()
    for value in headers.values():
        names |= set(_placeholders(value))
    return names


def _check_fields(names: set[str], allowed: frozenset[str], where: str, errors: list[str]) -> None:
    unknown = sorted(names - allowed)
    if unknown:
        errors.append(f"{where}: unknown placeholder(s) {', '.join('{' + n + '}' for n in unknown)}")


def _parse_path(value: str | None, name: str, errors: list[str], *, required: bool = False) -> Path | None:
    text = (value or "").strip()
    if not text:
        if required:
            errors.append(f"{name} is required")
        return None
    parts = tuple(part.strip() for part in text.split("."))
    if any(not part for part in parts):
        errors.append(f"{name}: invalid path {text!r}")
        return None
    return parts


def _check_url(template: str, name: str, allowed: frozenset[str], errors: list[str]) -> str | None:
    text = template.strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        errors.append(f"{name}: invalid URL")
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        errors.append(f"{name}: only absolute http(s) URLs are allowed")
        return None
    if "{" in parts.netloc or "}" in parts.netloc:
        errors.append(f"{name}: placeholders are not allowed in the host")
        return None
    if any(ch in text for ch in "\r\n\t "):
        errors.append(f"{name}: URL must not contain whitespace")
        return None
    names = set(_placeholders(text))
    forbidden = sorted(names & URL_FORBIDDEN)
    if forbidden:
        errors.append(f"{name}: {', '.join('{' + n + '}' for n in forbidden)} must not be placed in a URL")
    _check_fields(names - URL_FORBIDDEN, allowed, name, errors)
    return text


def _parse_body(template: str | None, body_format: str, name: str, errors: list[str]) -> tuple[Any, Any]:
    """(json_body, form_body) of a body template; both None for no body."""
    text = (template or "").strip()
    if not text:
        return None, None
    if body_format == "json":
        try:
            return json.loads(text), None
        except ValueError:
            errors.append(f"{name}: not valid JSON")
            return None, None
    if body_format == "form":
        try:
            pairs = parse_qsl(text, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            errors.append(f"{name}: not a valid form template (key=value&key2=value2)")
            return None, None
        return None, tuple(pairs)
    errors.append(f"unknown body format {body_format!r}")
    return None, None


def _parse_headers(text: str | None, errors: list[str]) -> dict[str, str]:
    if not (text or "").strip():
        return {}
    try:
        data = json.loads(text or "")
    except ValueError:
        errors.append("headers: not valid JSON")
        return {}
    if not isinstance(data, dict):
        errors.append("headers: must be a JSON object")
        return {}
    headers: dict[str, str] = {}
    for key, value in data.items():
        if not _HEADER_NAME.fullmatch(key):
            errors.append(f"headers: invalid header name {key!r}")
            continue
        if not isinstance(value, str):
            errors.append(f"headers: value of {key!r} must be a string")
            continue
        if "\r" in value or "\n" in value:
            errors.append(f"headers: value of {key!r} must be a single line")
            continue
        _check_fields(set(_placeholders(value)), STATUS_FIELDS, f"headers: {key}", errors)
        headers[key] = value
    return headers


def _parse_status_values(text: str | None, errors: list[str]) -> dict[str, PaymentStatus]:
    if not (text or "").strip():
        errors.append("status values mapping is required")
        return {}
    try:
        data = json.loads(text or "")
    except ValueError:
        errors.append("status values: not valid JSON")
        return {}
    if not isinstance(data, dict) or not data:
        errors.append("status values: must be a non-empty JSON object")
        return {}
    mapping: dict[str, PaymentStatus] = {}
    for key, value in data.items():
        status = STATUS_NAMES.get(str(value).strip().lower()) if isinstance(value, str) else None
        if status is None:
            errors.append(f"status values: {key!r} must map to succeeded, canceled or pending")
            continue
        mapping[key.strip().lower()] = status
    if PaymentStatus.SUCCEEDED not in mapping.values():
        errors.append("status values: at least one provider value must map to succeeded")
    return mapping


def _compile(config: CustomHttpConfig) -> tuple[_Compiled, list[str]]:
    errors: list[str] = []
    out = _Compiled()
    if not config.title.strip():
        errors.append("button title is required")
    if not config.currency.strip():
        errors.append("currency is required")
    out.headers = _parse_headers(config.headers, errors)

    # Create request.
    if config.create_method not in ("GET", "POST", "PUT"):
        errors.append("create method must be GET, POST or PUT")
    if config.body_format not in ("json", "form"):
        errors.append("body format must be json or form")
    if not (config.create_url or "").strip():
        errors.append("create URL is required")
    else:
        url = _check_url(config.create_url or "", "create URL", CREATE_FIELDS, errors)
        json_body, form_body = _parse_body(config.create_body, config.body_format, "create body", errors)
        if config.create_method == "GET" and (json_body is not None or form_body is not None):
            errors.append("create body is not allowed with GET")
        if url is not None:
            out.create = _Request(config.create_method, url, config.body_format, json_body, form_body)
            _check_fields(out.create.body_fields(), CREATE_FIELDS, "create body", errors)
    out.response_url = _parse_path(config.response_url_path, "response payment URL path", errors, required=True) or ()
    out.response_id = _parse_path(config.response_id_path, "response provider id path", errors)

    # Status request (optional).
    has_status = bool((config.status_url or "").strip())
    out.status_values = _parse_status_values(config.status_values, errors)
    if has_status:
        if config.status_method not in ("GET", "POST"):
            errors.append("status method must be GET or POST")
        url = _check_url(config.status_url or "", "status URL", STATUS_FIELDS, errors)
        json_body, form_body = _parse_body(config.status_body, config.body_format, "status body", errors)
        if config.status_method == "GET" and (json_body is not None or form_body is not None):
            errors.append("status body is not allowed with GET")
        if url is not None:
            out.status = _Request(config.status_method, url, config.body_format, json_body, form_body)
            _check_fields(out.status.body_fields(), STATUS_FIELDS, "status body", errors)
            names = _request_fields(out.status, out.headers)
            if "provider_id" in names and out.response_id is None:
                errors.append("status request uses {provider_id}: set the response provider id path")
        out.status_path = _parse_path(config.status_path, "status field path", errors, required=True)
        out.status_amount = _parse_path(config.status_amount_path, "status amount path", errors)
    elif (config.status_body or "").strip():
        errors.append("status body is set but the status URL is empty")
    if "provider_id" in {n for v in out.headers.values() for n in _placeholders(v)} and not has_status:
        errors.append("headers: {provider_id} is only available to the status request")

    # Webhook authentication (mandatory) and mapping.
    if not config.webhook_secret:
        errors.append("webhook HMAC secret is required")
    header = (config.webhook_signature_header or "").strip()
    if not header:
        errors.append("webhook signature header is required")
    elif not _HEADER_NAME.fullmatch(header):
        errors.append("webhook signature header: invalid header name")
    if config.webhook_hash not in ("sha256", "sha512"):
        errors.append("webhook hash must be sha256 or sha512")
    if config.webhook_signature_encoding not in ("hex", "base64"):
        errors.append("webhook signature encoding must be hex or base64")
    for item in config.webhook_allowed_ips:
        try:
            ipaddress.ip_network(item.strip(), strict=False)
        except ValueError:
            errors.append(f"webhook allowed IPs: invalid entry {item!r}")
    out.allowed_ips = parse_networks(i for i in config.webhook_allowed_ips if i.strip())
    out.webhook_payment_id = _parse_path(config.webhook_payment_id_path, "webhook payment id path", errors)
    out.webhook_provider_id = _parse_path(config.webhook_provider_id_path, "webhook provider id path", errors)
    if out.webhook_payment_id is None and out.webhook_provider_id is None:
        errors.append("webhook payment id path or webhook provider id path is required")
    elif out.webhook_payment_id is None and out.response_id is None:
        errors.append("webhooks identify payments by provider id: set the response provider id path")
    out.webhook_status = _parse_path(config.webhook_status_path, "webhook status path", errors, required=not has_status)
    out.webhook_amount = _parse_path(config.webhook_amount_path, "webhook amount path", errors)
    return out, errors


def config_errors(config: CustomHttpConfig) -> list[str]:
    """Human-readable problems with the settings (empty when valid). Never includes secret values."""
    return _compile(config)[1]


# ---------------------------------------------------------------------------------------------
# Rendering and response helpers
# ---------------------------------------------------------------------------------------------
def _render_text(template: str, values: Mapping[str, str], encode: Any = None) -> str:
    def sub(match: re.Match[str]) -> str:
        value = values.get(match.group(1), "")
        return encode(value) if encode is not None else value

    return PLACEHOLDER.sub(sub, template)


def _render_json(node: Any, values: Mapping[str, str], typed: Mapping[str, Any]) -> Any:
    """Substitute placeholders inside the decoded JSON template; a string that is exactly one typed
    placeholder (``"{amount_kop}"``) becomes a JSON number."""
    if isinstance(node, str):
        whole = PLACEHOLDER.fullmatch(node)
        if whole and whole.group(1) in typed:
            return typed[whole.group(1)]
        return _render_text(node, values)
    if isinstance(node, dict):
        return {key: _render_json(value, values, typed) for key, value in node.items()}
    if isinstance(node, list):
        return [_render_json(value, values, typed) for value in node]
    return node


def _url_part(value: str) -> str:
    return quote(value, safe="")


def get_path(data: Any, path: Sequence[str]) -> Any:
    """Follow a dotted path through objects and arrays; ``_MISSING`` if absent."""
    current = data
    for part in path:
        if isinstance(current, dict):
            if part not in current:
                return _MISSING
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
    return current


def _scalar(value: Any) -> str | None:
    """A scalar as text (booleans as true/false); None for missing, null, objects and arrays."""
    if value is _MISSING or value is None or isinstance(value, dict | list):
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


class CustomHttpGateway(HttpGateway):
    """A provider described entirely by settings (see :class:`CustomHttpConfig`)."""

    key = KEY
    min_amount_kop = 100

    def __init__(
        self,
        config: CustomHttpConfig,
        *,
        return_url: str | None = None,
        callback_url: str | None = None,
        trusted_proxies: Sequence[str] = (),
        timeout: float = 15,
    ) -> None:
        super().__init__(timeout=timeout)
        compiled, errors = _compile(config)
        if errors:
            raise CustomGatewayConfigError("; ".join(errors))
        self._c = compiled
        self._config = config
        self.title = config.title.strip()
        self._currency = config.currency.strip().upper()
        self._return_url = return_url or ""
        self._callback_url = callback_url or ""
        self._trusted = parse_networks(trusted_proxies)
        status = compiled.status
        self.supports_polling = status is not None
        uses_provider_id = status is not None and "provider_id" in _request_fields(status, compiled.headers)
        self.polls_by_payment_id = status is not None and not uses_provider_id
        self._status_needs_provider_id = uses_provider_id
        digest = hashlib.sha512 if config.webhook_hash == "sha512" else hashlib.sha256
        self._digest = digest
        self._secret = config.webhook_secret.encode()
        self._signature_header = (config.webhook_signature_header or "").strip()

    # --- requests ------------------------------------------------------------------------------
    def _values(self, payment: Payment, description: str, provider_id: str = "") -> dict[str, str]:
        return {
            "payment_id": payment.id,
            "amount": kop_to_str(payment.amount_kop),
            "amount_kop": str(payment.amount_kop),
            "amount_number": kop_to_str(payment.amount_kop),
            "currency": self._currency,
            "description": description,
            "user_id": str(payment.user_id),
            "return_url": self._return_url,
            "callback_url": self._callback_url,
            "api_key": self._config.api_key,
            "provider_id": provider_id,
        }

    async def _send(self, request: _Request, payment: Payment, values: dict[str, str], *, retries: int) -> Any:
        url = _render_text(request.url, values, _url_part)
        if urlsplit(url).scheme.lower() not in ("http", "https"):
            raise GatewayError("request URL is not http(s)")
        headers = {
            name: _render_text(value, values).replace("\r", " ").replace("\n", " ")
            for name, value in self._c.headers.items()
        }
        kwargs: dict[str, Any] = {"headers": headers}
        if request.json_body is not None:
            typed = {
                "amount_kop": payment.amount_kop,
                "amount_number": kop_to_number(payment.amount_kop),
                "user_id": payment.user_id,
            }
            body = _render_json(request.json_body, values, typed)
            kwargs["data"] = json.dumps(body).encode()
            if get_header(headers, "Content-Type") is None:
                headers["Content-Type"] = "application/json"
        elif request.form_body is not None:
            pairs = [(key, _render_text(value, values)) for key, value in request.form_body]
            kwargs["data"] = urlencode(pairs).encode()
            if get_header(headers, "Content-Type") is None:
                headers["Content-Type"] = "application/x-www-form-urlencoded"
        return await self._request_json(request.method, url, retries=retries, ok_statuses=(200, 201, 202), **kwargs)

    async def create(self, payment: Payment, user: User, description: str) -> Invoice:
        assert self._c.create is not None
        values = self._values(payment, description)
        data = await self._send(self._c.create, payment, values, retries=0)
        link = _scalar(get_path(data, self._c.response_url))
        if not link or urlsplit(link).scheme.lower() not in ("http", "https"):
            raise GatewayError("provider response has no http(s) payment URL at the configured path")
        provider_id: str | None = None
        if self._c.response_id is not None:
            provider_id = _scalar(get_path(data, self._c.response_id))
            if not provider_id or len(provider_id) > MAX_PROVIDER_ID:
                raise GatewayError("provider response has no valid payment id at the configured path")
        return Invoice(provider_id, link, kop_to_str(payment.amount_kop), self._currency)

    def _status_of(self, value: Any) -> PaymentStatus:
        token = _scalar(value)
        if token is None:
            raise ValueError("status field is missing")
        return self._c.status_values.get(token.lower(), PaymentStatus.PENDING)

    async def fetch(self, payment: Payment) -> ProviderStatus:
        request = self._c.status
        if request is None:
            raise GatewayError("status request is not configured")
        if self._status_needs_provider_id and not payment.provider_payment_id:
            raise GatewayError("payment has no provider id")
        values = self._values(payment, "", payment.provider_payment_id or "")
        data = await self._send(request, payment, values, retries=2)
        assert self._c.status_path is not None
        try:
            state = self._status_of(get_path(data, self._c.status_path))
        except ValueError as exc:
            raise GatewayError("provider response has no status at the configured path") from exc
        paid = None
        if state is PaymentStatus.SUCCEEDED and self._c.status_amount is not None:
            try:
                paid = amount_to_kop(_scalar(get_path(data, self._c.status_amount)) or "")
            except ValueError as exc:
                raise GatewayError("provider response has no valid amount at the configured path") from exc
        return ProviderStatus(state, paid, payment.provider_payment_id)

    # --- webhooks ------------------------------------------------------------------------------
    def _signature_ok(self, raw: bytes, given: str | None) -> bool:
        if not given:
            return False
        text = given.strip()
        prefix = f"{self._config.webhook_hash}="
        if text.lower().startswith(prefix):
            text = text[len(prefix) :]
        mac = hmac.new(self._secret, raw, self._digest).digest()
        if self._config.webhook_signature_encoding == "base64":
            return const_eq(base64.b64encode(mac).decode(), text)
        return const_eq(mac.hex(), text.lower())

    async def parse_webhook(self, raw: bytes, headers: Mapping[str, str], remote_ip: str | None) -> WebhookResult:
        if self._c.allowed_ips and not ip_in(client_ip(remote_ip, headers, self._trusted), self._c.allowed_ips):
            raise WebhookRejected("webhook from a disallowed IP", 403)
        if not self._signature_ok(raw, get_header(headers, self._signature_header)):
            raise WebhookRejected("bad webhook signature", 401)
        data = _webhook_data(raw)
        payment_ids: list[str] = []
        provider_ids: list[str] = []
        if self._c.webhook_payment_id is not None:
            ours = _scalar(get_path(data, self._c.webhook_payment_id))
            if ours:
                payment_ids.append(ours)
        if self._c.webhook_provider_id is not None:
            theirs = _scalar(get_path(data, self._c.webhook_provider_id))
            if theirs:
                provider_ids.append(theirs)
        if not payment_ids and not provider_ids:
            raise WebhookRejected("webhook carries no payment id", 400)
        if self._c.status is not None:
            return WebhookResult(payment_ids=payment_ids, provider_ids=provider_ids)
        assert self._c.webhook_status is not None
        try:
            state = self._status_of(get_path(data, self._c.webhook_status))
        except ValueError as exc:
            raise WebhookRejected("webhook carries no status", 400) from exc
        paid = None
        if state is PaymentStatus.SUCCEEDED and self._c.webhook_amount is not None:
            try:
                paid = amount_to_kop(_scalar(get_path(data, self._c.webhook_amount)) or "")
            except ValueError as exc:
                raise WebhookRejected("webhook carries no valid amount", 400) from exc
        # No provider id in the trusted status: the payment keeps the id from its invoice, and a
        # redelivery is absorbed by the idempotent credit.
        return WebhookResult(
            payment_ids=payment_ids, provider_ids=provider_ids, trusted_status=ProviderStatus(state, paid)
        )


def _webhook_data(raw: bytes) -> Any:
    """A JSON object/array body, or a form-encoded body as a flat object."""
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        data = None
    if isinstance(data, dict | list):
        return data
    try:
        pairs = parse_qsl(raw.decode(), keep_blank_values=True, strict_parsing=True)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WebhookRejected("malformed webhook body", 400) from exc
    if not pairs:
        raise WebhookRejected("malformed webhook body", 400)
    return dict(pairs)
