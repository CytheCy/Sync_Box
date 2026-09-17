from __future__ import annotations

from contextlib import closing
from io import BytesIO
import hashlib
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from box_sdk_gen.box.errors import BoxSDKError

from sync_box.database import load_baseline, replace_baseline
from sync_box.inventory import InventoryItem, ScanError
from sync_box.sync_engine import BoxMutations, SyncExecutionError, execute_sync
from sync_box.two_way import build_sync_plan, validate_baseline_match


def sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def local(path: str, data: bytes = b"one", *, inode: int = 1, kind: str = "file") -> InventoryItem:
    return InventoryItem(path, kind, size=len(data) if kind == "file" else None,
                         sha1=sha(data) if kind == "file" else None,
                         device=7, inode=inode, mtime_ns=10)


def box(path: str, data: bytes = b"one", *, item_id: str = "10", version: str = "1",
        kind: str = "file", etag: str = "1") -> InventoryItem:
    return InventoryItem(path, kind, size=len(data) if kind == "file" else None,
                         content_id=item_id, version_id=version,
                         sha1=sha(data) if kind == "file" else None, etag=etag)


ROOT_PAIR = (InventoryItem(".", "folder", device=7, inode=99),
             InventoryItem(".", "folder", content_id="0"))


class PlannerTests(unittest.TestCase):
    def plan(self, old_l: InventoryItem, old_b: InventoryItem,
             current_l: InventoryItem | None, current_b: InventoryItem | None):
        return build_sync_plan([ROOT_PAIR, (old_l, old_b)],
                               [ROOT_PAIR[0]] + ([current_l] if current_l else []),
                               [ROOT_PAIR[1]] + ([current_b] if current_b else []))

    def test_additions_both_directions(self) -> None:
        actions = build_sync_plan([ROOT_PAIR], [ROOT_PAIR[0], local("local.txt")],
                                  [ROOT_PAIR[1], box("remote.txt", item_id="20")])
        self.assertEqual({a.action for a in actions}, {"upload_new", "download_new"})

    def test_one_sided_modifications(self) -> None:
        self.assertEqual(self.plan(local("a"), box("a"), local("a", b"two"), box("a"))[0].action,
                         "upload_version")
        self.assertEqual(self.plan(local("a"), box("a"), local("a"), box("a", b"two", version="2"))[0].action,
                         "download_version")

    def test_simultaneous_edits_conflict_but_identical_result_does_not(self) -> None:
        conflict = self.plan(local("a"), box("a"), local("a", b"left"),
                             box("a", b"right", version="2"))
        self.assertEqual(conflict[0].action, "conflict")
        same = self.plan(local("a"), box("a"), local("a", b"same"),
                         box("a", b"same", version="2"))
        self.assertEqual(same, [])

    def test_deletions_require_unchanged_counterpart(self) -> None:
        self.assertEqual(self.plan(local("a"), box("a"), None, box("a"))[0].action, "delete_box")
        self.assertEqual(self.plan(local("a"), box("a"), local("a"), None)[0].action, "delete_local")
        self.assertEqual(self.plan(local("a"), box("a"), None,
                                   box("a", b"changed", version="2"))[0].action, "conflict")

    def test_box_id_and_inode_support_unambiguous_renames(self) -> None:
        local_move = self.plan(local("old", inode=4), box("old", item_id="42"),
                               local("new", inode=4), box("old", item_id="42"))
        self.assertEqual((local_move[0].action, local_move[0].destination_path), ("move_box", "new"))
        box_move = self.plan(local("old", inode=4), box("old", item_id="42"),
                             local("old", inode=4), box("new", item_id="42"))
        self.assertEqual((box_move[0].action, box_move[0].destination_path), ("move_local", "new"))

    def test_divergent_renames_are_conflicts(self) -> None:
        actions = self.plan(local("old", inode=4), box("old", item_id="42"),
                            local("left", inode=4), box("right", item_id="42"))
        self.assertEqual(actions[0].action, "conflict")

    def test_prebaseline_validation_rejects_missing_hash_and_mismatch(self) -> None:
        with self.assertRaisesRegex(ScanError, "paths differ"):
            validate_baseline_match([ROOT_PAIR[0], local("a")], [ROOT_PAIR[1]])
        with self.assertRaisesRegex(ScanError, "SHA-1 unavailable"):
            validate_baseline_match([ROOT_PAIR[0], InventoryItem("a", "file")],
                                    [ROOT_PAIR[1], box("a")])


