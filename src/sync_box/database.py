"""Transactional SQLite inventory, baseline, and operation state."""

from __future__ import annotations

from contextlib import closing
from importlib.resources import files
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Iterable

from sync_box.inventory import InventoryItem


SCHEMA_VERSION = 5
MIGRATIONS = {
    1: "schema.sql",
    2: "migrations/0002_inventory.sql",
    3: "migrations/0003_sync_state.sql",
    4: "migrations/0004_conflict_resolution.sql",
    5: "migrations/0005_sync_run_guard.sql",
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


def replace_baseline(
    path: Path,
    *,
    local_root: str,
    box_root_id: str,
    local_items: Iterable[InventoryItem],
    box_items: Iterable[InventoryItem],
    expected_generation: int | None = None,
    active_sync_run_id: int | None = None,
) -> int:
    """Atomically replace the successful-sync baseline with paired snapshots."""
    local = {item.relative_path: item for item in local_items}
    remote = {item.relative_path: item for item in box_items}
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            incomplete = connection.execute(
                "SELECT id FROM conflict_resolution_runs "
                "WHERE outcome='in_progress' LIMIT 1"
            ).fetchone()
            if incomplete is not None:
                raise RuntimeError(
                    "Cannot replace baseline while conflict resolution "
                    f"{incomplete[0]} is incomplete"
                )
            running_syncs = connection.execute(
                "SELECT id FROM sync_runs WHERE outcome='running'"
            ).fetchall()
            if any(int(row[0]) != active_sync_run_id for row in running_syncs):
                raise RuntimeError(
                    "Cannot replace baseline while another synchronization run is active"
                )
            current = connection.execute(
                "SELECT generation_id FROM current_baseline WHERE singleton=1"
            ).fetchone()
            if expected_generation is not None and (
                current is None or int(current[0]) != expected_generation
            ):
                found = "absent" if current is None else current[0]
                raise RuntimeError(
                    "Baseline generation changed before verified commit: "
                    f"expected {expected_generation}, found {found}"
                )
            if set(local) != set(remote):
                raise ValueError("Cannot baseline inventories with different paths")
            cursor = connection.execute(
                "INSERT INTO baseline_generations(local_root, box_root_id) VALUES (?, ?)",
                (local_root, box_root_id),
            )
            generation = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO baseline_items(
                    generation_id, relative_path, item_type,
                    local_size, local_mtime_ns, local_sha1, local_device, local_inode,
                    local_mode, box_item_id, box_version_id, box_etag, box_sha1,
                    box_sequence_id, box_size, box_modified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        generation, name, local[name].item_type,
                        local[name].size, local[name].mtime_ns, local[name].sha1,
                        local[name].device, local[name].inode, local[name].mode,
                        remote[name].content_id, remote[name].version_id,
                        remote[name].etag, remote[name].sha1,
                        remote[name].sequence_id, remote[name].size,
                        remote[name].modified_at,
                    )
                    for name in sorted(local)
                ),
            )
            connection.execute(
                "INSERT INTO current_baseline(singleton, generation_id) VALUES (1, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET generation_id=excluded.generation_id",
                (generation,),
            )
            connection.execute(
                "DELETE FROM baseline_generations WHERE id <> ?", (generation,)
            )
    return generation


