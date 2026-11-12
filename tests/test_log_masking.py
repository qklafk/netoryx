"""Secrets are removed from log lines, including the text of exceptions and bot tokens in URLs."""

from __future__ import annotations

import logging
import sys

from app.runtime import JsonFormatter, SecretMaskFilter

TOKEN = "123456789:AAEhBOweik6ad9r_QXMENQjcrTu-oWc0eFg"


def _record_with_exception(message: str) -> logging.LogRecord:
    try:
        raise RuntimeError(message)
    except RuntimeError:
        return logging.LogRecord("x", logging.ERROR, __file__, 1, "request failed", None, sys.exc_info())


def test_exception_text_is_masked_in_text_and_json_output() -> None:
    mask = SecretMaskFilter(["configured-secret-value"])
    record = _record_with_exception(f"404 for https://api.telegram.org/file/bot{TOKEN}/photos/1.jpg")
    mask.filter(record)
    text = logging.Formatter("%(message)s").format(record)
    assert TOKEN not in text and "RuntimeError" in text
    record = _record_with_exception("failed with configured-secret-value")
    mask.filter(record)
    assert "configured-secret-value" not in JsonFormatter().format(record)


def test_bot_token_in_a_message_is_masked_without_being_configured() -> None:
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "GET /bot%s/getMe", (TOKEN,), None)
    SecretMaskFilter().filter(record)
    assert TOKEN not in record.getMessage()
