"""Safe, Box-to-local initial download execution."""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path
import stat
import tempfile
from collections.abc import Callable
from typing import Any, BinaryIO

from sync_box.box_errors import format_box_api_error, is_expired_content_token_error
from sync_box.inventory import ScanError, normalize_relative_path
from sync_box.planner import InitialDownloadItem


class InitialDownloadError(RuntimeError):
    """Raised when an initial download cannot continue safely."""

    def __init__(self, message: str, *, result: DownloadResult | None = None) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True, slots=True)
class DownloadResult:
    downloaded: int
    already_verified: int
    failed_conflicting: int
    folders_created: int
    folders_reused: int
    bytes: int
    verified_sha1: int


def execute_initial_download(
    client: Any,
    local_root: Path,
    plan: list[InitialDownloadItem],
    *,
    refresh_client: Callable[[], Any] | None = None,
) -> DownloadResult:
    """Execute a preflighted initial download without replacing local paths."""
    root = local_root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise InitialDownloadError(f"Local synchronization root is unsafe: {root}")

    folders, files = _preflight(plan)
    created_folders = 0
    downloaded_files = 0
    downloaded_bytes = 0
    verified_sha1 = 0
    already_verified = 0
    reused_folder_count = 0
    current_client = client

    try:
        (
            verified_sha1,
            already_verified,
            reused_folder_count,
        ) = _preflight_local_destinations(root, plan)

        for item in sorted(
            folders, key=lambda value: (_depth(value), value.relative_path)
        ):
            destination = _destination(root, item.relative_path)
            _require_safe_parent(root, destination.parent, item.relative_path)
            try:
                destination.mkdir()
            except FileExistsError as exc:
                raise InitialDownloadError(
                    "Local path already exists; refusing to overwrite: "
                    f"{item.relative_path}"
                ) from exc
            except OSError as exc:
                raise InitialDownloadError(
                    f"Cannot create local folder {item.relative_path!r}"
                ) from exc
            created_folders += 1

        for item in sorted(files, key=lambda value: value.relative_path):
            size, was_verified, current_client = _download_file(
                current_client,
                root,
                item,
                refresh_client=refresh_client,
            )
            downloaded_files += 1
            downloaded_bytes += size
            verified_sha1 += was_verified
    except InitialDownloadError as exc:
        if exc.result is None:
            exc.result = DownloadResult(
                downloaded=downloaded_files,
                already_verified=already_verified,
                failed_conflicting=1,
                folders_created=created_folders,
                folders_reused=reused_folder_count,
                bytes=downloaded_bytes,
                verified_sha1=verified_sha1,
            )
        raise

    return DownloadResult(
        downloaded=downloaded_files,
        already_verified=already_verified,
        failed_conflicting=0,
        folders_created=created_folders,
        folders_reused=reused_folder_count,
        bytes=downloaded_bytes,
        verified_sha1=verified_sha1,
    )


def _preflight(
    plan: list[InitialDownloadItem],
) -> tuple[list[InitialDownloadItem], list[InitialDownloadItem]]:
    folders: list[InitialDownloadItem] = []
    files: list[InitialDownloadItem] = []
    reused_folders: list[InitialDownloadItem] = []
    verified_files: list[InitialDownloadItem] = []
    actions: dict[str, str] = {}

    for item in plan:
        _validate_relative_path(item.relative_path)
        if item.relative_path in actions:
            raise InitialDownloadError(
                f"Duplicate initial-download path: {item.relative_path!r}"
            )
        actions[item.relative_path] = item.action
        if item.action == "create_folder":
            folders.append(item)
        elif item.action == "reuse_folder":
            reused_folders.append(item)
        elif item.action == "download_file":
            if not item.box_item_id:
                raise InitialDownloadError(
                    f"Box file has no item ID: {item.relative_path!r}"
                )
            files.append(item)
        elif item.action == "skip_verified_file":
            if not item.box_item_id:
                raise InitialDownloadError(
                    f"Box file has no item ID: {item.relative_path!r}"
                )
            if item.size is None and item.box_sha1 is None:
                raise InitialDownloadError(
                    f"Existing local file cannot be verified: {item.relative_path!r}"
                )
            verified_files.append(item)
        else:
            raise InitialDownloadError(
                f"Unsupported Box item prevents initial download: {item.relative_path!r}"
            )

    for item in (*folders, *files, *reused_folders, *verified_files):
        parent = Path(item.relative_path).parent.as_posix()
        if parent != "." and actions.get(parent) not in (
            "create_folder",
            "reuse_folder",
        ):
            raise InitialDownloadError(
                f"Box item has no planned parent folder: {item.relative_path!r}"
            )
    return folders, files