def load_baseline(path: Path) -> tuple[int, str, str, list[tuple[InventoryItem, InventoryItem]]] | None:
    """Load the current complete paired baseline."""
    if not path.is_file():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            header = connection.execute(
                """SELECT g.id, g.local_root, g.box_root_id
                   FROM current_baseline c JOIN baseline_generations g
                   ON g.id=c.generation_id WHERE c.singleton=1"""
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if header is None:
            return None
        rows = connection.execute(
            "SELECT * FROM baseline_items WHERE generation_id=? ORDER BY relative_path",
            (header["id"],),
        ).fetchall()
    pairs: list[tuple[InventoryItem, InventoryItem]] = []
    for row in rows:
        common = {"relative_path": row["relative_path"], "item_type": row["item_type"]}
        pairs.append((
            InventoryItem(**common, size=row["local_size"], sha1=row["local_sha1"],
                          device=row["local_device"], inode=row["local_inode"],
                          mode=row["local_mode"], mtime_ns=row["local_mtime_ns"]),
            InventoryItem(**common, size=row["box_size"], modified_at=row["box_modified_at"],
                          content_id=row["box_item_id"], version_id=row["box_version_id"],
                          etag=row["box_etag"], sha1=row["box_sha1"],
                          sequence_id=row["box_sequence_id"]),
        ))
    return int(header["id"]), str(header["local_root"]), str(header["box_root_id"]), pairs


def begin_sync_run(
    path: Path,
    actions: Iterable[object],
    *,
    baseline_generation: int,
    local_root: str,
) -> int:
    """Durably record the exact operation list before mutations begin."""
    initialize_database(path)
    action_list = list(actions)
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            incomplete = connection.execute(
                "SELECT id FROM conflict_resolution_runs "
                "WHERE outcome='in_progress' LIMIT 1"
            ).fetchone()
            if incomplete is not None:
                raise RuntimeError(
                    f"Conflict resolution {incomplete[0]} is incomplete"
                )
            current = connection.execute(
                "SELECT generation_id FROM current_baseline WHERE singleton=1"
            ).fetchone()
            if current is None or int(current[0]) != baseline_generation:
                found = "absent" if current is None else current[0]
                raise RuntimeError(
                    "Baseline generation changed before synchronization: "
                    f"expected {baseline_generation}, found {found}"
                )
            # The process-level file lock proves no other executor is active.
            # Any surviving row is therefore an interrupted process, not a live peer.
            connection.execute(
                "UPDATE sync_runs SET finished_at=CURRENT_TIMESTAMP, outcome='failed', "
                "summary='interrupted before completion' WHERE outcome='running'"
            )
            plan_json = json.dumps(
                [getattr(action, "to_dict")() for action in action_list],
                sort_keys=True,
            )
            cursor = connection.execute(
                "INSERT INTO sync_runs(started_at, dry_run, outcome, baseline_generation, "
                "local_root, plan_json) VALUES (CURRENT_TIMESTAMP, 0, 'running', ?, ?, ?) ",
                (baseline_generation, local_root, plan_json),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                "INSERT INTO sync_operations(run_id, relative_path, action, status, detail, step_order) "
                "VALUES (?, ?, ?, 'pending', ?, ?)",
                ((run_id, getattr(a, "relative_path"), getattr(a, "action"),
                  json.dumps(getattr(a, "to_dict")(), sort_keys=True), index)
                 for index, a in enumerate(action_list, start=1)),
            )
    return run_id


def mark_operation(path: Path, run_id: int, relative_path: str, action: str, status: str, detail: str | None = None) -> None:
    if status not in {"completed", "failed"}:
        raise ValueError("Invalid operation status")
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                "UPDATE sync_operations SET status=?, detail=COALESCE(?, detail) WHERE run_id=? AND relative_path=? AND action=?",
                (status, detail, run_id, relative_path, action),
            )


def finish_sync_run(path: Path, run_id: int, outcome: str, summary: str) -> None:
    if outcome not in {"completed", "failed"}:
        raise ValueError("Invalid sync outcome")
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            connection.execute(
                "UPDATE sync_runs SET finished_at=CURRENT_TIMESTAMP, outcome=?, summary=? WHERE id=?",
                (outcome, summary, run_id),
            )


