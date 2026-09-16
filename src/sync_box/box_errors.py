"""Sanitized diagnostics for Box SDK failures."""

from __future__ import annotations

import re
from typing import Any

from box_sdk_gen.box.errors import BoxSDKError


_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(access[_-]?token|refresh[_-]?token|client[_-]?secret|authorization)"
    r"(\s*[:=]\s*)([^\s,;&]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[^\s,;&]+")
_EXPIRED_DEVELOPER_TOKEN_MESSAGE = (
    "Developer token has expired. Please provide a new one."
)


def is_expired_content_token_error(exc: Exception) -> bool:
    """Recognize only the SDK failure raised for an expired access token."""
    return (
        isinstance(exc, BoxSDKError)
        and type(exc) is BoxSDKError
        and getattr(exc, "message", None) == _EXPIRED_DEVELOPER_TOKEN_MESSAGE
    )


def safe_error_detail(value: Any, *, limit: int = 500) -> str:
    """Return bounded, single-line error text with common credentials removed."""
    text = " ".join(str(value).split())
    text = _BEARER_TOKEN.sub("Bearer [REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    return text[:limit]


def format_box_api_error(exc: Exception) -> str:
    """Describe a Box failure without rendering request headers or token values."""
    response = getattr(exc, "response_info", None)
    body = getattr(response, "body", None)
    if not isinstance(body, dict):
        body = {}

    fields = (
        ("status", getattr(response, "status_code", None)),
        ("code", getattr(response, "code", None)),
        ("message", body.get("message")),
        ("request_id", getattr(response, "request_id", None)),
    )
    details = [
        f"{name}={safe_error_detail(value)!r}"
        for name, value in fields
        if value is not None and safe_error_detail(value)
    ]
    if details:
        return "Box API error (" + ", ".join(details) + ")"

    message = getattr(exc, "message", None)
    if message:
        return f"Box SDK error ({safe_error_detail(message)})"
    return f"Box SDK error ({type(exc).__name__})"
