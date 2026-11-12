"""Tiny server-rendered HTML pages (placeholders, development helpers). No scripts, everything escaped."""

from __future__ import annotations

from html import escape

from fastapi.responses import HTMLResponse

_STYLE = (
    "body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:#0f172a;color:#e2e8f0;"
    "display:flex;min-height:100vh;margin:0;align-items:center;justify-content:center}"
    "main{max-width:32rem;padding:2rem;background:#1e293b;border-radius:1rem;text-align:center}"
    "h1{font-size:1.4rem;margin:0 0 1rem}p{line-height:1.5;color:#cbd5e1}a{color:#38bdf8}"
    "button{font-size:1rem;padding:.7rem 1.4rem;border:0;border-radius:.6rem;background:#22c55e;color:#04210f;"
    "cursor:pointer}code{background:#0f172a;padding:.1rem .3rem;border-radius:.3rem}"
)


def page(title: str, body_html: str, *, status_code: int = 200) -> HTMLResponse:
    """A minimal standalone page; ``body_html`` must already be escaped by the caller."""
    html = (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main><h1>{escape(title)}</h1>{body_html}</main></body></html>"
    )
    return HTMLResponse(html, status_code=status_code, headers={"Cache-Control": "no-store"})