def load_incomplete_resolution(path: Path) -> dict[str, Any] | None:
    """Return the one durable incomplete conflict resolution, if present."""
    if not path.is_file():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            run = connection.execute(
                "SELECT * FROM conflict_resolution_runs "
                "WHERE outcome='in_progress' ORDER BY id LIMIT 1"
            ).fetchone()
            if run is None:
                return None
            operations = connection.execute(
                "SELECT step_order, operation, status, detail "
                "FROM conflict_resolution_operations "
                "WHERE resolution_run_id=? ORDER BY step_order",
                (run["id"],),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    result = dict(run)
    result["operations"] = [dict(row) for row in operations]
    return result


def load_resolution_by_key(path: Path, resolution_key: str) -> dict[str, Any] | None:
    """Load one resolution and its ordered operation journal read-only."""
    if not path.is_file():
        return None
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        try:
            run = connection.execute(
                "SELECT * FROM conflict_resolution_runs WHERE resolution_key=?",
                (resolution_key,),
            ).fetchone()
            if run is None:
                return None
            operations = connection.execute(
                "SELECT step_order, operation, status, detail "
                "FROM conflict_resolution_operations "
                "WHERE resolution_run_id=? ORDER BY step_order",
                (run["id"],),
            ).fetchall()
        except sqlite3.OperationalError:
            return None
    result = dict(run)
    result["operations"] = [dict(row) for row in operations]
    return result


def begin_resolution_run(
    path: Path,
    *,
    resolution_key: str,
    baseline_generation: int,
    policy: str,
    original_path: str,
    conflict_copy_path: str,
    plan_json: str,
    operations: Iterable[str],
) -> int:
    """Durably record a fully prevalidated resolution before its first mutation."""
    initialize_database(path)
    operation_names = tuple(operations)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id, resolution_key FROM conflict_resolution_runs "
                "WHERE outcome='in_progress'"
            ).fetchone()
            if existing is not None:
                if str(existing[1]) == resolution_key:
                    return int(existing[0])
                raise RuntimeError(
                    "Another conflict resolution is incomplete; resume it before "
                    "starting a new one"
                )
            running_sync = connection.execute(
                "SELECT id FROM sync_runs WHERE outcome='running' LIMIT 1"
            ).fetchone()
            if running_sync is not None:
                raise RuntimeError(
                    f"Synchronization run {running_sync[0]} is still in progress"
                )
            current_generation = connection.execute(
                "SELECT generation_id FROM current_baseline WHERE singleton=1"
            ).fetchone()
            if (
                current_generation is None
                or int(current_generation[0]) != baseline_generation
            ):
                found = "absent" if current_generation is None else current_generation[0]
                raise RuntimeError(
                    "Baseline generation changed while starting resolution: "
                    f"expected {baseline_generation}, found {found}"
                )
            cursor = connection.execute(
                """
                INSERT INTO conflict_resolution_runs(
                    resolution_key, baseline_generation, policy, original_path,
                    conflict_copy_path, plan_json, outcome
                ) VALUES (?, ?, ?, ?, ?, ?, 'in_progress')
                """,
                (
                    resolution_key,
                    baseline_generation,
                    policy,
                    original_path,
                    conflict_copy_path,
                    plan_json,
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO conflict_resolution_operations(
                    resolution_run_id, step_order, operation, status
                ) VALUES (?, ?, ?, 'pending')
                """,
                (
                    (run_id, index, operation)
                    for index, operation in enumerate(operation_names, start=1)
                ),
            )
    return run_id


def mark_resolution_operation(
    path: Path,
    run_id: int,
    operation: str,
    status: str,
    detail: str | None = None,
) -> None:
    """Durably advance one resolution step without losing prior detail."""
    if status not in {"started", "completed"}:
        raise ValueError("Invalid resolution operation status")
    expected_status = "pending" if status == "started" else "started"
    with closing(sqlite3.connect(path)) as connection:
        with connection:
            cursor = connection.execute(
                """
                UPDATE conflict_resolution_operations
                SET status=?, detail=COALESCE(?, detail)
                WHERE resolution_run_id=? AND operation=? AND status=?
                """,
                (status, detail, run_id, operation, expected_status),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Invalid resolution operation transition for {operation}: "
                    f"expected {expected_status}, requested {status}"
                )


def replace_baseline_and_finish_resolution(
    path: Path,
    *,
    run_id: int,
    final_operation: str,
    local_root: str,
    box_root_id: str,
    local_items: Iterable[InventoryItem],
    box_items: Iterable[InventoryItem],
) -> int:
    """Atomically commit a verified baseline and complete its resolution."""
    local = {item.relative_path: item for item in local_items}
    remote = {item.relative_path: item for item in box_items}
    if set(local) != set(remote):
        raise ValueError("Cannot baseline inventories with different paths")
    initialize_database(path)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with connection:
            connection.execute("BEGIN IMMEDIATE")
            run = connection.execute(
                "SELECT outcome, baseline_generation FROM conflict_resolution_runs WHERE id=?",
                (run_id,),
            ).fetchone()
            if run is None or run[0] != "in_progress":
                raise RuntimeError("Conflict resolution is not incomplete")
            current_generation = connection.execute(
                "SELECT generation_id FROM current_baseline WHERE singleton=1"
            ).fetchone()
            if (
                current_generation is None
                or int(current_generation[0]) != int(run[1])
            ):
                raise RuntimeError("Baseline changed before atomic resolution commit")
            incomplete_other_steps = connection.execute(
                """
                SELECT count(*) FROM conflict_resolution_operations
                WHERE resolution_run_id=? AND operation<>? AND status<>'completed'
                """,
                (run_id, final_operation),
            ).fetchone()[0]
            final_status = connection.execute(
                """
                SELECT status FROM conflict_resolution_operations
                WHERE resolution_run_id=? AND operation=?
                """,
                (run_id, final_operation),
            ).fetchone()
            if incomplete_other_steps or final_status is None or final_status[0] != "started":
                raise RuntimeError("Resolution operations are not ready for baseline commit")

            cursor = connection.execute(
                "INSERT INTO baseline_generations(local_root, box_root_id) VALUES (?, ?)",
                (local_root, box_root_id),
            )
            generation = int(cursor.lastrowid)
            connection.executemany(
                """
                INSERT INTO baseline_items(
                    generation_id, relative_path, item_type,
                    local_size, local_mtime_ns, local_sha1, local_device, local_inode,
                    local_mode, box_item_id, box_version_id, box_etag, box_sha1,
                    box_sequence_id, box_size, box_modified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        generation, name, local[name].item_type,
                        local[name].size, local[name].mtime_ns, local[name].sha1,
                        local[name].device, local[name].inode, local[name].mode,
                        remote[name].content_id, remote[name].version_id,
                        remote[name].etag, remote[name].sha1,
                        remote[name].sequence_id, remote[name].size,
                        remote[name].modified_at,
                    )
                    for name in sorted(local)
                ),
            )
            connection.execute(
                "INSERT INTO current_baseline(singleton, generation_id) VALUES (1, ?) "
                "ON CONFLICT(singleton) DO UPDATE SET generation_id=excluded.generation_id",
                (generation,),
            )
            connection.execute(
                "DELETE FROM baseline_generations WHERE id <> ?", (generation,)
            )
            connection.execute(
                """
                UPDATE conflict_resolution_operations
                SET status='completed', detail=?
                WHERE resolution_run_id=? AND operation=?
                """,
                (f"baseline_generation={generation}", run_id, final_operation),
            )
            connection.execute(
                """
                UPDATE conflict_resolution_runs
                SET outcome='completed', finished_at=CURRENT_TIMESTAMP,
                    new_baseline_generation=?
                WHERE id=?
                """,
                (generation, run_id),
            )
    return generation
