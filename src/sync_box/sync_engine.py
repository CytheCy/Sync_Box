"""Verified execution of a previously built two-way synchronization plan."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from collections.abc import Callable
from typing import Any
from types import SimpleNamespace

from sync_box.box_errors import format_box_api_error, is_expired_content_token_error
from sync_box.database import (
    begin_sync_run,
    finish_sync_run,
    load_incomplete_resolution,
    mark_operation,
)
from sync_box.inventory import ScanError, normalize_relative_path
from sync_box.two_way import SyncAction


class SyncExecutionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SyncResult:
    completed: int
    run_id: int


class BoxMutations:
    """Small SDK adapter that keeps generated Box types out of the engine."""

    def __init__(self, client: Any, root_folder_id: str) -> None:
        self.client = client
        self.root_folder_id = root_folder_id

    def upload_new(self, parent_id: str, name: str, source: Any, sha1: str) -> None:
        from box_sdk_gen.managers.uploads import UploadFileAttributes, UploadFileAttributesParentField
        attributes = UploadFileAttributes(name=name, parent=UploadFileAttributesParentField(id=parent_id))
        self.client.uploads.upload_file(attributes, source, file_file_name=name,
                                        content_md_5=sha1)

    def upload_version(self, file_id: str, name: str, source: Any, etag: str | None, sha1: str) -> None:
        from box_sdk_gen.managers.uploads import UploadFileVersionAttributes
        attributes = UploadFileVersionAttributes(name=name)
        self.client.uploads.upload_file_version(file_id, attributes, source,
                                                file_file_name=name, if_match=etag,
                                                content_md_5=sha1)

    def create_folder(self, parent_id: str, name: str) -> str:
        from box_sdk_gen.managers.folders import CreateFolderParent
        created = self.client.folders.create_folder(name, CreateFolderParent(id=parent_id))
        return str(created.id)

    def delete(self, item_type: str, item_id: str, etag: str | None) -> None:
        if item_type == "folder":
            self.client.folders.delete_folder_by_id(item_id, recursive=False, if_match=etag)
        else:
            self.client.files.delete_file_by_id(item_id, if_match=etag)

    def move(self, item_type: str, item_id: str, parent_id: str, name: str, etag: str | None) -> None:
        if item_type == "folder":
            from box_sdk_gen.managers.folders import UpdateFolderByIdParent
            self.client.folders.update_folder_by_id(item_id, name=name,
                                                     parent=UpdateFolderByIdParent(id=parent_id), if_match=etag)
        else:
            from box_sdk_gen.managers.files import UpdateFileByIdParent
            self.client.files.update_file_by_id(item_id, name=name,
                                                parent=UpdateFileByIdParent(id=parent_id), if_match=etag)

    def download(self, file_id: str, version_id: str | None) -> Any:
        return self.client.downloads.download_file(file_id, version=version_id)


def execute_sync(
    box: BoxMutations,
    local_root: Path,
    state_database: Path,
    plan: list[SyncAction],
    *,
    box_items_by_path: dict[str, Any],
    refresh_box: Callable[[], BoxMutations] | None = None,
) -> SyncResult:
    """Execute conflict-free actions; journal each verified completion."""
    incomplete = load_incomplete_resolution(state_database)
    if incomplete is not None:
        raise SyncExecutionError(
            "Synchronization refused: conflict resolution "
            f"{incomplete['id']} is incomplete"
        )
    conflicts = [item for item in plan if item.action == "conflict"]
    if conflicts:
        raise SyncExecutionError(
            f"Synchronization refused: {len(conflicts)} conflict(s) require review"
        )
    root = local_root.resolve(strict=True)
    if root.is_symlink() or not root.is_dir():
        raise SyncExecutionError(f"Unsafe local root: {root}")
    _preflight_paths(root, plan)
    run_id = begin_sync_run(state_database, plan)
    completed = 0
    current_box = box
    try:
        for action in plan:
            try:
                current_box = _execute_one(current_box, root, action, box_items_by_path,
                                           refresh_box=refresh_box)
            except Exception as exc:
                mark_operation(state_database, run_id, action.relative_path, action.action,
                               "failed", str(exc))
                raise
            mark_operation(state_database, run_id, action.relative_path, action.action, "completed")
            completed += 1
    except Exception as exc:
        finish_sync_run(state_database, run_id, "failed", f"completed={completed}")
        if isinstance(exc, SyncExecutionError):
            raise
        raise SyncExecutionError(f"Synchronization stopped: {exc}") from exc
    finish_sync_run(state_database, run_id, "completed", f"completed={completed}")
    return SyncResult(completed, run_id)


def _execute_one(box: BoxMutations, root: Path, action: SyncAction,
                 box_items: dict[str, Any], *, refresh_box: Callable[[], BoxMutations] | None) -> BoxMutations:
    path = _path(root, action.relative_path)
    if action.action in {"upload_new", "upload_version"}:
        _safe_parent(root, path.parent, action.relative_path)
        parent_id = _parent_box_id(action.relative_path, box_items, box.root_folder_id)
        with _open_verified(path, action.sha1) as source:
            try:
                if action.action == "upload_new":
                    box.upload_new(parent_id, path.name, source, action.sha1 or "")
                else:
                    box.upload_version(_required(action.box_item_id), path.name, source,
                                       action.box_etag, action.sha1 or "")
            except Exception as exc:
                if refresh_box and is_expired_content_token_error(exc):
                    box = _refresh_safely(refresh_box, action.relative_path)
                    source.seek(0)
                    try:
                        if action.action == "upload_new":
                            box.upload_new(parent_id, path.name, source, action.sha1 or "")
                        else:
                            box.upload_version(_required(action.box_item_id), path.name, source,
                                               action.box_etag, action.sha1 or "")
                    except Exception as retry_exc:
                        raise SyncExecutionError(
                            f"Box upload failed for {action.relative_path!r}: "
                            f"{format_box_api_error(retry_exc)}"
                        ) from retry_exc
                else:
                    raise SyncExecutionError(f"Box upload failed for {action.relative_path!r}: {format_box_api_error(exc)}") from exc
    elif action.action == "create_box_folder":
        parent_id = _parent_box_id(action.relative_path, box_items, box.root_folder_id)
        try:
            item_id = box.create_folder(parent_id, path.name)
        except Exception as exc:
            if not (refresh_box and is_expired_content_token_error(exc)):
                raise SyncExecutionError(f"Box folder creation failed for {action.relative_path!r}: {format_box_api_error(exc)}") from exc
            box = _refresh_safely(refresh_box, action.relative_path)
            try: item_id = box.create_folder(parent_id, path.name)
            except Exception as retry_exc: raise SyncExecutionError(f"Box folder creation failed for {action.relative_path!r}: {format_box_api_error(retry_exc)}") from retry_exc
        box_items[action.relative_path] = SimpleNamespace(content_id=item_id)
    elif action.action == "delete_box":
        try:
            box.delete(_required(action.item_type), _required(action.box_item_id), action.box_etag)
        except Exception as exc:
            if not (refresh_box and is_expired_content_token_error(exc)):
                raise SyncExecutionError(f"Box deletion failed for {action.relative_path!r}: {format_box_api_error(exc)}") from exc
            box = _refresh_safely(refresh_box, action.relative_path)
            try: box.delete(_required(action.item_type), _required(action.box_item_id), action.box_etag)
            except Exception as retry_exc: raise SyncExecutionError(f"Box deletion failed for {action.relative_path!r}: {format_box_api_error(retry_exc)}") from retry_exc
    elif action.action == "move_box":
        destination = _required(action.destination_path)
        parent_id = _parent_box_id(destination, box_items, box.root_folder_id)
        try:
            box.move(_required(action.item_type), _required(action.box_item_id), parent_id,
                     Path(destination).name, action.box_etag)
        except Exception as exc:
            if not (refresh_box and is_expired_content_token_error(exc)):
                raise SyncExecutionError(f"Box move failed for {action.relative_path!r}: {format_box_api_error(exc)}") from exc
            box = _refresh_safely(refresh_box, action.relative_path)
            try: box.move(_required(action.item_type), _required(action.box_item_id), parent_id, Path(destination).name, action.box_etag)
            except Exception as retry_exc: raise SyncExecutionError(f"Box move failed for {action.relative_path!r}: {format_box_api_error(retry_exc)}") from retry_exc
    elif action.action in {"download_new", "download_version"}:
        try:
            _download_atomic(box, root, action, replace=action.action == "download_version")
        except Exception as exc:
            if not (refresh_box and is_expired_content_token_error(exc)):
                if isinstance(exc, SyncExecutionError): raise
                raise SyncExecutionError(f"Box download failed for {action.relative_path!r}: {format_box_api_error(exc)}") from exc
            box = _refresh_safely(refresh_box, action.relative_path)
            try: _download_atomic(box, root, action, replace=action.action == "download_version")
            except Exception as retry_exc:
                if isinstance(retry_exc, SyncExecutionError): raise
                raise SyncExecutionError(f"Box download failed for {action.relative_path!r}: {format_box_api_error(retry_exc)}") from retry_exc
    elif action.action == "create_local_folder":
        _safe_parent(root, path.parent, action.relative_path)
        path.mkdir()
        _fsync_directory(path.parent)
    elif action.action == "delete_local":
        _safe_parent(root, path.parent, action.relative_path)
        _verify_local(path, action.expected_local_sha1, action.item_type)
        path.rmdir() if action.item_type == "folder" else path.unlink()
        _fsync_directory(path.parent)
    elif action.action == "move_local":
        destination = _path(root, _required(action.destination_path))
        _safe_parent(root, path.parent, action.relative_path)
        _verify_local(path, action.expected_local_sha1, action.item_type)
        _safe_parent(root, destination.parent, action.destination_path or "")
        if destination.exists() or destination.is_symlink():
            raise SyncExecutionError(f"Move destination exists: {action.destination_path!r}")
        os.rename(path, destination)
        _fsync_directory(destination.parent)
    else:
        raise SyncExecutionError(f"Unsupported sync action: {action.action}")
    return box


def _download_atomic(box: BoxMutations, root: Path, action: SyncAction, *, replace: bool) -> None:
    destination = _path(root, action.relative_path)
    _safe_parent(root, destination.parent, action.relative_path)
    if replace:
        _verify_local(destination, action.expected_local_sha1, "file")
    elif destination.exists() or destination.is_symlink():
        raise SyncExecutionError(f"Download destination exists: {action.relative_path!r}")
    temporary: Path | None = None
    stream = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".sync-box-download-", dir=destination.parent)
        temporary = Path(name)
        digest = hashlib.sha1()
        count = 0
        with os.fdopen(descriptor, "wb") as output:
            stream = box.download(_required(action.box_item_id), action.box_version_id)
            while chunk := stream.read(1024 * 1024):
                output.write(chunk); digest.update(chunk); count += len(chunk)
            output.flush(); os.fsync(output.fileno())
        if action.size is not None and count != action.size:
            raise SyncExecutionError(f"Downloaded size mismatch for {action.relative_path!r}")
        if action.sha1 and digest.hexdigest().lower() != action.sha1.lower():
            raise SyncExecutionError(f"Downloaded SHA-1 mismatch for {action.relative_path!r}")
        if replace:
            _verify_local(destination, action.expected_local_sha1, "file")
            os.replace(temporary, destination)
        else:
            os.link(temporary, destination, follow_symlinks=False)
            temporary.unlink()
        temporary = None
        _fsync_directory(destination.parent)
    finally:
        if stream is not None:
            with closing(stream): pass
        if temporary is not None:
            try: temporary.unlink()
            except FileNotFoundError: pass


def _preflight_paths(root: Path, plan: list[SyncAction]) -> None:
    for action in plan:
        _path(root, action.relative_path)
        if action.destination_path:
            _path(root, action.destination_path)


def _path(root: Path, relative: str) -> Path:
    try: normalized = normalize_relative_path(tuple(relative.split("/")))
    except ScanError as exc: raise SyncExecutionError(str(exc)) from exc
    if normalized in {"."} or normalized != relative:
        raise SyncExecutionError(f"Unsafe synchronization path: {relative!r}")
    path = root.joinpath(*relative.split("/"))
    if not path.is_relative_to(root):
        raise SyncExecutionError(f"Path escapes local root: {relative!r}")
    return path


def _safe_parent(root: Path, parent: Path, relative: str) -> None:
    try: resolved = parent.resolve(strict=True)
    except OSError as exc: raise SyncExecutionError(f"Missing parent for {relative!r}") from exc
    if resolved != parent or not resolved.is_relative_to(root):
        raise SyncExecutionError(f"Unsafe parent for {relative!r}")


def _verify_local(path: Path, sha1: str | None, item_type: str | None) -> None:
    try: value = path.lstat()
    except OSError as exc: raise SyncExecutionError(f"Local item unavailable: {path.name!r}") from exc
    if item_type == "folder":
        if not stat.S_ISDIR(value.st_mode) or path.is_symlink():
            raise SyncExecutionError(f"Unsafe local folder: {path.name!r}")
    else:
        with _open_verified(path, sha1): pass


class _open_verified:
    def __init__(self, path: Path, sha1: str | None) -> None: self.path, self.sha1, self.file = path, sha1, None
    def __enter__(self) -> Any:
        try: descriptor = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError as exc: raise SyncExecutionError(f"Cannot safely open {self.path.name!r}") from exc
        self.file = os.fdopen(descriptor, "rb")
        before = os.fstat(self.file.fileno())
        if not stat.S_ISREG(before.st_mode): self.file.close(); raise SyncExecutionError("Local item is not a regular file")
        digest = hashlib.file_digest(self.file, "sha1").hexdigest(); after = os.fstat(self.file.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            self.file.close(); raise SyncExecutionError("Local file changed during verification")
        if self.sha1 and digest.lower() != self.sha1.lower():
            self.file.close(); raise SyncExecutionError("Local file no longer matches the plan")
        self.file.seek(0); return self.file
    def __exit__(self, *args: object) -> None:
        if self.file: self.file.close()


def _parent_box_id(path: str, items: dict[str, Any], root_id: str) -> str:
    parent = Path(path).parent.as_posix()
    if parent == ".": return root_id
    item = items.get(parent)
    value = getattr(item, "content_id", None)
    if not value: raise SyncExecutionError(f"Box parent ID unavailable for {path!r}")
    return str(value)


def _required(value: str | None) -> str:
    if not value: raise SyncExecutionError("Required operation identity is unavailable")
    return value


def _refresh_safely(refresh: Callable[[], BoxMutations], path: str) -> BoxMutations:
    try:
        return refresh()
    except Exception:
        raise SyncExecutionError(f"Box authentication refresh failed for {path!r}") from None


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
