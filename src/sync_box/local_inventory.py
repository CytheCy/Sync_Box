"""Read-only local filesystem inventory."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import stat

from sync_box.inventory import (
    InventoryItem,
    ScanError,
    ensure_unique_path,
    is_excluded_path,
    normalize_relative_path,
)


def scan_local(
    root: Path,
    *,
    hash_files: bool = False,
    excluded_paths: tuple[str, ...] = (),
    excluded_names: tuple[str, ...] = (),
) -> list[InventoryItem]:
    root = root.resolve(strict=False)
    if not root.is_dir():
        raise ScanError(f"Local synchronization root is not a directory: {root}")

    try:
        root_stat = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise ScanError(f"Cannot stat local synchronization root: {root}") from exc

    items = [_local_item(".", "folder", root_stat)]
    seen = {".": "."}
    pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]

    while pending:
        directory, parent_parts = pending.pop()
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise ScanError(f"Cannot read local directory: {directory}") from exc

        child_directories: list[tuple[Path, tuple[str, ...]]] = []
        for entry in entries:
            parts = (*parent_parts, entry.name)
            relative_path = normalize_relative_path(parts)
            original_path = "/".join(parts)
            if is_excluded_path(relative_path, excluded_paths, excluded_names):
                continue
            ensure_unique_path(seen, relative_path, original_path)
            try:
                item_stat = entry.stat(follow_symlinks=False)
                if entry.is_symlink():
                    item_type = "symlink"
                elif stat.S_ISDIR(item_stat.st_mode):
                    item_type = "folder"
                    child_directories.append((Path(entry.path), parts))
                elif stat.S_ISREG(item_stat.st_mode):
                    item_type = "file"
                else:
                    item_type = "other"
            except OSError as exc:
                raise ScanError(f"Cannot inspect local path: {entry.path}") from exc
            sha1 = (
                _sha1_file(Path(entry.path), item_stat)
                if hash_files and item_type == "file"
                else None
            )
            items.append(_local_item(relative_path, item_type, item_stat, sha1=sha1))
        pending.extend(reversed(child_directories))

    return sorted(items, key=lambda item: item.relative_path)


def _local_item(
    relative_path: str,
    item_type: str,
    item_stat: os.stat_result,
    *,
    sha1: str | None = None,
) -> InventoryItem:
    return InventoryItem(
        relative_path=relative_path,
        item_type=item_type,
        size=item_stat.st_size if item_type == "file" else None,
        modified_at=datetime.fromtimestamp(
            item_stat.st_mtime_ns / 1_000_000_000, tz=timezone.utc
        ).isoformat(),
        content_id=f"{item_stat.st_dev}:{item_stat.st_ino}",
        sha1=sha1,
        device=item_stat.st_dev,
        inode=item_stat.st_ino,
        mode=stat.S_IMODE(item_stat.st_mode),
        mtime_ns=item_stat.st_mtime_ns,
    )


def _sha1_file(path: Path, expected: os.stat_result) -> str:
    """Hash a stable regular file without following a final symlink."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as file_handle:
            before = os.fstat(file_handle.fileno())
            if not _same_file_version(expected, before) or not stat.S_ISREG(
                before.st_mode
            ):
                raise ScanError(f"Local file changed while opening it: {path}")
            digest = hashlib.file_digest(file_handle, "sha1").hexdigest()
            after = os.fstat(file_handle.fileno())
    except OSError as exc:
        raise ScanError(f"Cannot hash local file: {path}") from exc
    if not _same_file_version(before, after):
        raise ScanError(f"Local file changed while hashing it: {path}")
    return digest


def _same_file_version(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_size,
        left.st_mtime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_size,
        right.st_mtime_ns,
    )
