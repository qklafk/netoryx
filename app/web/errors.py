"""Uniform JSON errors for the HTTP API: ``{"error": "<code>", "message": "<text>"}``."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.web.masking import mask_path

log = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    """Error response model (also used in OpenAPI ``responses=``)."""

    error: str
    message: str


class ApiError(Exception):
    """Raise from routes and dependencies to return a JSON error with a stable machine-readable code."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message or _default_message(status_code)
        self.headers = headers
        self.extra = extra
        super().__init__(f"{status_code} {code}: {self.message}")


def _default_message(status_code: int) -> str:
    try:
        return HTTPStatus(status_code).phrase
    except ValueError:
        return "Error"


_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "too_many_requests",
    500: "internal_error",
    503: "unavailable",
}


def code_for(status_code: int) -> str:
    """Default error code for an HTTP status."""
    return _CODES.get(status_code, "error" if status_code < 500 else "internal_error")


def error_response(
    status_code: int,
    code: str | None = None,
    message: str | None = None,
    *,
    headers: dict[str, str] | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    """Build the standard JSON error response."""
    body: dict[str, Any] = {"error": code or code_for(status_code), "message": message or _default_message(status_code)}
    if extra:
        body.update({k: v for k, v in extra.items() if k not in body})
    return JSONResponse(body, status_code=status_code, headers=headers)


async def _api_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status_code, exc.code, exc.message, headers=exc.headers, extra=exc.extra)


async def _http_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    message = exc.detail if isinstance(exc.detail, str) else None
    return error_response(exc.status_code, message=message, headers=getattr(exc, "headers", None))


async def _validation_error(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"loc": [str(part) for part in err.get("loc", ())], "msg": str(err.get("msg", "")), "type": err.get("type")}
        for err in exc.errors()
    ]
    return error_response(422, "validation_error", "Request validation failed", extra={"details": details})


async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    path = mask_path(request.url.path)
    log.error("unhandled error in %s %s (request %s)", request.method, path, request_id, exc_info=exc)
    return error_response(500, "internal_error", "Internal server error")


def install_error_handlers(app: FastAPI) -> None:
    """Register the JSON error handlers on ``app``."""
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(Exception, _unhandled)
