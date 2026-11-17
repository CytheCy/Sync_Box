"""Resolve ordinary divergent edits by modification time, retaining both backups."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging

from sync_box.database import active_file_resolution
from sync_box.file_resolution import (
    FileResolutionError, execute_file_resolution, preview_file_conflict,
)

LOGGER = logging.getLogger("sync_box")


def _modified_second(value):
    # Box timestamps have second precision. Treat edits within that second as ties.
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc).replace(microsecond=0)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def newer_file_policy(action, local, remote):
    """Choose only between two divergent files at the same, unchanged path."""
    if (action.action != "conflict" or action.reason != "both sides changed since baseline"
            or local is None or remote is None
            or local.relative_path != action.relative_path
            or remote.relative_path != action.relative_path
            or local.item_type != "file" or remote.item_type != "file"
            or not local.sha1 or not remote.sha1 or local.sha1.lower() == remote.sha1.lower()):
        return None
    left, right = _modified_second(local.modified_at), _modified_second(remote.modified_at)
    if left is None or right is None or left == right:
        return None
    return "keep_local" if left > right else "keep_box"


def resolve_newer_conflicts(config, plan, local_items, box_items, *, inventories, box=None):
    """Use the backed-up, conditional resolution engine; resume only automatic choices."""
    if config.conflict_policy != "prefer_newest":
        return []
    local = {item.relative_path: item for item in local_items}
    remote = {item.relative_path: item for item in box_items}
    paths = [action.relative_path for action in plan
             if newer_file_policy(action, local.get(action.relative_path), remote.get(action.relative_path))]
    active = active_file_resolution(config.state_database)
    if active:
        data = json.loads(active["snapshot_json"])
        if data.get("automatic_policy") != active["policy"] or active["policy"] not in {"keep_local", "keep_box"}:
            raise FileResolutionError("An interrupted manual resolution must be resumed in Resolve Conflict")
    if not paths and not active:
        return []
    if box is None:
        from sync_box.box_auth import build_write_authenticated_client
        from sync_box.sync_engine import BoxMutations
        box = BoxMutations(build_write_authenticated_client(config), config.box_folder_id)
    resolved = []

    def execute(token, path, policy):
        result = execute_file_resolution(config, token, policy, inventories=inventories, box=box)
        result.update(path=path, policy=policy)
        resolved.append(result)
        LOGGER.info("Automatically resolved %r using %s; retained backups: %s",
                    path, policy, result["backup_directory"])

    if active:
        execute(active["token"], active["original_path"], active["policy"])
        paths = [path for path in paths if path != active["original_path"]]
    for path in paths:
        # Recompute the winner from the fresh inventory used to create the backups.
        preview = preview_file_conflict(config, path, inventories=inventories, box=box, automatic=True)
        if preview is not None:
            execute(preview["token"], path, preview["automatic_policy"])
    return resolved
