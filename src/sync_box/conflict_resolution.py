"""Shared, resumable conflict-resolution planning and execution."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from enum import StrEnum
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any

from sync_box.box_errors import format_box_api_error, is_expired_content_token_error
from sync_box.database import (
    begin_resolution_run,
    load_baseline,
    load_incomplete_resolution,
    load_resolution_by_key,
    mark_resolution_operation,
    replace_baseline_and_finish_resolution,
)
from sync_box.inventory import InventoryItem, ScanError, normalize_relative_path
from sync_box.sync_engine import (
    BoxMutations,
    _download_atomic,
    _fsync_directory,
    _open_verified,
    _parent_box_id,
    _path,
    _safe_parent,
)
from sync_box.two_way import SyncAction, build_sync_plan, validate_baseline_match


class ConflictResolutionPolicy(StrEnum):
    """Policies understood by the shared engine."""

    KEEP_BOX_AT_ORIGINAL = "keep_box_at_original"


@dataclass(frozen=True, slots=True)
class ConflictResolutionPlan:
    """Immutable preconditions and destinations for one conflict resolution."""

    policy: ConflictResolutionPolicy
    expected_baseline_generation: int
    original_path: str
    conflict_copy_path: str
    baseline_local_sha1: str
    baseline_box_version_id: str
    local_size: int
    local_sha1: str
    local_device: int | None
    local_inode: int | None
    local_mtime_ns: int | None
    box_item_id: str
    box_version_id: str
    box_etag: str
    box_size: int
    box_sha1: str
    reason: str

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["policy"] = self.policy.value
        result["resolution_key"] = self.resolution_key
        return result

    @property
    def resolution_key(self) -> str:
        payload = asdict(self)
        payload["policy"] = self.policy.value
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ConflictResolutionResult:
    """Structured execution state for CLI and GUI presentation."""

    resolution_run_id: int
    resolution_key: str
    status: str
    policy: ConflictResolutionPolicy
    original_path: str
    conflict_copy_path: str
    completed_steps: tuple[str, ...]
    baseline_generation_before: int
    baseline_generation_after: int | None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["policy"] = self.policy.value
        return result


class ConflictResolutionError(RuntimeError):
    """A resolution was refused or remains safely incomplete."""

    def __init__(
        self, message: str, *, result: ConflictResolutionResult | None = None
    ) -> None:
        super().__init__(message)
        self.result = result


InventoryProvider = Callable[[], tuple[list[InventoryItem], list[InventoryItem]]]
InterruptionHook = Callable[[str], None]

_UPLOAD_COPY = "upload_local_conflict_copy"
_PUBLISH_LOCAL_COPY = "publish_local_conflict_copy"
_DOWNLOAD_ORIGINAL = "download_box_original"
_VERIFY_AND_BASELINE = "verify_and_advance_baseline"
RESOLUTION_STEPS = (
    _UPLOAD_COPY,
    _PUBLISH_LOCAL_COPY,
    _DOWNLOAD_ORIGINAL,
    _VERIFY_AND_BASELINE,
)


def build_conflict_resolution_plan(
    *,
    policy: ConflictResolutionPolicy,
    expected_baseline_generation: int,
    baseline_generation: int,
    baseline_pairs: list[tuple[InventoryItem, InventoryItem]],
    local_items: list[InventoryItem],
    box_items: list[InventoryItem],
    conflict_path: str,
) -> ConflictResolutionPlan:
    """Build a side-effect-free keep-both plan from fresh inventories."""
    if policy is not ConflictResolutionPolicy.KEEP_BOX_AT_ORIGINAL:
        raise ConflictResolutionError(f"Unsupported conflict resolution policy: {policy}")
    if expected_baseline_generation != baseline_generation:
        raise ConflictResolutionError(
            "Baseline generation changed: expected "
            f"{expected_baseline_generation}, found {baseline_generation}"
        )
    _require_safe_relative_path(conflict_path)
    baseline = {left.relative_path: (left, right) for left, right in baseline_pairs}
    local = _unique(local_items, "local")
    remote = _unique(box_items, "Box")
    old = baseline.get(conflict_path)
    if old is None:
        raise ConflictResolutionError(
            f"Conflict path is absent from baseline generation {baseline_generation}"
        )
    old_local, old_box = old
    current_local = local.get(conflict_path)
    current_box = next(
        (item for item in remote.values() if item.content_id == old_box.content_id),
        None,
    )
    if current_local is None or current_box is None:
        raise ConflictResolutionError("Keep-both resolution requires both current files")
    if current_box.relative_path != conflict_path:
        raise ConflictResolutionError("Box item moved after the baseline")
    if current_local.item_type != "file" or current_box.item_type != "file":
        raise ConflictResolutionError("Keep-both resolution supports regular files only")

    matching_conflicts = [
        action
        for action in build_sync_plan(baseline_pairs, local_items, box_items)
        if action.relative_path == conflict_path
        and action.action == "conflict"
        and action.reason == "both sides changed since baseline"
    ]
    if len(matching_conflicts) != 1:
        raise ConflictResolutionError(
            "Path is not one divergent both-sides-changed conflict"
        )
    if not old_local.sha1 or not old_box.version_id:
        raise ConflictResolutionError("Baseline conflict fingerprints are incomplete")
    if current_local.size is None or not current_local.sha1:
        raise ConflictResolutionError("Current local size or SHA-1 is unavailable")
    if (
        not current_box.content_id
        or not current_box.version_id
        or current_box.etag is None
        or current_box.size is None
        or not current_box.sha1
    ):
        raise ConflictResolutionError("Current Box identity or fingerprint is incomplete")
    if current_local.sha1.lower() == current_box.sha1.lower():
        raise ConflictResolutionError("Current local and Box content is not divergent")

    conflict_copy_path = deterministic_conflict_copy_path(
        conflict_path, source="local", sha1=current_local.sha1
    )
    if conflict_copy_path in local:
        raise ConflictResolutionError(
            f"Conflict-copy destination already exists locally: {conflict_copy_path!r}"
        )
    if conflict_copy_path in remote:
        raise ConflictResolutionError(
            f"Conflict-copy destination already exists on Box: {conflict_copy_path!r}"
        )

    return ConflictResolutionPlan(
        policy=policy,
        expected_baseline_generation=expected_baseline_generation,
        original_path=conflict_path,
        conflict_copy_path=conflict_copy_path,
        baseline_local_sha1=old_local.sha1,
        baseline_box_version_id=old_box.version_id,
        local_size=current_local.size,
        local_sha1=current_local.sha1,
        local_device=current_local.device,
        local_inode=current_local.inode,
        local_mtime_ns=current_local.mtime_ns,
        box_item_id=current_box.content_id,
        box_version_id=current_box.version_id,
        box_etag=current_box.etag,
        box_size=current_box.size,
        box_sha1=current_box.sha1,
        reason=matching_conflicts[0].reason,
    )


def deterministic_conflict_copy_path(path: str, *, source: str, sha1: str) -> str:
    """Return a deterministic NFC path with a filename of at most 255 UTF-8 bytes."""
    _require_safe_relative_path(path)
    if source not in {"local", "Box"}:
        raise ConflictResolutionError("Conflict-copy source must be 'local' or 'Box'")
    if len(sha1) != 40 or any(character not in "0123456789abcdefABCDEF" for character in sha1):
        raise ConflictResolutionError("Conflict-copy SHA-1 is invalid")
    original = PurePosixPath(path)
    name = original.name
    suffix = PurePosixPath(name).suffix if not name.startswith(".") else ""
    stem = name[: -len(suffix)] if suffix else name
    marker = f" ({source} conflict {sha1.lower()[:12]})"
    reserved = len((marker + suffix).encode("utf-8"))
    if reserved >= 255:
        raise ConflictResolutionError("Conflict-copy marker is too long")
    stem = _truncate_utf8(stem, 255 - reserved)
    if not stem:
        stem = "file"
        if len((stem + marker + suffix).encode("utf-8")) > 255:
            raise ConflictResolutionError("Cannot construct a safe conflict-copy name")
    candidate_name = f"{stem}{marker}{suffix}"
    parent = original.parent
    candidate = candidate_name if str(parent) == "." else f"{parent.as_posix()}/{candidate_name}"
    _require_safe_relative_path(candidate)
    if candidate == path:
        raise ConflictResolutionError("Conflict-copy path equals the original path")
    return candidate


def execute_conflict_resolution(
    box: BoxMutations,
    local_root: Path,
    state_database: Path,
    plan: ConflictResolutionPlan,
    *,
    inventory_provider: InventoryProvider,
    refresh_box: Callable[[], BoxMutations] | None = None,
    interruption_hook: InterruptionHook | None = None,
) -> ConflictResolutionResult:
    """Execute or resume a journaled resolution and baseline only verified equality."""
    if plan.policy is not ConflictResolutionPolicy.KEEP_BOX_AT_ORIGINAL:
        raise ConflictResolutionError(f"Unsupported conflict resolution policy: {plan.policy}")
    root = local_root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise ConflictResolutionError(f"Unsafe local root: {root}")
    _path(root, plan.original_path)
    _path(root, plan.conflict_copy_path)
    plan_json = json.dumps(plan.to_dict(), sort_keys=True, separators=(",", ":"))

    existing = load_resolution_by_key(state_database, plan.resolution_key)
    incomplete = load_incomplete_resolution(state_database)
    if existing is not None and existing["outcome"] == "completed":
        return _result_from_run(plan, existing)
    if incomplete is not None and incomplete["resolution_key"] != plan.resolution_key:
        raise ConflictResolutionError(
            "Another conflict resolution is incomplete; resume it before starting a new one"
        )

    baseline = load_baseline(state_database)
    if baseline is None:
        raise ConflictResolutionError("No verified baseline exists")
    generation, baseline_local_root, baseline_box_root, pairs = baseline
    if baseline_local_root != str(root) or baseline_box_root != box.root_folder_id:
        raise ConflictResolutionError("Baseline roots do not match the resolution roots")

    if existing is None:
        if generation != plan.expected_baseline_generation:
            raise ConflictResolutionError(
                "Baseline generation changed: expected "
                f"{plan.expected_baseline_generation}, found {generation}"
            )
        local_items, box_items = inventory_provider()
        revalidated = build_conflict_resolution_plan(
            policy=plan.policy,
            expected_baseline_generation=plan.expected_baseline_generation,
            baseline_generation=generation,
            baseline_pairs=pairs,
            local_items=local_items,
            box_items=box_items,
            conflict_path=plan.original_path,
        )
        if revalidated.resolution_key != plan.resolution_key:
            raise ConflictResolutionError(
                "Conflict fingerprints or deterministic destination changed before execution"
            )
        try:
            run_id = begin_resolution_run(
                state_database,
                resolution_key=plan.resolution_key,
                baseline_generation=plan.expected_baseline_generation,
                policy=plan.policy.value,
                original_path=plan.original_path,
                conflict_copy_path=plan.conflict_copy_path,
                plan_json=plan_json,
                operations=RESOLUTION_STEPS,
            )
        except RuntimeError as exc:
            raise ConflictResolutionError(str(exc)) from exc
    else:
        if existing["plan_json"] != plan_json:
            raise ConflictResolutionError("Stored resolution plan does not match requested plan")
        run_id = int(existing["id"])

    try:
        return _resume_keep_box_resolution(
            box,
            root,
            state_database,
            run_id,
            plan,
            inventory_provider,
            refresh_box=refresh_box,
            interruption_hook=interruption_hook,
        )
    except ConflictResolutionError as exc:
        if exc.result is None:
            run = load_resolution_by_key(state_database, plan.resolution_key)
            if run is not None:
                exc.result = _result_from_run(plan, run)
        raise
    except Exception as exc:
        run = load_resolution_by_key(state_database, plan.resolution_key)
        result = _result_from_run(plan, run) if run is not None else None
        raise ConflictResolutionError(
            f"Conflict resolution remains incomplete: {type(exc).__name__}",
            result=result,
        ) from exc


def _resume_keep_box_resolution(
    box: BoxMutations,
    root: Path,
    state_database: Path,
    run_id: int,
    plan: ConflictResolutionPlan,
    inventory_provider: InventoryProvider,
    *,
    refresh_box: Callable[[], BoxMutations] | None,
    interruption_hook: InterruptionHook | None,
) -> ConflictResolutionResult:
    run = _required_run(state_database, plan)
    statuses = _operation_statuses(run)
    current_baseline = load_baseline(state_database)
    if current_baseline is None:
        raise ConflictResolutionError("Verified baseline disappeared during resolution")
    if current_baseline[0] != plan.expected_baseline_generation:
        raise ConflictResolutionError(
            "Baseline generation changed outside the incomplete resolution"
        )

    if statuses[_UPLOAD_COPY] != "completed":
        _execute_or_reconcile_upload_copy(
            box, root, state_database, run_id, plan, inventory_provider,
            status=statuses[_UPLOAD_COPY],
        )
        _after_step(_UPLOAD_COPY, interruption_hook)

    run = _required_run(state_database, plan)
    statuses = _operation_statuses(run)
    if statuses[_PUBLISH_LOCAL_COPY] != "completed":
        _execute_or_reconcile_local_copy(
            root, state_database, run_id, plan, inventory_provider,
            status=statuses[_PUBLISH_LOCAL_COPY],
        )
        _after_step(_PUBLISH_LOCAL_COPY, interruption_hook)

    run = _required_run(state_database, plan)
    statuses = _operation_statuses(run)
    if statuses[_DOWNLOAD_ORIGINAL] != "completed":
        box = _execute_or_reconcile_download(
            box, root, state_database, run_id, plan, inventory_provider,
            status=statuses[_DOWNLOAD_ORIGINAL], refresh_box=refresh_box,
        )
        _after_step(_DOWNLOAD_ORIGINAL, interruption_hook)

    run = _required_run(state_database, plan)
    statuses = _operation_statuses(run)
    if statuses[_VERIFY_AND_BASELINE] != "completed":
        local_items, box_items = inventory_provider()
        _verify_resolved_trees(plan, local_items, box_items)
        validate_baseline_match(local_items, box_items)
        baseline = load_baseline(state_database)
        if baseline is None or baseline[0] != plan.expected_baseline_generation:
            raise ConflictResolutionError("Baseline changed before resolution commit")
        mark_resolution_operation(
            state_database, run_id, _VERIFY_AND_BASELINE, "started"
        )
        replace_baseline_and_finish_resolution(
            state_database,
            run_id=run_id,
            final_operation=_VERIFY_AND_BASELINE,
            local_root=str(root),
            box_root_id=box.root_folder_id,
            local_items=local_items,
            box_items=box_items,
        )
        _after_step(_VERIFY_AND_BASELINE, interruption_hook)
    else:
        raise ConflictResolutionError(
            "Resolution journal is incomplete after its baseline step completed"
        )

    return _result_from_run(plan, _required_run(state_database, plan))


def _execute_or_reconcile_upload_copy(
    box: BoxMutations,
    root: Path,
    state_database: Path,
    run_id: int,
    plan: ConflictResolutionPlan,
    inventory_provider: InventoryProvider,
    *,
    status: str,
) -> None:
    local_items, box_items = inventory_provider()
    _verify_unresolved_originals(plan, local_items, box_items)
    local = _unique(local_items, "local")
    remote = _unique(box_items, "Box")
    existing = remote.get(plan.conflict_copy_path)
    if existing is not None:
        if status != "started":
            raise ConflictResolutionError(
                "Box conflict-copy destination appeared after planning"
            )
        _verify_box_conflict_copy(plan, existing)
        mark_resolution_operation(
            state_database,
            run_id,
            _UPLOAD_COPY,
            "completed",
            f"box_item_id={existing.content_id}",
        )
        return
    if plan.conflict_copy_path in local:
        raise ConflictResolutionError(
            "Local conflict-copy destination appeared before Box preservation"
        )
    if status == "started":
        raise ConflictResolutionError(
            "Box conflict-copy upload outcome is ambiguous and the destination "
            "is not visible; refusing automatic retry"
        )

    mark_resolution_operation(state_database, run_id, _UPLOAD_COPY, "started")
    _upload_conflict_copy(
        box, root, state_database, run_id, plan, box_items, inventory_provider
    )


def _upload_conflict_copy(
    box: BoxMutations,
    root: Path,
    state_database: Path,
    run_id: int,
    plan: ConflictResolutionPlan,
    box_items: list[InventoryItem],
    inventory_provider: InventoryProvider,
) -> None:
    source_path = _path(root, plan.original_path)
    parent_id = _parent_box_id(
        plan.conflict_copy_path,
        {item.relative_path: item for item in box_items},
        box.root_folder_id,
    )
    try:
        with _open_verified(source_path, plan.local_sha1) as source:
            box.upload_new(
                parent_id,
                PurePosixPath(plan.conflict_copy_path).name,
                source,
                plan.local_sha1,
            )
    except Exception as exc:
        _, refreshed_box = inventory_provider()
        candidate = _unique(refreshed_box, "Box").get(plan.conflict_copy_path)
        if candidate is not None:
            _verify_box_conflict_copy(plan, candidate)
            mark_resolution_operation(
                state_database,
                run_id,
                _UPLOAD_COPY,
                "completed",
                f"box_item_id={candidate.content_id}; reconciled_after_error",
            )
            return
        raise ConflictResolutionError(
            "Box conflict-copy upload outcome is ambiguous; upload was not retried: "
            f"{format_box_api_error(exc)}"
        ) from exc
    _, refreshed_box = inventory_provider()
    candidate = _unique(refreshed_box, "Box").get(plan.conflict_copy_path)
    if candidate is None:
        raise ConflictResolutionError(
            "Box conflict-copy upload returned but the new item could not be "
            "verified; upload was not retried"
        )
    _verify_box_conflict_copy(plan, candidate)
    mark_resolution_operation(
        state_database,
        run_id,
        _UPLOAD_COPY,
        "completed",
        f"box_item_id={candidate.content_id}",
    )


def _execute_or_reconcile_local_copy(
    root: Path,
    state_database: Path,
    run_id: int,
    plan: ConflictResolutionPlan,
    inventory_provider: InventoryProvider,
    *,
    status: str,
) -> None:
    local_items, box_items = inventory_provider()
    _verify_box_state_with_copy(plan, box_items)
    local = _unique(local_items, "local")
    destination = local.get(plan.conflict_copy_path)
    if destination is not None:
        if status != "started":
            raise ConflictResolutionError(
                "Local conflict-copy destination appeared after planning"
            )
        _verify_local_conflict_copy(plan, destination)
        mark_resolution_operation(
            state_database, run_id, _PUBLISH_LOCAL_COPY, "completed"
        )
        return
    _verify_local_original(plan, local.get(plan.original_path), allow_box_content=False)
    mark_resolution_operation(state_database, run_id, _PUBLISH_LOCAL_COPY, "started")
    _copy_local_no_clobber(
        root,
        plan.original_path,
        plan.conflict_copy_path,
        expected_size=plan.local_size,
        expected_sha1=plan.local_sha1,
    )
    _verify_file(
        _path(root, plan.conflict_copy_path), plan.local_size, plan.local_sha1
    )
    mark_resolution_operation(
        state_database, run_id, _PUBLISH_LOCAL_COPY, "completed"
    )


def _execute_or_reconcile_download(
    box: BoxMutations,
    root: Path,
    state_database: Path,
    run_id: int,
    plan: ConflictResolutionPlan,
    inventory_provider: InventoryProvider,
    *,
    status: str,
    refresh_box: Callable[[], BoxMutations] | None,
) -> BoxMutations:
    del status  # Exact-version downloads and atomic replacement are idempotent.
    local_items, box_items = inventory_provider()
    _verify_box_state_with_copy(plan, box_items)
    local = _unique(local_items, "local")
    _verify_local_conflict_copy(plan, local.get(plan.conflict_copy_path))
    original = local.get(plan.original_path)
    if _matches_file(original, plan.box_size, plan.box_sha1):
        mark_resolution_operation(
            state_database, run_id, _DOWNLOAD_ORIGINAL, "completed"
        )
        return box
    _verify_local_original(plan, original, allow_box_content=False)
    mark_resolution_operation(state_database, run_id, _DOWNLOAD_ORIGINAL, "started")
    action = SyncAction(
        action="download_version",
        relative_path=plan.original_path,
        item_type="file",
        box_item_id=plan.box_item_id,
        box_version_id=plan.box_version_id,
        box_etag=plan.box_etag,
        size=plan.box_size,
        sha1=plan.box_sha1,
        expected_local_sha1=plan.local_sha1,
        reason="keep Box at original during conflict resolution",
    )
    try:
        _download_atomic(box, root, action, replace=True)
    except Exception as exc:
        if not (refresh_box and is_expired_content_token_error(exc)):
            if isinstance(exc, ConflictResolutionError):
                raise
            raise ConflictResolutionError(
                f"Box original download failed: {format_box_api_error(exc)}"
            ) from exc
        box = refresh_box()
        try:
            _download_atomic(box, root, action, replace=True)
        except Exception as retry_exc:
            raise ConflictResolutionError(
                "Box original download failed after safe token refresh: "
                f"{format_box_api_error(retry_exc)}"
            ) from retry_exc
    _verify_file(_path(root, plan.original_path), plan.box_size, plan.box_sha1)
    mark_resolution_operation(
        state_database, run_id, _DOWNLOAD_ORIGINAL, "completed"
    )
    return box


def _verify_unresolved_originals(
    plan: ConflictResolutionPlan,
    local_items: list[InventoryItem],
    box_items: list[InventoryItem],
) -> None:
    local = _unique(local_items, "local")
    remote = _unique(box_items, "Box")
    _verify_local_original(plan, local.get(plan.original_path), allow_box_content=False)
    _verify_box_original(plan, remote)


def _verify_box_state_with_copy(
    plan: ConflictResolutionPlan, box_items: list[InventoryItem]
) -> None:
    remote = _unique(box_items, "Box")
    _verify_box_original(plan, remote)
    copy = remote.get(plan.conflict_copy_path)
    if copy is None:
        raise ConflictResolutionError("Verified Box conflict copy is missing")
    _verify_box_conflict_copy(plan, copy)


def _verify_box_original(
    plan: ConflictResolutionPlan, remote: dict[str, InventoryItem]
) -> None:
    item = remote.get(plan.original_path)
    if item is None:
        raise ConflictResolutionError("Box original is missing")
    if (
        item.item_type != "file"
        or item.content_id != plan.box_item_id
        or item.version_id != plan.box_version_id
        or item.etag != plan.box_etag
        or item.size != plan.box_size
        or not item.sha1
        or item.sha1.lower() != plan.box_sha1.lower()
    ):
        raise ConflictResolutionError("Box original identity or fingerprint changed")


def _verify_box_conflict_copy(
    plan: ConflictResolutionPlan, item: InventoryItem
) -> None:
    if (
        item.item_type != "file"
        or not item.content_id
        or not item.version_id
        or item.size != plan.local_size
        or not item.sha1
        or item.sha1.lower() != plan.local_sha1.lower()
    ):
        raise ConflictResolutionError("Box conflict copy failed size/SHA-1 verification")


def _verify_local_original(
    plan: ConflictResolutionPlan,
    item: InventoryItem | None,
    *,
    allow_box_content: bool,
) -> None:
    if _matches_file(item, plan.local_size, plan.local_sha1):
        if item is not None and any(
            (
                expected is not None
                and actual is not None
                and actual != expected
            )
            for actual, expected in (
                (item.device, plan.local_device),
                (item.inode, plan.local_inode),
                (item.mtime_ns, plan.local_mtime_ns),
            )
        ):
            raise ConflictResolutionError("Local file identity changed")
        return
    if allow_box_content and _matches_file(item, plan.box_size, plan.box_sha1):
        return
    raise ConflictResolutionError("Local original fingerprint changed")


def _verify_local_conflict_copy(
    plan: ConflictResolutionPlan, item: InventoryItem | None
) -> None:
    if not _matches_file(item, plan.local_size, plan.local_sha1):
        raise ConflictResolutionError("Local conflict copy failed size/SHA-1 verification")


def _verify_resolved_trees(
    plan: ConflictResolutionPlan,
    local_items: list[InventoryItem],
    box_items: list[InventoryItem],
) -> None:
    local = _unique(local_items, "local")
    remote = _unique(box_items, "Box")
    if not _matches_file(local.get(plan.original_path), plan.box_size, plan.box_sha1):
        raise ConflictResolutionError("Resolved local original does not match Box")
    if not _matches_file(remote.get(plan.original_path), plan.box_size, plan.box_sha1):
        raise ConflictResolutionError("Resolved Box original changed")
    if not _matches_file(local.get(plan.conflict_copy_path), plan.local_size, plan.local_sha1):
        raise ConflictResolutionError("Resolved local conflict copy is invalid")
    if not _matches_file(remote.get(plan.conflict_copy_path), plan.local_size, plan.local_sha1):
        raise ConflictResolutionError("Resolved Box conflict copy is invalid")
    _verify_box_original(plan, remote)
    _verify_box_conflict_copy(plan, remote[plan.conflict_copy_path])


def _copy_local_no_clobber(
    root: Path,
    source_relative: str,
    destination_relative: str,
    *,
    expected_size: int,
    expected_sha1: str,
) -> None:
    source = _path(root, source_relative)
    destination = _path(root, destination_relative)
    _safe_parent(root, source.parent, source_relative)
    _safe_parent(root, destination.parent, destination_relative)
    if destination.exists() or destination.is_symlink():
        raise ConflictResolutionError(
            f"Local conflict-copy destination exists: {destination_relative!r}"
        )
    temporary: Path | None = None
    try:
        descriptor, name = tempfile.mkstemp(
            prefix=".sync-box-conflict-copy-", dir=destination.parent
        )
        temporary = Path(name)
        digest = hashlib.sha1()
        count = 0
        with _open_verified(source, expected_sha1) as input_file, os.fdopen(
            descriptor, "wb"
        ) as output:
            while chunk := input_file.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
                count += len(chunk)
            output.flush()
            os.fsync(output.fileno())
        if count != expected_size or digest.hexdigest().lower() != expected_sha1.lower():
            raise ConflictResolutionError(
                "Local conflict-copy source changed during publication"
            )
        os.link(temporary, destination, follow_symlinks=False)
        temporary.unlink()
        temporary = None
        _fsync_directory(destination.parent)
    except FileExistsError as exc:
        raise ConflictResolutionError(
            f"Local conflict-copy destination exists: {destination_relative!r}"
        ) from exc
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _verify_file(path: Path, expected_size: int, expected_sha1: str) -> None:
    try:
        size = path.stat(follow_symlinks=False).st_size
    except OSError as exc:
        raise ConflictResolutionError(f"Cannot verify local file: {path.name!r}") from exc
    if path.is_symlink() or not path.is_file() or size != expected_size:
        raise ConflictResolutionError(f"Local file size/type verification failed: {path.name!r}")
    with _open_verified(path, expected_sha1):
        pass


def _matches_file(item: InventoryItem | None, size: int, sha1: str) -> bool:
    return bool(
        item
        and item.item_type == "file"
        and item.size == size
        and item.sha1
        and item.sha1.lower() == sha1.lower()
    )


def _required_run(state_database: Path, plan: ConflictResolutionPlan) -> dict[str, Any]:
    run = load_resolution_by_key(state_database, plan.resolution_key)
    if run is None:
        raise ConflictResolutionError("Resolution journal disappeared")
    return run


def _operation_statuses(run: dict[str, Any]) -> dict[str, str]:
    statuses = {entry["operation"]: entry["status"] for entry in run["operations"]}
    if set(statuses) != set(RESOLUTION_STEPS):
        raise ConflictResolutionError("Resolution journal has an invalid operation set")
    return statuses


def _result_from_run(
    plan: ConflictResolutionPlan, run: dict[str, Any]
) -> ConflictResolutionResult:
    completed = tuple(
        entry["operation"]
        for entry in run["operations"]
        if entry["status"] == "completed"
    )
    return ConflictResolutionResult(
        resolution_run_id=int(run["id"]),
        resolution_key=plan.resolution_key,
        status="completed" if run["outcome"] == "completed" else "incomplete",
        policy=plan.policy,
        original_path=plan.original_path,
        conflict_copy_path=plan.conflict_copy_path,
        completed_steps=completed,
        baseline_generation_before=plan.expected_baseline_generation,
        baseline_generation_after=(
            int(run["new_baseline_generation"])
            if run["new_baseline_generation"] is not None
            else None
        ),
    )


def _after_step(step: str, hook: InterruptionHook | None) -> None:
    if hook is not None:
        hook(step)


def _unique(items: list[InventoryItem], label: str) -> dict[str, InventoryItem]:
    result = {item.relative_path: item for item in items}
    if len(result) != len(items):
        raise ConflictResolutionError(f"Duplicate path in {label} inventory")
    return result


def _require_safe_relative_path(path: str) -> None:
    try:
        normalized = normalize_relative_path(tuple(path.split("/")))
    except ScanError as exc:
        raise ConflictResolutionError(str(exc)) from exc
    if normalized == "." or normalized != path:
        raise ConflictResolutionError(f"Unsafe resolution path: {path!r}")


def _truncate_utf8(value: str, maximum_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= maximum_bytes:
        return value
    shortened = encoded[:maximum_bytes]
    while shortened:
        try:
            return shortened.decode("utf-8")
        except UnicodeDecodeError:
            shortened = shortened[:-1]
    return ""
