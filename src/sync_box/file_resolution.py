"""Previewed, backed-up, resumable resolution of individual file conflicts."""
from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import difflib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import uuid

from sync_box.config import AppConfig
from sync_box.database import (
    active_file_resolution, initialize_database, load_baseline, load_incomplete_resolution,
    record_upload_receipt, upload_recovered_pairs,
)
from sync_box.inventory import InventoryItem
from sync_box.sync_engine import (
    BoxMutations, _exclusive_sync_lock, _fsync_directory, _open_verified,
    _path, _safe_parent, _parent_box_id, _rename_noreplace,
)
from sync_box.two_way import build_sync_plan, validate_baseline_match
from sync_box.conflict_resolution import deterministic_conflict_copy_path


class FileResolutionError(RuntimeError):
    pass


def _snapshot(item):
    return item.to_dict() if item else None


def _same(left, right, *, local=False):
    if left is None or right is None:
        return left is right
    keys = ("item_type", "sha1", "size", "device", "inode", "mtime_ns") if local else (
        "item_type", "sha1", "size", "content_id", "version_id", "etag")
    return all(left.get(key) == right.get(key) for key in keys)


def _matches_content(item, source):
    return bool(item and source and item.item_type == "file" and
                item.sha1 == source["sha1"] and item.size == source["size"])


def _write_backup(path: Path, stream, expected: InventoryItem):
    digest = hashlib.sha1()
    total = 0
    try:
        with path.open("xb") as target:
            os.fchmod(target.fileno(), 0o600)
            chunks = iter(lambda: stream.read(1024 * 1024), b"") if hasattr(stream, "read") else iter(stream)
            for chunk in chunks:
                target.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            target.flush()
            os.fsync(target.fileno())
        if total != expected.size or digest.hexdigest() != expected.sha1:
            raise FileResolutionError("Backup failed size/SHA-1 verification; no files were changed")
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _inventories(config):
    from sync_box.box_auth import build_authenticated_client
    from sync_box.cli import _fresh_inventories
    return _fresh_inventories(config, build_authenticated_client(config))


def _load(config, token):
    with closing(sqlite3.connect(f"file:{config.state_database}?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM file_resolutions WHERE token=?", (token,)).fetchone()
    if row is None:
        raise FileResolutionError("The conflict preview no longer exists")
    result = dict(row)
    if result["local_root"] != str(config.local_root) or result["box_root_id"] != config.box_folder_id:
        raise FileResolutionError("Folder configuration changed; create a new preview")
    return result


def preview_file_conflict(config: AppConfig, path: str, *, inventories=None, box=None, automatic=False):
    """Read both versions, create verified private backups, and return a review token."""
    _path(config.local_root, path)
    initialize_database(config.state_database)
    active = active_file_resolution(config.state_database)
    if active:
        data = json.loads(active["snapshot_json"])
        if automatic:
            raise FileResolutionError("Another file resolution is incomplete; resume it first")
        return _preview_result(active["token"], data, resume=True, policy=active["policy"])
    baseline = load_baseline(config.state_database)
    if baseline is None or baseline[1:3] != (str(config.local_root), config.box_folder_id):
        raise FileResolutionError("A matching verified baseline is required")
    generation, _, _, pairs = baseline
    local_items, remote_items = (inventories or (lambda: _inventories(config)))()
    pairs = upload_recovered_pairs(config.state_database, generation, str(config.local_root), config.box_folder_id, pairs)
    conflicts = [a for a in build_sync_plan(pairs, local_items, remote_items, config.folder_modes)
                 if a.action == "conflict" and a.relative_path == path]
    if not conflicts:
        raise FileResolutionError("This path is no longer a conflict. Run sync to refresh its status.")
    local = {i.relative_path: i for i in local_items}
    remote = {i.relative_path: i for i in remote_items}
    old = next((right for left, right in pairs if left.relative_path == path), None)
    right = next((i for i in remote_items if old and i.content_id == old.content_id), remote.get(path))
    left = local.get(path)
    remote_path = right.relative_path if right else path
    if remote_path != path and (remote.get(path) or local.get(remote_path)):
        raise FileResolutionError("The moved file's destination is occupied. Review the move in its folders first.")
    if not left and not right:
        raise FileResolutionError("Neither version exists; run sync to refresh the conflict")
    for item in (left, right):
        if item and (item.item_type != "file" or item.sha1 is None or item.size is None):
            raise FileResolutionError("This action supports regular file conflicts only")
    automatic_policy = None
    if automatic:
        from sync_box.newest_conflicts import newer_file_policy
        automatic_policy = newer_file_policy(conflicts[0], left, right)
        if automatic_policy is None:
            return None
    token = uuid.uuid4().hex
    directory = config.state_database.parent / "conflict-backups" / token
    if directory.resolve().is_relative_to(config.local_root.resolve()):
        raise FileResolutionError("Conflict backups must be outside the synchronized folder")
    directory.mkdir(parents=True, mode=0o700)
    directory.parent.chmod(0o700)
    copy_path = deterministic_conflict_copy_path(path, source="local", sha1=left.sha1) if left and right else None
    touched = sorted({path, remote_path} | ({copy_path} if copy_path else set()))
    initial_local = {p: _snapshot(local.get(p)) for p in touched}
    initial_box = {p: _snapshot(remote.get(p)) for p in touched}
    sources = {}
    if right and not (right.content_id and right.version_id and right.etag is not None):
        raise FileResolutionError("The Box version or conditional write identity is unavailable")
    if box is None:
        from sync_box.box_auth import build_authenticated_client
        box = BoxMutations(build_authenticated_client(config), config.box_folder_id)
    for side, item in (("local", left), ("box", right)):
        if item is None:
            continue
        backup = directory / side
        if side == "local":
            source_path = _path(config.local_root, item.relative_path)
            _safe_parent(config.local_root, source_path.parent, item.relative_path)
            with _open_verified(source_path, item.sha1) as stream:
                _write_backup(backup, stream, item)
        else:
            stream = box.download(item.content_id, item.version_id)
            try:
                _write_backup(backup, stream, item)
            finally:
                if hasattr(stream, "close"):
                    stream.close()
        sources[side] = {"file": str(backup), "sha1": item.sha1, "size": item.size}
    data = dict(path=path, box_path=remote_path, copy_path=copy_path,
                local=initial_local, box=initial_box, sources=sources, backup_directory=str(directory),
                reason=conflicts[0].reason, automatic_policy=automatic_policy)
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps(data, indent=2))
    manifest.chmod(0o600)
    with manifest.open("rb") as handle:
        os.fsync(handle.fileno())
    _fsync_directory(directory)
    _fsync_directory(directory.parent)
    with closing(sqlite3.connect(config.state_database)) as db, db:
        db.execute("INSERT INTO file_resolutions(token,generation_id,local_root,box_root_id,original_path,snapshot_json,outcome) "
                   "VALUES (?,?,?,?,?,?,'preview')", (token, generation, str(config.local_root), config.box_folder_id, path, json.dumps(data)))
    return _preview_result(token, data)