def _preflight_local_destinations(
    root: Path, plan: list[InitialDownloadItem]
) -> tuple[int, int, int]:
    """Reject current local conflicts before making the first local change."""
    sha1_verified = 0
    already_verified = 0
    folders_reused = 0
    for item in plan:
        try:
            destination = _destination(root, item.relative_path)
            if item.action in ("create_folder", "download_file") and (
                destination.exists() or destination.is_symlink()
            ):
                raise InitialDownloadError(
                    "Local path already exists; refusing to overwrite: "
                    f"{item.relative_path}"
                )
            if item.action == "reuse_folder":
                _require_existing_directory(root, destination, item.relative_path)
                folders_reused += 1
            elif item.action == "skip_verified_file":
                sha1_verified += _verify_existing_file(destination, item)
                already_verified += 1
        except InitialDownloadError as exc:
            if exc.result is None:
                exc.result = DownloadResult(
                    downloaded=0,
                    already_verified=already_verified,
                    failed_conflicting=1,
                    folders_created=0,
                    folders_reused=folders_reused,
                    bytes=0,
                    verified_sha1=sha1_verified,
                )
            raise
    return sha1_verified, already_verified, folders_reused


def _require_existing_directory(
    root: Path, destination: Path, relative_path: str
) -> None:
    try:
        item_stat = destination.lstat()
    except OSError as exc:
        raise InitialDownloadError(
            f"Expected local folder is unavailable: {relative_path!r}"
        ) from exc
    if not stat.S_ISDIR(item_stat.st_mode) or destination.is_symlink():
        raise InitialDownloadError(
            f"Expected local folder is unsafe: {relative_path!r}"
        )
    _require_safe_parent(root, destination, relative_path)


