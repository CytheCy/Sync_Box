"""Sanitized diagnostics for Box SDK failures."""

from __future__ import annotations

import re
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class BoxFileConflict:
    """Existing Box file reported by an item-name upload conflict."""

    content_id: str
    name: str
    sha1: str
    version_id: str | None = None
    etag: str | None = None
    size: int | None = None


def is_expired_content_token_error(exc: Exception) -> bool:
    """Recognize only the SDK failure raised for an expired access token."""
    return (
        isinstance(exc, BoxSDKError)
        and type(exc) is BoxSDKError
        and getattr(exc, "message", None) == _EXPIRED_DEVELOPER_TOKEN_MESSAGE
    )


def matching_upload_conflict(
    exc: Exception, *, name: str, sha1: str
) -> BoxFileConflict | None:
    """Return the existing file only when a 409 proves identical content."""
    response = getattr(exc, "response_info", None)
    if (
        getattr(response, "status_code", None) != 409
        or getattr(response, "code", None) != "item_name_in_use"
    ):
        return None

    context = getattr(response, "context_info", None)
    if not isinstance(context, dict):
        body = getattr(response, "body", None)
        context = body.get("context_info") if isinstance(body, dict) else None
    conflicts = context.get("conflicts") if isinstance(context, dict) else None
    if isinstance(conflicts, dict):
        conflicts = [conflicts]
    if not isinstance(conflicts, list):
        return None

    matches: list[BoxFileConflict] = []
    for conflict in conflicts:
        if not isinstance(conflict, dict):
            continue
        conflict_sha1 = conflict.get("sha1", conflict.get("sha_1"))
        content_id = conflict.get("id")
        item_type = conflict.get("type")
        conflict_name = conflict.get("name")
        if (
            item_type != "file"
            or conflict_name != name
            or not isinstance(content_id, str)
            or not isinstance(conflict_sha1, str)
            or conflict_sha1.lower() != sha1.lower()
        ):
            continue
        version = conflict.get("file_version")
        version_id = version.get("id") if isinstance(version, dict) else None
        size = conflict.get("size")
        matches.append(
            BoxFileConflict(
                content_id=content_id,
                name=conflict_name,
                sha1=conflict_sha1,
                version_id=version_id if isinstance(version_id, str) else None,
                etag=conflict.get("etag") if isinstance(conflict.get("etag"), str) else None,
                size=size if isinstance(size, int) else None,
            )
        )
    return matches[0] if len(matches) == 1 else None


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
