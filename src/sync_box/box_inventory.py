"""Read-only recursive Box folder inventory."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sync_box.box_errors import format_box_api_error
from sync_box.inventory import (
    InventoryItem,
    ScanError,
    ensure_unique_path,
    is_excluded_path,
    normalize_relative_path,
)


BOX_FIELDS = [
    "id",
    "type",
    "name",
    "size",
    "modified_at",
    "etag",
    "sha1",
    "sequence_id",
    "version_number",
    "file_version",
]


def scan_box(
    client: Any,
    folder_id: str,
    *,
    excluded_paths: tuple[str, ...] = (),
    excluded_names: tuple[str, ...] = (),
) -> list[InventoryItem]:
    """Inventory Box metadata using GET-only SDK folder methods."""
    try:
        root = client.folders.get_folder_by_id(folder_id, fields=BOX_FIELDS)
    except Exception as exc:
        raise ScanError(
            f"Cannot read configured Box folder {folder_id}: "
            f"{format_box_api_error(exc)}"
        ) from exc

    items = [_box_item(root, ".", forced_type="folder")]
    seen = {".": "."}
    pending: list[tuple[str, tuple[str, ...]]] = [(folder_id, ())]

    while pending:
        current_id, parent_parts = pending.pop()
        marker: str | None = None
        while True:
            arguments: dict[str, object] = {
                "fields": BOX_FIELDS,
                "usemarker": True,
                "limit": 1000,
            }
            if marker:
                arguments["marker"] = marker
            try:
                page = client.folders.get_folder_items(current_id, **arguments)
            except Exception as exc:
                raise ScanError(
                    f"Cannot list Box folder {current_id}: "
                    f"{format_box_api_error(exc)}"
                ) from exc

            for entry in page.entries or []:
                name = getattr(entry, "name", None)
                if not isinstance(name, str):
                    raise ScanError(f"Box item {getattr(entry, 'id', '?')} has no name")
                parts = (*parent_parts, name)
                relative_path = normalize_relative_path(parts)
                original_path = "/".join(parts)
                if is_excluded_path(relative_path, excluded_paths, excluded_names):
                    continue
                ensure_unique_path(seen, relative_path, original_path)
                item_type = _enum_value(getattr(entry, "type", "other"))
                items.append(_box_item(entry, relative_path, forced_type=item_type))
                if item_type == "folder":
                    pending.append((str(entry.id), parts))

            marker = getattr(page, "next_marker", None)
            if not marker:
                break

    return sorted(items, key=lambda item: item.relative_path)


def _box_item(item: Any, relative_path: str, *, forced_type: str) -> InventoryItem:
    file_version = getattr(item, "file_version", None)
    return InventoryItem(
        relative_path=relative_path,
        item_type=forced_type,
        size=_optional_int(getattr(item, "size", None)),
        modified_at=_timestamp(getattr(item, "modified_at", None)),
        content_id=_optional_string(getattr(item, "id", None)),
        version_id=_optional_string(getattr(file_version, "id", None)),
        etag=_optional_string(getattr(item, "etag", None)),
        # The Box API field is named "sha1", while SDK 10.x exposes it as
        # ``sha_1`` on generated file models.
        sha1=_optional_string(getattr(item, "sha_1", None)),
        sequence_id=_optional_string(getattr(item, "sequence_id", None)),
    )


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _optional_string(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)
