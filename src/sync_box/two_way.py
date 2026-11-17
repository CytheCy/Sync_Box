"""Baseline validation and conservative two-way synchronization planning."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from sync_box.inventory import InventoryItem, ScanError
from sync_box.config import folder_mode


SYNCABLE = {"file", "folder"}


@dataclass(frozen=True, slots=True)
class SyncAction:
    action: str
    relative_path: str
    destination_path: str | None = None
    item_type: str | None = None
    box_item_id: str | None = None
    box_version_id: str | None = None
    box_etag: str | None = None
    size: int | None = None
    sha1: str | None = None
    expected_local_sha1: str | None = None
    expected_local_device: int | None = None
    expected_local_inode: int | None = None
    local_precondition_path: str | None = None
    expected_local_subtree: tuple[tuple[object, ...], ...] | None = None
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def validate_baseline_match(
    local_items: list[InventoryItem], box_items: list[InventoryItem]
) -> None:
    """Require exact paths/types and verified file content before baselining."""
    local = _unique(local_items, "local")
    remote = _unique(box_items, "Box")
    if set(local) != set(remote):
        missing_local = sorted(set(remote) - set(local))
        missing_box = sorted(set(local) - set(remote))
        raise ScanError(
            "Baseline refused: inventory paths differ "
            f"(Box-only={len(missing_local)}, local-only={len(missing_box)})"
        )
    for path in sorted(local):
        left, right = local[path], remote[path]
        if left.item_type not in SYNCABLE or right.item_type not in SYNCABLE:
            raise ScanError(f"Baseline refused at {path!r}: unsupported item type")
        if left.item_type != right.item_type:
            raise ScanError(f"Baseline refused at {path!r}: item types differ")
        if left.item_type == "file":
            if left.sha1 is None or right.sha1 is None:
                raise ScanError(f"Baseline refused at {path!r}: SHA-1 unavailable")
            if left.sha1.lower() != right.sha1.lower():
                raise ScanError(f"Baseline refused at {path!r}: SHA-1 differs")
        if right.content_id is None:
            raise ScanError(f"Baseline refused at {path!r}: Box ID unavailable")


def build_sync_plan(
    baseline_pairs: list[tuple[InventoryItem, InventoryItem]],
    local_items: list[InventoryItem],
    box_items: list[InventoryItem],
    folder_modes: tuple[tuple[str, str], ...] = (),
) -> list[SyncAction]:
    """Build actions by comparing both fresh inventories with one baseline."""
    if folder_modes:
        def is_one_way(path: str) -> bool:
            return folder_mode(path, folder_modes) == "one-way"

        _unique(local_items, "local")
        _unique(box_items, "Box")
        remote_by_id = {item.content_id: item for item in box_items if item.content_id}
        if len(remote_by_id) != sum(bool(item.content_id) for item in box_items):
            raise ScanError("Duplicate Box item ID in inventory")
        boundary_conflicts = []
        blocked_paths = set()
        for old_local, old_box in baseline_pairs:
            current = remote_by_id.get(old_box.content_id)
            if current and is_one_way(old_local.relative_path) != is_one_way(current.relative_path):
                blocked_paths.update((old_local.relative_path, current.relative_path))
                boundary_conflicts.append(_conflict(old_local.relative_path, current,
                    "Box move crosses folders with different sync directions"))
        normal = build_sync_plan(
            [(left, right) for left, right in baseline_pairs if not is_one_way(left.relative_path)],
            [item for item in local_items if not is_one_way(item.relative_path)],
            [item for item in box_items if not is_one_way(item.relative_path)],
        )
        # A recursive operation on a parent must not bypass a child's direction.
        for index, action in enumerate(normal):
            if action.item_type == "folder" and action.action in {"delete_box", "delete_local", "move_box", "move_local"}:
                if any(mode == "one-way" and any(root.startswith(parent + "/")
                           for parent in (action.relative_path, action.destination_path) if parent)
                       for root, mode in folder_modes):
                    normal[index] = _conflict(action.relative_path, None,
                        "parent move/deletion crosses a one-way folder setting")
        actions = normal + _one_way_plan(local_items, box_items, folder_modes)
        actions = [action for action in actions if not any(
            action.relative_path == path or action.relative_path.startswith(path + "/")
            for path in blocked_paths)]
        return sorted(actions + boundary_conflicts, key=_sort_key)
    local = _unique(local_items, "local")
    remote = _unique(box_items, "Box")
    base = {left.relative_path: (left, right) for left, right in baseline_pairs}
    base_box_ids = {right.content_id: path for path, (_, right) in base.items() if right.content_id}
    now_box_ids = {item.content_id: item for item in remote.values() if item.content_id}
    if len(now_box_ids) != sum(bool(i.content_id) for i in remote.values()):
        raise ScanError("Duplicate Box item ID in inventory")

    actions: list[SyncAction] = []
    claimed_local: set[str] = set()
    claimed_box: set[str] = set()

    for old_path, (old_local, old_box) in sorted(base.items()):
        if old_path == ".":
            continue
        current_box = now_box_ids.get(old_box.content_id) if old_box.content_id else remote.get(old_path)
        box_path = current_box.relative_path if current_box else None
        current_local = local.get(old_path)
        local_path = old_path if current_local else None

        # Local inode identity is only a rename hint when globally unique and content is unchanged.
        if current_local is None and old_local.device is not None and old_local.inode is not None:
            candidates = [i for i in local.values() if (i.device, i.inode) == (old_local.device, old_local.inode)]
            if len(candidates) == 1:
                current_local, local_path = candidates[0], candidates[0].relative_path

        if current_local:
            claimed_local.add(current_local.relative_path)
        if current_box:
            claimed_box.add(current_box.relative_path)

        replacement = remote.get(old_path)
        if current_box is None and replacement is not None:
            claimed_box.add(old_path)
            actions.append(_conflict(old_path, replacement, "baseline Box item was replaced by a different item ID"))
            continue

        local_changed = current_local is not None and not _same_local(old_local, current_local)
        box_changed = current_box is not None and not _same_box(old_box, current_box)
        local_moved = local_path is not None and local_path != old_path
        box_moved = box_path is not None and box_path != old_path

        if current_local is None and current_box is None:
            continue
        if current_local is None:
            if box_changed or box_moved:
                actions.append(_conflict(old_path, old_box, "local deleted while Box changed"))
            else:
                actions.append(_action("delete_box", old_path, current_box, reason="post-baseline local deletion"))
            continue
        if current_box is None:
            if local_changed or local_moved:
                actions.append(_conflict(local_path or old_path, old_box, "Box deleted while local changed"))
            else:
                action = _action("delete_local", old_path, old_box, expected_local_sha1=old_local.sha1,
                                 reason="post-baseline Box deletion")
                actions.append(_bind_local_folder(action, current_local, local, old_path))
            continue
        if current_local.item_type != current_box.item_type:
            actions.append(_conflict(old_path, current_box, "item types differ"))
            continue
        if local_moved or box_moved:
            if local_moved and box_moved and local_path == box_path and not local_changed and not box_changed:
                continue
            if local_moved and not box_moved and not local_changed and not box_changed:
                if local_path in remote and local_path != old_path:
                    actions.append(_conflict(old_path, current_box, "local rename destination is occupied on Box"))
                else:
                    action = _action("move_box", old_path, current_box, destination=local_path,
                                     reason="unambiguous local rename/move")
                    actions.append(_bind_local_folder(action, current_local, local, local_path))
            elif box_moved and not local_moved and not local_changed and not box_changed:
                if box_path in local and box_path != old_path:
                    actions.append(_conflict(old_path, current_box, "Box rename destination is occupied locally"))
                else:
                    action = _action("move_local", old_path, current_box, destination=box_path,
                                     expected_local_sha1=old_local.sha1,
                                     reason="unambiguous Box rename/move")
                    actions.append(_bind_local_folder(action, current_local, local, old_path))
            else:
                actions.append(_conflict(old_path, current_box, "ambiguous or simultaneous rename/change"))
            continue
        if current_local.item_type == "folder":
            continue
        if local_changed and box_changed:
            if _same_content(current_local, current_box):
                continue
            actions.append(_conflict(old_path, current_box, "both sides changed since baseline"))
        elif local_changed:
            actions.append(_action("upload_version", old_path, current_box,
                                   size=current_local.size, sha1=current_local.sha1,
                                   reason="local changed; Box unchanged"))
        elif box_changed:
            actions.append(_action("download_version", old_path, current_box,
                                   expected_local_sha1=old_local.sha1,
                                   reason="Box changed; local unchanged"))

    new_local = {p: i for p, i in local.items() if p != "." and p not in claimed_local and p not in base}
    new_box = {p: i for p, i in remote.items() if p != "." and p not in claimed_box and p not in base_box_ids.values()}
    local_casefold = _casefold_paths(local)
    box_casefold = _casefold_paths(remote)
    for path in sorted(set(new_local) | set(new_box)):
        left, right = new_local.get(path), new_box.get(path)
        if left and right:
            if left.item_type == right.item_type and (left.item_type == "folder" or _same_content(left, right)):
                continue
            actions.append(_conflict(path, right, "different new items occupy the same path"))
        elif left:
            case_collision = _different_case_item(path, box_casefold)
            if case_collision is not None:
                actions.append(_conflict(
                    path,
                    case_collision,
                    "local name differs only by case from an existing Box item",
                ))
            elif left.item_type not in SYNCABLE:
                actions.append(_conflict(path, None, f"unsupported local {left.item_type}"))
            else:
                action = SyncAction("create_box_folder" if left.item_type == "folder" else "upload_new",
                                    path, item_type=left.item_type, size=left.size, sha1=left.sha1,
                                    reason="new local item")
                actions.append(_bind_local_folder(action, left, local, path))
        elif right:
            case_collision = _different_case_item(path, local_casefold)
            if case_collision is not None and case_collision.relative_path in new_local:
                # The local-side path already emitted the one conflict for this pair.
                continue
            if case_collision is not None:
                actions.append(_conflict(
                    path,
                    right,
                    "Box name differs only by case from an existing local item",
                ))
            elif right.item_type not in SYNCABLE:
                actions.append(_conflict(path, right, f"unsupported Box {right.item_type}"))
            else:
                actions.append(_action("create_local_folder" if right.item_type == "folder" else "download_new",
                                       path, right, reason="new Box item"))
    return sorted(_collapse_moves(actions), key=_sort_key)


def summarize_sync_plan(plan: list[SyncAction]) -> dict[str, int]:
    result = {
        "total": len(plan),
        "upload_new": 0,
        "upload_version": 0,
        "create_box_folder": 0,
        "download_new": 0,
        "download_version": 0,
        "create_local_folder": 0,
        "delete_box": 0,
        "delete_local": 0,
        "move_box": 0,
        "move_local": 0,
        "conflicts": 0,
    }
    for entry in plan:
        key = "conflicts" if entry.action == "conflict" else entry.action
        result[key] = result.get(key, 0) + 1
    return result


def render_sync_plan(plan: list[SyncAction], *, as_json: bool = False, limit: int | None = None) -> str:
    visible = plan if limit is None else plan[:limit]
    if as_json:
        return json.dumps([entry.to_dict() for entry in visible], indent=2, sort_keys=True)
    lines = ["action\tpath\tdestination\treason"]
    for entry in visible:
        lines.append(f"{entry.action}\t{json.dumps(entry.relative_path)}\t"
                     f"{json.dumps(entry.destination_path) if entry.destination_path else '-'}\t{entry.reason}")
    return "\n".join(lines)


def _unique(items: list[InventoryItem], label: str) -> dict[str, InventoryItem]:
    result = {item.relative_path: item for item in items}
    if len(result) != len(items):
        raise ScanError(f"Duplicate path in {label} inventory")
    return result


def _casefold_paths(
    items: dict[str, InventoryItem]
) -> dict[str, list[InventoryItem]]:
    result: dict[str, list[InventoryItem]] = {}
    for path, item in items.items():
        result.setdefault(path.casefold(), []).append(item)
    return result


def _different_case_item(
    path: str, folded: dict[str, list[InventoryItem]]
) -> InventoryItem | None:
    candidates = [
        item for item in folded.get(path.casefold(), [])
        if item.relative_path != path
    ]
    return sorted(candidates, key=lambda item: item.relative_path)[0] if candidates else None


def _same_content(left: InventoryItem, right: InventoryItem) -> bool:
    return bool(left.sha1 and right.sha1 and left.sha1.lower() == right.sha1.lower())


def _same_local(old: InventoryItem, new: InventoryItem) -> bool:
    if old.item_type != new.item_type:
        return False
    return old.item_type == "folder" or _same_content(old, new)


def _same_box(old: InventoryItem, new: InventoryItem) -> bool:
    if old.item_type != new.item_type:
        return False
    if old.item_type == "folder":
        return old.content_id == new.content_id
    if old.version_id and new.version_id:
        return old.version_id == new.version_id
    if old.sha1 and new.sha1:
        return old.sha1.lower() == new.sha1.lower()
    return old.etag == new.etag and old.size == new.size


def _action(action: str, path: str, box: InventoryItem | None, *, destination: str | None = None,
            size: int | None = None, sha1: str | None = None,
            expected_local_sha1: str | None = None, reason: str) -> SyncAction:
    return SyncAction(action, path, destination, box.item_type if box else None,
                      box.content_id if box else None, box.version_id if box else None,
                      box.etag if box else None, size if size is not None else (box.size if box else None),
                      sha1 if sha1 is not None else (box.sha1 if box else None), expected_local_sha1,
                      reason=reason)


def _bind_local_folder(
    action: SyncAction,
    item: InventoryItem | None,
    local: dict[str, InventoryItem],
    path: str | None,
) -> SyncAction:
    """Bind a folder action to the inventoried directory and complete subtree."""
    if item is None or item.item_type != "folder" or path is None:
        return action
    data = action.to_dict()
    data.update(
        expected_local_device=item.device,
        expected_local_inode=item.inode,
        local_precondition_path=path,
        expected_local_subtree=_inventory_subtree(local, path),
    )
    return SyncAction(**data)


def _inventory_subtree(
    local: dict[str, InventoryItem], root: str
) -> tuple[tuple[object, ...], ...]:
    prefix = root + "/"
    entries: list[tuple[object, ...]] = []
    for path, item in sorted(local.items()):
        if path != root and not path.startswith(prefix):
            continue
        relative = "." if path == root else path[len(prefix):]
        entries.append(
            (
                relative,
                item.item_type,
                item.device,
                item.inode,
                item.mode,
                item.mtime_ns,
                item.size,
                item.sha1,
            )
        )
    return tuple(sorted(entries, key=lambda entry: str(entry[0])))


def _conflict(path: str, box: InventoryItem | None, reason: str) -> SyncAction:
    return _action("conflict", path, box, reason=reason)


def _sort_key(action: SyncAction) -> tuple[int, int, str]:
    depth = action.relative_path.count("/")
    if action.action in {"create_box_folder", "create_local_folder"}:
        return (0, depth, action.relative_path)
    if action.action in {"delete_box", "delete_local"}:
        return (2, -depth, action.relative_path)
    return (1, depth, action.relative_path)


def _collapse_moves(actions: list[SyncAction]) -> list[SyncAction]:
    """A folder move carries all descendants, so omit redundant child moves."""
    folder_moves = [a for a in actions if a.action in {"move_box", "move_local"}
                    and a.item_type == "folder" and a.destination_path]
    result: list[SyncAction] = []
    for action in actions:
        redundant = False
        for parent in folder_moves:
            prefix = parent.relative_path + "/"
            if action is parent or action.action != parent.action or not action.relative_path.startswith(prefix):
                continue
            suffix = action.relative_path[len(prefix):]
            if action.destination_path == f"{parent.destination_path}/{suffix}":
                redundant = True
                break
        if not redundant:
            result.append(action)
    return result


def _one_way_plan(
    local_items: list[InventoryItem], box_items: list[InventoryItem],
    folder_modes: tuple[tuple[str, str], ...],
) -> list[SyncAction]:
    local = _unique(local_items, "local")
    remote = _unique(box_items, "Box")
    folded = _casefold_paths(remote)
    actions = []
    for path, left in sorted(local.items()):
        if path == "." or folder_mode(path, folder_modes) != "one-way":
            continue
        right = remote.get(path)
        collision = _different_case_item(path, folded)
        if left.item_type not in SYNCABLE:
            actions.append(_conflict(path, right, f"unsupported local {left.item_type}"))
        elif right is None and collision is not None:
            actions.append(_conflict(path, collision,
                "local name differs only by case from an existing Box item"))
        elif right is not None and left.item_type != right.item_type:
            actions.append(_conflict(path, right, "item types differ"))
        elif right is None:
            action = SyncAction("create_box_folder" if left.item_type == "folder" else "upload_new",
                path, item_type=left.item_type, size=left.size, sha1=left.sha1,
                reason="one-way: new local item")
            actions.append(_bind_local_folder(action, left, local, path))
        elif left.item_type == "file" and not _same_content(left, right):
            actions.append(_action("upload_version", path, right, size=left.size, sha1=left.sha1,
                                   reason="one-way: local version replaces Box"))
    return actions


def baseline_inventories(
    local_items: list[InventoryItem], box_items: list[InventoryItem],
    folder_modes: tuple[tuple[str, str], ...] = (),
) -> tuple[list[InventoryItem], list[InventoryItem]]:
    """Box-only items in upload-only folders are intentionally left unpaired."""
    local_paths = {item.relative_path for item in local_items}
    return local_items, [item for item in box_items
        if item.relative_path in local_paths or folder_mode(item.relative_path, folder_modes) != "one-way"]
