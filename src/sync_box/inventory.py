"""Shared, side-effect-free inventory data structures and rendering."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import PurePosixPath
import unicodedata


class ScanError(RuntimeError):
    """Raised when an inventory cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class InventoryItem:
    relative_path: str
    item_type: str
    size: int | None = None
    modified_at: str | None = None
    content_id: str | None = None
    version_id: str | None = None
    etag: str | None = None
    sha1: str | None = None
    sequence_id: str | None = None
    device: int | None = None
    inode: int | None = None
    mode: int | None = None
    mtime_ns: int | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_relative_path(parts: tuple[str, ...]) -> str:
    """Return a normalized relative POSIX path, rejecting ambiguous segments."""
    if not parts:
        return "."
    normalized: list[str] = []
    for part in parts:
        value = unicodedata.normalize("NFC", part)
        if value in ("", ".", "..") or "/" in value or "\x00" in value:
            raise ScanError(f"Unsafe path segment encountered: {part!r}")
        normalized.append(value)
    path = PurePosixPath(*normalized)
    if path.is_absolute() or ".." in path.parts:
        raise ScanError(f"Unsafe relative path: {path}")
    return path.as_posix()


def ensure_unique_path(seen: dict[str, str], normalized: str, original: str) -> None:
    previous = seen.get(normalized)
    if previous is not None:
        if previous == original:
            raise ScanError(f"Duplicate path encountered: {original!r}")
        raise ScanError(f"Path normalization collision: {previous!r}, {original!r}")
    seen[normalized] = original


def is_excluded_path(
    relative_path: str,
    excluded_paths: tuple[str, ...],
    excluded_names: tuple[str, ...] = (),
) -> bool:
    """Match configured subtrees or filenames at any directory depth."""
    excluded_by_path = any(
        relative_path == excluded
        or relative_path.startswith(f"{excluded}/")
        for excluded in excluded_paths
    )
    name = relative_path.rsplit("/", 1)[-1]
    return excluded_by_path or name in excluded_names


def summarize(items: list[InventoryItem]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(items)}
    for item in items:
        summary[item.item_type] = summary.get(item.item_type, 0) + 1
    return summary


def render_inventory(items: list[InventoryItem], *, as_json: bool) -> str:
    if as_json:
        return json.dumps([item.to_dict() for item in items], indent=2, sort_keys=True)
    lines = ["type\tsize\tmodified_at\tpath"]
    for item in items:
        safe_path = json.dumps(item.relative_path, ensure_ascii=True)
        lines.append(
            f"{item.item_type}\t{item.size if item.size is not None else '-'}\t"
            f"{item.modified_at or '-'}\t{safe_path}"
        )
    return "\n".join(lines)
