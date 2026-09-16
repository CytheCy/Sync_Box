"""Pure, read-only comparison planning for local and Box inventories."""

from __future__ import annotations

from dataclasses import dataclass
import json

from sync_box.inventory import InventoryItem, ScanError


@dataclass(frozen=True, slots=True)
class PlanItem:
    relative_path: str
    status: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "relative_path": self.relative_path,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class InitialDownloadItem:
    relative_path: str
    action: str
    size: int | None
    box_item_id: str | None
    box_version_id: str | None
    box_sha1: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "action": self.action,
            "size": self.size,
            "box_item_id": self.box_item_id,
            "box_version_id": self.box_version_id,
            "box_sha1": self.box_sha1,
        }


def build_comparison_plan(
    local_items: list[InventoryItem], box_items: list[InventoryItem]
) -> list[PlanItem]:
    """Compare two complete inventories without choosing sync directions."""
    local = {item.relative_path: item for item in local_items}
    remote = {item.relative_path: item for item in box_items}
    plan: list[PlanItem] = []

    for path in sorted(set(local) | set(remote)):
        if path == ".":
            continue
        local_item = local.get(path)
        box_item = remote.get(path)

        if local_item is None:
            plan.append(
                PlanItem(
                    path,
                    "box_only",
                    "present only on Box; direction requires a baseline",
                )
            )
            continue
        if box_item is None:
            status = (
                "unsupported"
                if local_item.item_type not in ("file", "folder")
                else "local_only"
            )
            reason = (
                f"local {local_item.item_type} is not syncable"
                if status == "unsupported"
                else "present only locally; direction requires a baseline"
            )
            plan.append(PlanItem(path, status, reason))
            continue
        if local_item.item_type not in ("file", "folder") or box_item.item_type not in (
            "file",
            "folder",
        ):
            plan.append(
                PlanItem(
                    path,
                    "unsupported",
                    f"unsupported types: local={local_item.item_type}, box={box_item.item_type}",
                )
            )
            continue
        if local_item.item_type != box_item.item_type:
            plan.append(
                PlanItem(
                    path,
                    "type_mismatch",
                    f"local is {local_item.item_type}; Box is {box_item.item_type}",
                )
            )
            continue
        if local_item.item_type == "folder":
            plan.append(PlanItem(path, "same", "folder exists on both sides"))
            continue
        if local_item.sha1 is None or box_item.sha1 is None:
            plan.append(
                PlanItem(path, "unknown", "content hash is unavailable on one side")
            )
        elif local_item.sha1.lower() == box_item.sha1.lower():
            plan.append(PlanItem(path, "same", "file content hashes match"))
        else:
            plan.append(
                PlanItem(
                    path,
                    "content_mismatch",
                    "file content hashes differ; direction requires a baseline",
                )
            )

    return plan


def summarize_plan(plan: list[PlanItem]) -> dict[str, int]:
    summary: dict[str, int] = {"total": len(plan)}
    for item in plan:
        summary[item.status] = summary.get(item.status, 0) + 1
    summary["review"] = sum(item.status != "same" for item in plan)
    return summary


def build_initial_download_plan(
    local_items: list[InventoryItem], box_items: list[InventoryItem]
) -> list[InitialDownloadItem]:
    """Plan a new or safely resumed Box-to-local initial population."""
    local = {item.relative_path: item for item in local_items}
    remote = {item.relative_path: item for item in box_items}
    if len(local) != len(local_items) or len(remote) != len(box_items):
        raise ScanError("Duplicate path in initial-download inventory")

    for path, local_item in local.items():
        if path == ".":
            continue
        box_item = remote.get(path)
        if box_item is None:
            raise ScanError(
                f"Initial download conflict: unexpected local path {path!r}"
            )
        if local_item.item_type != box_item.item_type:
            raise ScanError(
                f"Initial download conflict at {path!r}: local is "
                f"{local_item.item_type}, Box is {box_item.item_type}"
            )
        if local_item.item_type == "folder":
            continue
        if local_item.item_type != "file":
            raise ScanError(
                f"Initial download conflict at {path!r}: unsupported local "
                f"{local_item.item_type}"
            )
        _verify_inventory_file(local_item, box_item)

    plan: list[InitialDownloadItem] = []
    for item in box_items:
        if item.relative_path == ".":
            continue
        local_item = local.get(item.relative_path)
        if item.item_type == "file":
            action = "skip_verified_file" if local_item else "download_file"
        elif item.item_type == "folder":
            action = "reuse_folder" if local_item else "create_folder"
        else:
            action = "skip_unsupported"
        plan.append(
            InitialDownloadItem(
                relative_path=item.relative_path,
                action=action,
                size=item.size,
                box_item_id=item.content_id,
                box_version_id=item.version_id,
                box_sha1=item.sha1,
            )
        )
    return sorted(plan, key=lambda item: item.relative_path)


def summarize_initial_download(plan: list[InitialDownloadItem]) -> dict[str, int]:
    files = [item for item in plan if item.action == "download_file"]
    return {
        "total": len(plan),
        "files": len(files),
        "folders": sum(item.action == "create_folder" for item in plan),
        "already_verified": sum(
            item.action == "skip_verified_file" for item in plan
        ),
        "reused_folders": sum(item.action == "reuse_folder" for item in plan),
        "bytes": sum(item.size or 0 for item in files),
        "unknown_size_files": sum(item.size is None for item in files),
        "unsupported": sum(item.action == "skip_unsupported" for item in plan),
    }


def _verify_inventory_file(local: InventoryItem, box: InventoryItem) -> None:
    if box.size is None and box.sha1 is None:
        raise ScanError(
            f"Initial download conflict at {local.relative_path!r}: Box provides "
            "neither size nor SHA-1, so the local file cannot be verified"
        )
    if box.size is not None and local.size != box.size:
        raise ScanError(
            f"Initial download conflict at {local.relative_path!r}: size differs "
            f"(local {local.size}, Box {box.size})"
        )
    if box.sha1 is not None:
        if local.sha1 is None:
            raise ScanError(
                f"Initial download conflict at {local.relative_path!r}: "
                "local SHA-1 was not computed"
            )
        if local.sha1.lower() != box.sha1.lower():
            raise ScanError(
                f"Initial download conflict at {local.relative_path!r}: SHA-1 differs"
            )


def render_plan(
    plan: list[PlanItem], *, as_json: bool = False, limit: int | None = None
) -> str:
    visible = [item for item in plan if item.status != "same"]
    if limit is not None:
        visible = visible[:limit]
    if as_json:
        return json.dumps([item.to_dict() for item in visible], indent=2, sort_keys=True)
    lines = ["status\tpath\treason"]
    for item in visible:
        lines.append(
            f"{item.status}\t{json.dumps(item.relative_path, ensure_ascii=True)}\t{item.reason}"
        )
    return "\n".join(lines)


def render_initial_download_plan(
    plan: list[InitialDownloadItem],
    *,
    as_json: bool = False,
    limit: int | None = None,
) -> str:
    visible = plan if limit is None else plan[:limit]
    if as_json:
        return json.dumps([item.to_dict() for item in visible], indent=2, sort_keys=True)
    lines = ["action\tsize\tpath"]
    for item in visible:
        size = item.size if item.size is not None else "-"
        lines.append(
            f"{item.action}\t{size}\t"
            f"{json.dumps(item.relative_path, ensure_ascii=True)}"
        )
    return "\n".join(lines)