def _preview_result(token, data, *, resume=False, policy=None):
    texts = {}
    for side in ("local", "box"):
        source = data["sources"].get(side)
        if source is None:
            texts[side] = "[File is missing]"
            continue
        with Path(source["file"]).open("rb") as handle:
            content = handle.read(65537)
        try:
            text = content[:65536].decode("utf-8")
            if "\x00" in text:
                raise UnicodeError()
            texts[side] = text + ("\n[Preview truncated at 64 KiB]" if len(content) > 65536 else "")
        except UnicodeError:
            texts[side] = f"[Binary file · {source['size']:,} bytes · SHA-1 {source['sha1']}]"
    diff = "".join(difflib.unified_diff(texts["local"].splitlines(True), texts["box"].splitlines(True),
                                        fromfile="Local", tofile="Box"))
    return dict(token=token, path=data["path"], box_path=data["box_path"], copy_path=data["copy_path"], local_preview=texts["local"],
                box_preview=texts["box"], diff=diff or "The content previews match.",
                sources=data["sources"], backup_directory=data["backup_directory"],
                resume=resume, policy=policy, automatic_policy=data.get("automatic_policy"),
                keep_both_available=not data["copy_path"] or not (
                    data["local"].get(data["copy_path"]) or data["box"].get(data["copy_path"])))


def _desired(data, policy):
    original, remote_path = data["path"], data["box_path"]
    sources = data["sources"]
    desired = {original: None, remote_path: None}
    if policy == "keep_local":
        desired[original] = sources.get("local")
    elif policy == "keep_box":
        desired[remote_path] = sources.get("box")
    elif policy == "keep_both":
        if "box" in sources:
            desired[remote_path] = sources["box"]
        if "local" in sources:
            if "box" not in sources:
                desired[original] = sources["local"]
            else:
                copy_path = data["copy_path"]
                if data["local"].get(copy_path) or data["box"].get(copy_path):
                    raise FileResolutionError("The Keep Both copy name is occupied; review that file first")
                desired[copy_path] = sources["local"]
    else:
        raise FileResolutionError("Choose Keep Local, Keep Box, or Keep Both")
    return desired


