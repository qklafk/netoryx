"""Hide secrets that travel in URLs (subscription tokens, one-time links, webhook secrets) before logging."""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode

MASK = "***"

# Path prefix -> number of following segments that are public (kept); the next segment is a secret.
# "/open/<app>/<token>" keeps the app name, "/sub/<token>/wl" masks only the token.
_SECRET_AFTER: tuple[tuple[str, int], ...] = (
    ("sub", 0),
    ("open", 1),
    ("gift", 0),
    ("dev", 1),  # /dev/pay/<payment id>
)
_PREFIX_RULES = dict(_SECRET_AFTER)

# Anything that looks like a random token in any other position: long run of URL-safe characters.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-.~=]{24,}$")
_SENSITIVE_QUERY = re.compile(
    r"(token|secret|key|sig|signature|hash|init_?data|auth|password|pass|code|session|csrf)", re.IGNORECASE
)


def mask_path(path: str) -> str:
    """``path`` with secret segments replaced by ``***``."""
    if not path:
        return path
    parts = path.split("/")
    # parts[0] is "" for absolute paths
    start = 1 if parts and parts[0] == "" else 0
    secret_index: int | None = None
    if len(parts) > start:
        keep = _PREFIX_RULES.get(parts[start])
        if keep is not None:
            secret_index = start + 1 + keep
    out: list[str] = []
    for i, part in enumerate(parts):
        if part and (i == secret_index or _TOKEN_RE.match(part)):
            out.append(MASK)
        else:
            out.append(part)
    return "/".join(out)


def mask_query(query: str) -> str:
    """Query string with values of sensitive-looking parameters replaced by ``***``."""
    if not query:
        return query
    try:
        pairs = parse_qsl(query, keep_blank_values=True)
    except ValueError:
        return MASK
    masked = [(k, MASK if _SENSITIVE_QUERY.search(k) or _TOKEN_RE.match(v) else v) for k, v in pairs]
    return urlencode(masked, safe="*")


def mask_url(path: str, query: str = "") -> str:
    """Masked ``path?query`` for log lines."""
    masked = mask_path(path)
    q = mask_query(query)
    return f"{masked}?{q}" if q else masked
