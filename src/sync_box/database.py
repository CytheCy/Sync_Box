"""SQLite state database setup. No sync operations live here yet."""

from __future__ import annotations

from contextlib import closing
from importlib.resources import files
import os
from pathlib import Path
import sqlite3
from typing import Iterable

from sync_box.inventory import InventoryItem


SCHEMA_VERSION = 2
MIGRATIONS = {
    1: "schema.sql",
    2: "migrations/0002_inventory.sql",
}


def initialize_database(path: Path) -> None:
    """Create an empty state database and validate its schema version."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.exists():
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
    path.chmod(0o600)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            current_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if current_version < 0 or current_version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"Unsupported database schema version {current_version}; "
                    f"expected {SCHEMA_VERSION}"
                )
            for version in range(current_version + 1, SCHEMA_VERSION + 1):
                resource = files("sync_box").joinpath(*MIGRATIONS[version].split("/"))
                connection.executescript(resource.read_text(encoding="utf-8"))


def save_inventory(
    path: Path,
    *,
    source: str,
    root_identifier: str,
    items: Iterable[InventoryItem],
) -> int:
    """Persist a completed metadata snapshot without touching either source."""
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            cursor = connection.execute(
                "INSERT INTO inventory_runs(source, root_identifier) VALUES (?, ?)",
                (source, root_identifier),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO inventory_items(
                    run_id, relative_path, item_type, size, modified_at, content_id,
                    version_id, etag, sha1, sequence_id, device, inode, mode, mtime_ns
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        run_id,
                        item.relative_path,
                        item.item_type,
                        item.size,
                        item.modified_at,
                        item.content_id,
                        item.version_id,
                        item.etag,
                        item.sha1,
                        item.sequence_id,
                        item.device,
                        item.inode,
                        item.mode,
                        item.mtime_ns,
                    )
                    for item in items
                ),
            )
    return run_id