class BaselineDatabaseTests(unittest.TestCase):
    def test_round_trip_and_transaction_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            db = Path(temporary) / "state.db"
            left = [ROOT_PAIR[0], local("a")]
            right = [ROOT_PAIR[1], box("a")]
            first = replace_baseline(db, local_root="/safe", box_root_id="0",
                                     local_items=left, box_items=right)
            loaded = load_baseline(db)
            self.assertEqual((loaded[0], loaded[1], loaded[2], len(loaded[3])),
                             (first, "/safe", "0", 2))
            with closing(sqlite3.connect(db)) as connection:
                connection.execute("CREATE TRIGGER reject_new BEFORE INSERT ON baseline_items BEGIN SELECT RAISE(ABORT, 'stop'); END")
                connection.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                replace_baseline(db, local_root="/safe", box_root_id="0",
                                 local_items=left, box_items=right)
            self.assertEqual(load_baseline(db)[0], first)


class FakeBox:
    root_folder_id = "0"
    def __init__(self, downloads: dict[str, bytes] | None = None) -> None:
        self.downloads = downloads or {}
        self.calls = []
    def download(self, file_id, version):
        self.calls.append(("download", file_id, version))
        return BytesIO(self.downloads[file_id])
    def create_folder(self, parent_id, name):
        self.calls.append(("mkdir", parent_id, name)); return "new-folder"
    def upload_new(self, parent, name, source, sha1):
        self.calls.append(("upload", parent, name, source.read()))
    def upload_version(self, item_id, name, source, etag, sha1):
        self.calls.append(("version", item_id, etag, source.read()))
    def delete(self, kind, item_id, etag): self.calls.append(("delete", kind, item_id, etag))
    def move(self, kind, item_id, parent, name, etag): self.calls.append(("move", item_id, parent, name))


class ExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "root"; self.root.mkdir()
        self.db = Path(self.temp.name) / "state.db"
        self.generation = replace_baseline(
            self.db, local_root=str(self.root), box_root_id="0",
            local_items=[ROOT_PAIR[0]], box_items=[ROOT_PAIR[1]],
        )
    def tearDown(self) -> None: self.temp.cleanup()

    def test_download_is_verified_and_published(self) -> None:
        content = b"remote"
        action = build_sync_plan([ROOT_PAIR], [ROOT_PAIR[0]],
                                 [ROOT_PAIR[1], box("new", content, item_id="20")])[0]
        result = execute_sync(FakeBox({"20": content}), self.root, self.db, [action],
                              baseline_generation=self.generation,
                              box_items_by_path={".": ROOT_PAIR[1]})
        self.assertEqual(result.completed, 1)
        self.assertEqual((self.root / "new").read_bytes(), content)

    def test_box_adapter_sends_sha1_hex_as_content_md5(self) -> None:
        uploads = Mock()
        adapter = BoxMutations(SimpleNamespace(uploads=uploads), "0")
        source = BytesIO(b"content")
        digest = sha(b"content")

        adapter.upload_new("0", "file.txt", source, digest)

        self.assertEqual(source.tell(), 0)
        self.assertEqual(uploads.upload_file.call_args.kwargs["content_md_5"], digest)

    def test_conflict_preflight_makes_no_changes(self) -> None:
        action = self._conflict()
        with self.assertRaisesRegex(SyncExecutionError, "conflict"):
            execute_sync(FakeBox(), self.root, self.db, [action],
                         baseline_generation=self.generation, box_items_by_path={})
        with closing(sqlite3.connect(self.db)) as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM sync_runs").fetchone()[0], 0)

    def _conflict(self):
        return self._action("conflict", "a")
    @staticmethod
    def _action(name, path, **kwargs):
        from sync_box.two_way import SyncAction
        return SyncAction(name, path, **kwargs)

    def test_unsafe_path_and_symlink_parent_are_rejected(self) -> None:
        with self.assertRaises(SyncExecutionError):
            execute_sync(FakeBox(), self.root, self.db, [self._action("download_new", "../escape")],
                         baseline_generation=self.generation, box_items_by_path={})
        outside = Path(self.temp.name) / "outside"; outside.mkdir()
        (self.root / "link").symlink_to(outside, target_is_directory=True)
        action = self._action("download_new", "link/file", box_item_id="20", size=1, sha1=sha(b"x"))
        with self.assertRaises(SyncExecutionError):
            execute_sync(FakeBox({"20": b"x"}), self.root, self.db, [action],
                         baseline_generation=self.generation, box_items_by_path={})
        self.assertFalse((outside / "file").exists())

    def test_interruption_is_journaled_and_baseline_is_unchanged(self) -> None:
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=[ROOT_PAIR[0]], box_items=[ROOT_PAIR[1]])
        before = load_baseline(self.db)[0]
        actions = [self._action("create_local_folder", "done", item_type="folder"),
                   self._action("download_new", "missing", box_item_id="99", size=1, sha1=sha(b"x"))]
        with self.assertRaises(SyncExecutionError):
            execute_sync(FakeBox(), self.root, self.db, actions,
                         baseline_generation=self.generation, box_items_by_path={})
        self.assertTrue((self.root / "done").is_dir())
        self.assertEqual(load_baseline(self.db)[0], before)
        with closing(sqlite3.connect(self.db)) as connection:
            statuses = [row[0] for row in connection.execute("SELECT status FROM sync_operations ORDER BY id")]
        self.assertEqual(statuses, ["completed", "failed"])

    def test_filesystem_error_does_not_advance_operation(self) -> None:
        action = self._action("create_local_folder", "folder", item_type="folder")
        (self.root / "folder").write_bytes(b"collision")
        with self.assertRaises(SyncExecutionError):
            execute_sync(FakeBox(), self.root, self.db, [action],
                         baseline_generation=self.generation, box_items_by_path={})

    def test_upload_error_is_sanitized(self) -> None:
        (self.root / "a").write_bytes(b"data")
        action = self._action("upload_new", "a", size=4, sha1=sha(b"data"))
        class Broken(FakeBox):
            def upload_new(self, *args): raise RuntimeError("Authorization: Bearer secret")
        with self.assertRaises(SyncExecutionError) as raised:
            execute_sync(Broken(), self.root, self.db, [action],
                         baseline_generation=self.generation, box_items_by_path={})
        self.assertNotIn("secret", str(raised.exception))

    def test_expired_download_token_refreshes_once(self) -> None:
        content = b"fresh"
        action = self._action("download_new", "fresh", box_item_id="20",
                              size=len(content), sha1=sha(content))
        class Expired(FakeBox):
            def download(self, file_id, version):
                raise BoxSDKError(message="Developer token has expired. Please provide a new one.")
        replacement = FakeBox({"20": content})
        calls = []
        def refresh(): calls.append(True); return replacement
        execute_sync(Expired(), self.root, self.db, [action], box_items_by_path={},
                     baseline_generation=self.generation, refresh_box=refresh)
        self.assertEqual(len(calls), 1)
        self.assertEqual((self.root / "fresh").read_bytes(), content)


if __name__ == "__main__":
    unittest.main()
