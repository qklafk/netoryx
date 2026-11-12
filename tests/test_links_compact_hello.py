"""REALITY links can ask for a ClientHello that fits into one TCP segment."""

from __future__ import annotations

from app.services.links import compact_reality_hello


def test_reality_params_get_firefox_without_post_quantum_share() -> None:
    params = {"security": "reality", "fp": "chrome", "pbk": "key", "sid": "ab"}
    compact = compact_reality_hello(params)
    assert compact["fp"] == "firefox"
    assert compact["support-x25519mlkem768"] == "false"
    assert compact["pbk"] == "key" and params["fp"] == "chrome"  # the input is not modified


def test_other_security_types_are_unchanged() -> None:
    for security in ("tls", "none", ""):
        params = {"security": security, "fp": "chrome"}
        assert compact_reality_hello(params) == params