def _verify_existing_file(destination: Path, item: InitialDownloadItem) -> int:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(destination, flags)
        with os.fdopen(descriptor, "rb") as source:
            before = os.fstat(source.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise InitialDownloadError(
                    f"Expected local file is unsafe: {item.relative_path!r}"
                )
            if item.size is not None and before.st_size != item.size:
                raise InitialDownloadError(
                    f"Initial download conflict at {item.relative_path!r}: size differs "
                    f"(local {before.st_size}, Box {item.size})"
                )
            digest = (
                hashlib.file_digest(source, "sha1").hexdigest()
                if item.box_sha1 is not None
                else None
            )
            after = os.fstat(source.fileno())
    except InitialDownloadError:
        raise
    except OSError as exc:
        raise InitialDownloadError(
            f"Cannot verify existing local file {item.relative_path!r}"
        ) from exc
    if _file_identity(before) != _file_identity(after):
        raise InitialDownloadError(
            f"Existing local file changed during verification: {item.relative_path!r}"
        )
    if digest is not None and digest.lower() != item.box_sha1.lower():
        raise InitialDownloadError(
            f"Initial download conflict at {item.relative_path!r}: SHA-1 differs"
        )
    return int(digest is not None)


def _file_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _download_file(
    client: Any,
    root: Path,
    item: InitialDownloadItem,
    *,
    refresh_client: Callable[[], Any] | None,
) -> tuple[int, int, Any]:
    destination = _destination(root, item.relative_path)
    parent = destination.parent
    _require_safe_parent(root, parent, item.relative_path)
    if destination.exists() or destination.is_symlink():
        raise InitialDownloadError(
            f"Local path already exists; refusing to overwrite: {item.relative_path}"
        )

    temporary_path: Path | None = None
    stream: BinaryIO | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".sync-box-download-", dir=parent
        )
        temporary_path = Path(temporary_name)
        with os.fdopen(descriptor, "wb") as output:
            token_refreshes = 0
            while True:
                try:
                    stream = client.downloads.download_file(
                        item.box_item_id,
                        version=item.box_version_id,
                    )
                    break
                except Exception as exc:
                    if (
                        refresh_client is not None
                        and token_refreshes == 0
                        and is_expired_content_token_error(exc)
                    ):
                        token_refreshes += 1
                        try:
                            client = refresh_client()
                        except Exception:
                            raise InitialDownloadError(
                                "Box download authentication refresh failed for "
                                f"{item.relative_path!r}"
                            ) from None
                        continue
                    raise InitialDownloadError(
                        f"Box download failed for {item.relative_path!r}: "
                        f"{format_box_api_error(exc)}"
                    ) from exc
            if stream is None:
                raise InitialDownloadError(
                    f"Box download is not ready for {item.relative_path!r}"
                )
            digest, byte_count = _copy_and_hash(stream, output, item.relative_path)
            output.flush()
            os.fsync(output.fileno())

        if item.size is not None and byte_count != item.size:
            raise InitialDownloadError(
                f"Size verification failed for {item.relative_path!r}: "
                f"expected {item.size}, received {byte_count}"
            )
        if item.box_sha1 is not None and digest.lower() != item.box_sha1.lower():
            raise InitialDownloadError(
                f"SHA-1 verification failed for {item.relative_path!r}"
            )

        try:
            os.link(temporary_path, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise InitialDownloadError(
                f"Local path already exists; refusing to overwrite: {item.relative_path}"
            ) from exc
        except OSError as exc:
            raise InitialDownloadError(
                f"Cannot publish downloaded file {item.relative_path!r}"
            ) from exc
        temporary_path.unlink()
        temporary_path = None
        return byte_count, int(item.box_sha1 is not None), client
    finally:
        if stream is not None:
            with closing(stream):
                pass
        if temporary_path is not None:
            try:
                temporary_path.unlink()
            except FileNotFoundError:
                pass


def _copy_and_hash(
    source: BinaryIO, destination: BinaryIO, relative_path: str
) -> tuple[str, int]:
    digest = hashlib.sha1()
    byte_count = 0
    try:
        while chunk := source.read(1024 * 1024):
            if not isinstance(chunk, bytes):
                raise InitialDownloadError(
                    f"Box returned invalid content for {relative_path!r}"
                )
            destination.write(chunk)
            digest.update(chunk)
            byte_count += len(chunk)
    except InitialDownloadError:
        raise
    except Exception as exc:
        raise InitialDownloadError(
            f"Could not write downloaded file {relative_path!r}"
        ) from exc
    return digest.hexdigest(), byte_count


def _destination(root: Path, relative_path: str) -> Path:
    destination = root.joinpath(*relative_path.split("/"))
    if not destination.is_relative_to(root):
        raise InitialDownloadError(f"Unsafe local path: {relative_path!r}")
    return destination


def _validate_relative_path(relative_path: str) -> None:
    try:
        normalized = normalize_relative_path(tuple(relative_path.split("/")))
    except ScanError as exc:
        raise InitialDownloadError(str(exc)) from exc
    if normalized == "." or normalized != relative_path:
        raise InitialDownloadError(f"Unsafe initial-download path: {relative_path!r}")


def _require_safe_parent(root: Path, parent: Path, relative_path: str) -> None:
    try:
        resolved = parent.resolve(strict=True)
    except OSError as exc:
        raise InitialDownloadError(
            f"Local parent folder is unavailable for {relative_path!r}"
        ) from exc
    if resolved != parent or not resolved.is_relative_to(root):
        raise InitialDownloadError(
            f"Local parent folder is unsafe for {relative_path!r}"
        )


def _depth(item: InitialDownloadItem) -> int:
    return len(item.relative_path.split("/"))
