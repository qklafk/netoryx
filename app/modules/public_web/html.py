"""Small server-rendered pages for people who open a link in a browser (no external resources, strict CSP).

Every dynamic value is escaped by the caller with :func:`esc`; a page may carry one inline script, allowed by a
per-response nonce.
"""

from __future__ import annotations

import json
import secrets
from html import escape

from fastapi.responses import HTMLResponse

# Colors follow the visitor's system theme: light by default, dark when the device prefers it.
STYLE = (
    ":root{color-scheme:light dark;--bg:#f1f5f9;--card:#fff;--fg:#0f172a;--text:#334155;--muted:#64748b;"
    "--link:#0369a1;--btn:#e2e8f0;--btn-fg:#0f172a;--code:#f1f5f9;--ok:#15803d;--bad:#b91c1c;"
    "--shadow:0 10px 30px rgba(15,23,42,.08)}"
    "@media (prefers-color-scheme:dark){:root{--bg:#0f172a;--card:#1e293b;--fg:#e2e8f0;--text:#cbd5e1;"
    "--muted:#94a3b8;--link:#38bdf8;--btn:#334155;--btn-fg:#f8fafc;--code:#0f172a;--ok:#4ade80;--bad:#f87171;"
    "--shadow:0 10px 30px rgba(0,0,0,.35)}}"
    "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg);margin:0;"
    "display:flex;min-height:100vh;align-items:center;justify-content:center}"
    "main{width:100%;max-width:30rem;margin:1rem;padding:1.6rem;background:var(--card);border-radius:1.2rem;"
    "box-shadow:var(--shadow)}"
    "h1{font-size:1.35rem;margin:0 0 .6rem}h2{font-size:1.05rem;margin:1.4rem 0 .6rem}"
    "p{line-height:1.5;color:var(--text);margin:.5rem 0}a{color:var(--link)}"
    ".btn{display:block;text-align:center;text-decoration:none;font-weight:600;padding:.8rem 1rem;margin:.5rem 0;"
    "border-radius:.8rem;background:var(--btn);color:var(--btn-fg);border:0;width:100%;box-sizing:border-box;"
    "font-size:1rem;cursor:pointer}.btn.main{background:#22c55e;color:#052e16}.btn.small{display:inline-block;"
    "width:auto;padding:.5rem .8rem;margin:.25rem .25rem .25rem 0;font-weight:500;font-size:.9rem}"
    ".grid{display:grid;grid-template-columns:1fr 1fr;gap:.5rem}.grid .btn{margin:0}"
    "code{display:block;word-break:break-all;background:var(--code);padding:.6rem;border-radius:.5rem;font-size:.85rem}"
    ".muted{color:var(--muted);font-size:.9rem}.ok{color:var(--ok)}.bad{color:var(--bad)}"
    ".qr{display:block;margin:1rem auto;width:13rem;height:13rem;border-radius:.6rem;background:#fff;"
    "border:1px solid var(--btn)}"
    "details{margin-top:1rem}summary{cursor:pointer;color:var(--muted)}"
)


def esc(value: object) -> str:
    return escape(str(value), quote=True)


def js_string(value: str) -> str:
    """A JavaScript string literal safe to embed in a ``<script>`` element."""
    return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def csp(nonce: str | None) -> str:
    script = f"'nonce-{nonce}'" if nonce else "'none'"
    return (
        f"default-src 'none'; script-src {script}; style-src 'unsafe-inline'; img-src data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def new_nonce() -> str:
    return secrets.token_urlsafe(16)


def page(
    title: str,
    body_html: str,
    *,
    lang: str = "ru",
    script: str | None = None,
    nonce: str | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """A standalone page; ``body_html`` must be escaped already. ``script`` runs with the response's nonce."""
    if script is not None and nonce is None:
        nonce = new_nonce()
    script_html = f'<script nonce="{nonce}">{script}</script>' if script is not None else ""
    document = (
        f'<!doctype html><html lang="{esc(lang)}"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        f"<title>{esc(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body_html}</main>{script_html}</body></html>"
    )
    headers = {"Cache-Control": "no-store", "Content-Security-Policy": csp(nonce if script is not None else None)}
    return HTMLResponse(document, status_code=status_code, headers=headers)