def _ensure_parent(root, path):
    _safe_parent(root, root, str(path.relative_to(root)))
    relative = path.parent.relative_to(root)
    current = root
    for component in relative.parts:
        _safe_parent(root, current, str(path.relative_to(root)))
        current = current / component
        if not current.exists():
            current.mkdir(mode=0o700)
        _safe_parent(root, current, str(path.relative_to(root)))


def _publish_local(root, relative, source, expected):
    destination = _path(root, relative)
    _ensure_parent(root, destination)
    # Fingerprint immediately before replacing or deleting the reviewed version.
    def check():
        if expected is None:
            if destination.exists() or destination.is_symlink():
                raise FileResolutionError("A local destination appeared after the preview")
        else:
            with _open_verified(destination, expected["sha1"]) as handle:
                st = os.fstat(handle.fileno())
                if (st.st_dev, st.st_ino, st.st_mtime_ns) != (expected["device"], expected["inode"], expected["mtime_ns"]):
                    raise FileResolutionError("The local file changed after the preview")
    if source is None:
        check()
        if expected:
            destination.unlink()
            _fsync_directory(destination.parent)
        return
    descriptor, name = tempfile.mkstemp(prefix=".sync-box-resolution-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as target, _open_verified(Path(source["file"]), source["sha1"]) as backup:
            shutil.copyfileobj(backup, target)
            target.flush()
            os.fsync(target.fileno())
        check()
        if expected:
            temporary.replace(destination)
        else:
            _rename_noreplace(temporary, destination)
        _fsync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def execute_file_resolution(config: AppConfig, token: str, policy: str, *, inventories=None, box=None, after_step=None):
    """Converge only reviewed paths; other conflicts do not block this resolution."""
    inventories = inventories or (lambda: _inventories(config))
    initialize_database(config.state_database)
    if box is None:
        from sync_box.box_auth import build_write_authenticated_client
        box = BoxMutations(build_write_authenticated_client(config), config.box_folder_id)
    with _exclusive_sync_lock(config.state_database, config.local_root):
        row = _load(config, token)
        if row["outcome"] == "cancelled":
            raise FileResolutionError("This preview was cancelled. Create a fresh preview.")
        if row["outcome"] == "completed":
            return {"backup_directory": json.loads(row["snapshot_json"])["backup_directory"]}
        if load_incomplete_resolution(config.state_database):
            raise FileResolutionError("A legacy resolution is incomplete; resume it first")
        active = active_file_resolution(config.state_database)
        if active and active["token"] != token:
            raise FileResolutionError("Resume the other interrupted resolution first")
        baseline = load_baseline(config.state_database)
        if baseline is None or baseline[0] != row["generation_id"]:
            raise FileResolutionError("The baseline changed. Create a new preview.")
        data = json.loads(row["snapshot_json"])
        desired = _desired(data, policy)
        for source in data["sources"].values():
            with _open_verified(Path(source["file"]), source["sha1"]) as backup:
                if os.fstat(backup.fileno()).st_size != source["size"]:
                    raise FileResolutionError("A backup changed; no further changes will be applied")
        if row["outcome"] == "in_progress":
            if row["policy"] != policy:
                raise FileResolutionError("Resume with the original resolution choice")
            desired = json.loads(row["desired_json"])
        else:
            local_items, remote_items = inventories()
            local, remote = ({i.relative_path: i for i in items} for items in (local_items, remote_items))
            for path in desired:
                if not _same(_snapshot(local.get(path)), data["local"].get(path), local=True) or not _same(
                    _snapshot(remote.get(path)), data["box"].get(path)):
                    raise FileResolutionError("A file changed after the preview. Create a new preview before choosing.")
            with closing(sqlite3.connect(config.state_database)) as db, db:
                db.execute("UPDATE file_resolutions SET policy=?,desired_json=?,outcome='in_progress',updated_at=CURRENT_TIMESTAMP WHERE token=?",
                           (policy, json.dumps(desired), token))
        local_items, remote_items = inventories()
        # If a request completed before a crash, its exact
        # desired content is enough to skip it; divergent intervening edits stop us.
        for side in ("box", "local"):
            for path, source in sorted(desired.items()):
                items = remote_items if side == "box" else local_items
                current = next((i for i in items if i.relative_path == path), None)
                if (source is None and current is None) or _matches_content(current, source):
                    continue
                if not _same(_snapshot(current), data[side].get(path), local=side == "local"):
                    raise FileResolutionError("A file changed during resolution. Backups are saved; resume after reviewing it.")
                if side == "local":
                    _publish_local(config.local_root, path, source, data[side].get(path))
                elif source is None:
                    box.delete("file", current.content_id, current.etag)
                    remote_items = [i for i in remote_items if i.relative_path != path]
                else:
                    with _open_verified(Path(source["file"]), source["sha1"]) as stream:
                        if current:
                            uploaded = box.upload_version(current.content_id, Path(path).name, stream, current.etag, source["sha1"])
                        else:
                            uploaded = box.upload_new(_parent_box_id(path, {i.relative_path: i for i in remote_items}, config.box_folder_id),
                                                      Path(path).name, stream, source["sha1"])
                    if isinstance(uploaded, InventoryItem):
                        uploaded = replace(uploaded, relative_path=path)
                        remote_items = [i for i in remote_items if i.relative_path != path] + [uploaded]
                        record_upload_receipt(config.state_database, row["generation_id"], str(config.local_root),
                            config.box_folder_id, InventoryItem(path, "file", size=source["size"], sha1=source["sha1"]), uploaded)
                if after_step:
                    after_step(side, path)
        local_items, remote_items = inventories()
        local, remote = ({i.relative_path: i for i in items} for items in (local_items, remote_items))
        for path, source in desired.items():
            if source is None:
                if path in local or path in remote:
                    raise FileResolutionError("A deleted file reappeared; the resolution remains incomplete")
            elif not _matches_content(local.get(path), source) or not _matches_content(remote.get(path), source):
                raise FileResolutionError("Files changed during resolution verification; resume after they settle")
        row["policy"] = policy
        _commit_resolution(config, row, desired, local, remote)
        return {"backup_directory": data["backup_directory"]}


def _commit_resolution(config, row, desired, local, remote):
    baseline = load_baseline(config.state_database)
    pairs = upload_recovered_pairs(config.state_database, row["generation_id"], str(config.local_root), config.box_folder_id, baseline[3])
    paired = {left.relative_path: (left, right) for left, right in pairs}
    for path, source in desired.items():
        paired.pop(path, None)
        if source:
            validate_baseline_match([local[path]], [remote[path]])
            paired[path] = (local[path], remote[path])
    # Include any structural parents created for a reviewed moved file.
    for path in desired:
        for parent in Path(path).parents:
            name = parent.as_posix()
            if name in local and name in remote and local[name].item_type == remote[name].item_type == "folder":
                paired[name] = (local[name], remote[name])
    with closing(sqlite3.connect(config.state_database)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT generation_id FROM current_baseline WHERE singleton=1").fetchone()
        if current is None or current[0] != row["generation_id"]:
            raise FileResolutionError("The baseline changed during resolution")
        generation = db.execute("INSERT INTO baseline_generations(local_root,box_root_id) VALUES (?,?)",
                                (str(config.local_root), config.box_folder_id)).lastrowid
        for left, right in paired.values():
            db.execute("INSERT INTO baseline_items VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                generation, left.relative_path, left.item_type, left.size, left.mtime_ns, left.sha1,
                left.device, left.inode, left.mode, right.content_id, right.version_id, right.etag,
                right.sha1, right.sequence_id, right.size, right.modified_at))
        db.execute("UPDATE current_baseline SET generation_id=? WHERE singleton=1", (generation,))
        db.execute("UPDATE file_resolutions SET outcome='completed',updated_at=CURRENT_TIMESTAMP WHERE token=?", (row["token"],))
        db.execute("UPDATE conflicts SET resolved_at=CURRENT_TIMESTAMP,resolution=? WHERE relative_path=? AND resolved_at IS NULL",
                   (row["policy"] or "resolved", row["original_path"]))
        latest = db.execute("SELECT id,plan_json FROM sync_runs WHERE dry_run=0 ORDER BY id DESC LIMIT 1").fetchone()
        if latest:
            plan = [a for a in json.loads(latest[1] or "[]") if a.get("relative_path") != row["original_path"]]
            count = sum(a.get("action") == "conflict" for a in plan)
            summary = f"conflicts={count}" if count else "retry: resolution complete; sync pending"
            db.execute("UPDATE sync_runs SET plan_json=?,summary=? WHERE id=?", (json.dumps(plan), summary, latest[0]))


def cancel_file_resolution(config: AppConfig, token: str):
    """Release an interrupted choice for re-review; keep every backup and change."""
    with _exclusive_sync_lock(config.state_database, config.local_root):
        row = _load(config, token)
        if row["outcome"] == "completed":
            return
        with closing(sqlite3.connect(config.state_database)) as db, db:
            db.execute("UPDATE file_resolutions SET outcome='cancelled',updated_at=CURRENT_TIMESTAMP WHERE token=?", (token,))
