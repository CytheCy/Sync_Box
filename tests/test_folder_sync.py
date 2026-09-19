from __future__ import annotations

from contextlib import closing
import fcntl
import hashlib
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from sync_box.database import load_baseline, replace_baseline
from sync_box.inventory import InventoryItem
from sync_box.local_inventory import scan_local
from sync_box.sync_engine import (
    BoxMutations,
    SyncExecutionError,
    _rename_noreplace,
    execute_sync,
)
from sync_box.two_way import build_sync_plan


def remote_tree(local_items: list[InventoryItem]) -> list[InventoryItem]:
    result = []
    for item in local_items:
        item_id = "0" if item.relative_path == "." else "box:" + item.relative_path
        result.append(
            InventoryItem(
                item.relative_path,
                item.item_type,
                size=item.size,
                content_id=item_id,
                version_id="1" if item.item_type == "file" else None,
                etag="1",
                sha1=item.sha1,
            )
        )
    return result


def moved_remote(items: list[InventoryItem], source: str, destination: str) -> list[InventoryItem]:
    result = []
    for item in items:
        path = item.relative_path
        if path == source or path.startswith(source + "/"):
            path = destination + path[len(source):]
        data = item.to_dict()
        data["relative_path"] = path
        result.append(InventoryItem(**data))
    return result


class FakeBox:
    root_folder_id = "0"

    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []
        self.next_folder = 100

    def create_folder(self, parent_id, name):
        self.next_folder += 1
        item_id = str(self.next_folder)
        self.calls.append(("mkdir", parent_id, name, item_id))
        return item_id

    def upload_new(self, parent_id, name, source, sha1):
        self.calls.append(("upload", parent_id, name, source.read(), sha1))

    def upload_version(self, *args):
        raise AssertionError("not expected")

    def download(self, item_id, version):
        raise AssertionError("not expected")

    def delete(self, kind, item_id, etag):
        self.calls.append(("delete", kind, item_id, etag))

    def move(self, kind, item_id, parent_id, name, etag):
        self.calls.append(("move", kind, item_id, parent_id, name, etag))


class FolderPlannerTests(unittest.TestCase):
    def test_folder_creation_order_both_directions(self) -> None:
        root_l = InventoryItem(".", "folder", device=1, inode=1)
        root_b = InventoryItem(".", "folder", content_id="0")
        local = [
            root_l,
            InventoryItem("p", "folder", device=1, inode=2, mtime_ns=1),
            InventoryItem("p/c", "folder", device=1, inode=3, mtime_ns=1),
            InventoryItem("p/c/f", "file", size=1, sha1=hashlib.sha1(b"x").hexdigest(), device=1, inode=4),
        ]
        local_plan = build_sync_plan([(root_l, root_b)], local, [root_b])
        self.assertEqual(
            [(a.action, a.relative_path) for a in local_plan],
            [("create_box_folder", "p"), ("create_box_folder", "p/c"), ("upload_new", "p/c/f")],
        )
        remote = remote_tree(local)
        box_plan = build_sync_plan([(root_l, root_b)], [root_l], remote)
        self.assertEqual(
            [(a.action, a.relative_path) for a in box_plan],
            [("create_local_folder", "p"), ("create_local_folder", "p/c"), ("download_new", "p/c/f")],
        )

    def test_nonempty_deletion_is_child_before_parent_both_directions(self) -> None:
        local = [
            InventoryItem(".", "folder", device=1, inode=1),
            InventoryItem("p", "folder", device=1, inode=2, mtime_ns=1),
            InventoryItem("p/c", "folder", device=1, inode=3, mtime_ns=1),
            InventoryItem("p/c/f", "file", size=1, sha1="a", device=1, inode=4),
        ]
        remote = remote_tree(local)
        pairs = list(zip(local, remote))
        delete_box = build_sync_plan(pairs, [local[0]], remote)
        delete_local = build_sync_plan(pairs, local, [remote[0]])
        expected = ["p/c/f", "p/c", "p"]
        self.assertEqual([a.relative_path for a in delete_box], expected)
        self.assertEqual([a.relative_path for a in delete_local], expected)

    def test_folder_moves_collapse_descendants_and_collisions_conflict(self) -> None:
        old_local = [
            InventoryItem(".", "folder", device=1, inode=1),
            InventoryItem("old", "folder", device=1, inode=2, mtime_ns=1),
            InventoryItem("old/f", "file", size=1, sha1="a", device=1, inode=3),
        ]
        old_box = remote_tree(old_local)
        pairs = list(zip(old_local, old_box))
        new_local = [old_local[0],
                     InventoryItem("new", "folder", device=1, inode=2, mtime_ns=1),
                     InventoryItem("new/f", "file", size=1, sha1="a", device=1, inode=3)]
        plan = build_sync_plan(pairs, new_local, old_box)
        self.assertEqual([(a.action, a.relative_path, a.destination_path) for a in plan],
                         [("move_box", "old", "new")])
        box_plan = build_sync_plan(pairs, old_local, moved_remote(old_box, "old", "new"))
        self.assertEqual([(a.action, a.relative_path, a.destination_path) for a in box_plan],
                         [("move_local", "old", "new")])
        occupied = old_box + [InventoryItem("new", "folder", content_id="occupied")]
        self.assertTrue(any(a.action == "conflict" for a in build_sync_plan(pairs, new_local, occupied)))

    def test_parent_move_precedes_new_child_and_changed_child_conflicts(self) -> None:
        old_local = [
            InventoryItem(".", "folder", device=1, inode=1),
            InventoryItem("old", "folder", device=1, inode=2, mtime_ns=1),
            InventoryItem("old/f", "file", size=1, sha1="a", device=1, inode=3),
        ]
        old_box = remote_tree(old_local)
        pairs = list(zip(old_local, old_box))
        moved = [old_local[0],
                 InventoryItem("new", "folder", device=1, inode=2, mtime_ns=2),
                 InventoryItem("new/f", "file", size=1, sha1="a", device=1, inode=3),
                 InventoryItem("new/extra", "file", size=1, sha1="b", device=1, inode=4)]
        plan = build_sync_plan(pairs, moved, old_box)
        self.assertEqual([(a.action, a.relative_path) for a in plan],
                         [("move_box", "old"), ("upload_new", "new/extra")])
        changed = moved[:-1]
        changed[2] = InventoryItem("new/f", "file", size=1, sha1="changed", device=1, inode=3)
        self.assertTrue(any(a.action == "conflict" for a in build_sync_plan(pairs, changed, old_box)))


class FolderExecutorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.db = Path(self.temp.name) / "state.sqlite3"
        self.baseline_local = scan_local(self.root, hash_files=True)
        self.baseline_box = remote_tree(self.baseline_local)
        self.generation = replace_baseline(
            self.db,
            local_root=str(self.root),
            box_root_id="0",
            local_items=self.baseline_local,
            box_items=self.baseline_box,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def plan(self, local_items, box_items):
        return build_sync_plan(load_baseline(self.db)[3], local_items, box_items)

    def execute_plan(self, plan, box, box_items, **kwargs):
        return execute_sync(
            box,
            self.root,
            self.db,
            plan,
            baseline_generation=self.generation,
            box_items_by_path={item.relative_path: item for item in box_items},
            **kwargs,
        )

    def test_empty_folder_creation_both_directions(self) -> None:
        (self.root / "local").mkdir()
        local_now = scan_local(self.root, hash_files=True)
        fake = FakeBox()
        self.execute_plan(self.plan(local_now, self.baseline_box), fake, self.baseline_box)
        self.assertEqual(fake.calls[0][0:3], ("mkdir", "0", "local"))

        remote = self.baseline_box + [InventoryItem("remote", "folder", content_id="20", etag="1")]
        self.execute_plan(self.plan(self.baseline_local, remote), FakeBox(), remote)
        self.assertTrue((self.root / "remote").is_dir())

    def test_folder_preflight_accepts_hyphen_prefixed_child(self) -> None:
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "-child").write_bytes(b"x")
        local_now = scan_local(self.root, hash_files=True)
        fake = FakeBox()

        self.execute_plan(
            self.plan(local_now, self.baseline_box), fake, self.baseline_box
        )

        self.assertEqual(fake.calls[0][0:3], ("mkdir", "0", "folder"))
        self.assertEqual(fake.calls[1][0:3], ("upload", "101", "-child"))

    def test_empty_and_nonempty_folder_deletion_both_directions(self) -> None:
        # Empty Box deletion and local deletion execution.
        folder = self.root / "empty"
        folder.mkdir()
        local = scan_local(self.root, hash_files=True)
        remote = remote_tree(local)
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=local, box_items=remote)
        folder.rmdir()
        local_now = scan_local(self.root, hash_files=True)
        fake = FakeBox()
        self.execute_plan(self.plan(local_now, remote), fake, remote)
        self.assertEqual(fake.calls[-1][0:2], ("delete", "folder"))

        # Empty local folder deletion after Box removed the folder.
        folder.mkdir()
        local = scan_local(self.root, hash_files=True)
        remote = remote_tree(local)
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=local, box_items=remote)
        current_box = [item for item in remote if item.relative_path != "empty"]
        self.execute_plan(self.plan(local, current_box), FakeBox(), current_box)
        self.assertFalse(folder.exists())

        # Non-empty Box deletion after the complete local tree was removed.
        parent = self.root / "outbound"
        parent.mkdir()
        (parent / "child").write_bytes(b"x")
        local = scan_local(self.root, hash_files=True)
        remote = remote_tree(local)
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=local, box_items=remote)
        (parent / "child").unlink()
        parent.rmdir()
        local_now = scan_local(self.root, hash_files=True)
        fake = FakeBox()
        self.execute_plan(self.plan(local_now, remote), fake, remote)
        deletes = [call for call in fake.calls if call[0] == "delete"]
        self.assertEqual([call[1] for call in deletes], ["file", "folder"])

        # Non-empty local deletion after Box removed the tree.
        parent = self.root / "tree"
        parent.mkdir()
        (parent / "child").write_bytes(b"x")
        local = scan_local(self.root, hash_files=True)
        remote = remote_tree(local)
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=local, box_items=remote)
        current_box = [item for item in remote if item.relative_path not in {"tree", "tree/child"}]
        self.execute_plan(self.plan(local, current_box), FakeBox(), current_box)
        self.assertFalse(parent.exists())

    def test_local_folder_move_updates_runtime_parent_map(self) -> None:
        parent = self.root / "old"
        parent.mkdir()
        (parent / "kept").write_bytes(b"x")
        local = scan_local(self.root, hash_files=True)
        remote = remote_tree(local)
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=local, box_items=remote)
        parent.rename(self.root / "new")
        (self.root / "new" / "extra").write_bytes(b"y")
        local_now = scan_local(self.root, hash_files=True)
        plan = self.plan(local_now, remote)
        fake = FakeBox()
        mapping = {item.relative_path: item for item in remote}
        execute_sync(fake, self.root, self.db, plan, baseline_generation=self.generation,
                     box_items_by_path=mapping)
        moved_id = next(item.content_id for item in remote if item.relative_path == "old")
        upload = next(call for call in fake.calls if call[0] == "upload")
        self.assertEqual(upload[1], moved_id)

    def test_box_folder_move_renames_local_tree(self) -> None:
        parent = self.root / "old"
        parent.mkdir()
        (parent / "kept").write_bytes(b"x")
        local = scan_local(self.root, hash_files=True)
        remote = remote_tree(local)
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=local, box_items=remote)
        moved = moved_remote(remote, "old", "new")
        self.execute_plan(self.plan(local, moved), FakeBox(), moved)
        self.assertTrue((self.root / "new/kept").is_file())
        self.assertFalse((self.root / "old").exists())

    def test_folder_replacement_and_subtree_change_are_refused(self) -> None:
        folder = self.root / "folder"
        folder.mkdir()
        local = scan_local(self.root, hash_files=True)
        plan = self.plan(local, self.baseline_box)
        folder.rmdir()
        folder.mkdir()
        with self.assertRaisesRegex(SyncExecutionError, "identity changed"):
            self.execute_plan(plan, FakeBox(), self.baseline_box)

        local = scan_local(self.root, hash_files=True)
        plan = self.plan(local, self.baseline_box)
        (folder / "racing").write_bytes(b"x")
        with self.assertRaisesRegex(SyncExecutionError, "subtree changed"):
            self.execute_plan(plan, FakeBox(), self.baseline_box)

    def test_atomic_destination_race_never_overwrites(self) -> None:
        source = self.root / "source"
        source.mkdir()
        local = scan_local(self.root, hash_files=True)
        remote = remote_tree(local)
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=local, box_items=remote)
        moved = moved_remote(remote, "source", "destination")
        plan = self.plan(local, moved)

        def race(src, dst):
            dst.mkdir()
            (dst / "winner").write_bytes(b"safe")
            return _rename_noreplace(src, dst)

        with patch("sync_box.sync_engine._rename_noreplace", side_effect=race):
            with self.assertRaisesRegex(SyncExecutionError, "destination exists"):
                self.execute_plan(plan, FakeBox(), moved)
        self.assertEqual((self.root / "destination/winner").read_bytes(), b"safe")
        self.assertTrue(source.is_dir())

    def test_unexpected_child_stops_folder_deletion(self) -> None:
        folder = self.root / "folder"
        folder.mkdir()
        local = scan_local(self.root, hash_files=True)
        remote = remote_tree(local)
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                                           local_items=local, box_items=remote)
        current_box = [item for item in remote if item.relative_path != "folder"]
        plan = self.plan(local, current_box)
        import sync_box.sync_engine as engine
        original = engine._verify_folder_identity
        calls = 0

        def add_child(path, action):
            nonlocal calls
            original(path, action)
            calls += 1
            if calls == 2:
                (path / "unexpected").write_bytes(b"safe")

        with patch("sync_box.sync_engine._verify_folder_identity", side_effect=add_child):
            with self.assertRaisesRegex(SyncExecutionError, "unexpected child"):
                self.execute_plan(plan, FakeBox(), current_box)
        self.assertEqual((folder / "unexpected").read_bytes(), b"safe")

    def test_stale_generation_and_concurrent_lock_are_refused(self) -> None:
        with self.assertRaisesRegex((RuntimeError, SyncExecutionError), "generation changed"):
            execute_sync(FakeBox(), self.root, self.db, [],
                         baseline_generation=self.generation + 1, box_items_by_path={})
        descriptor = self.db.open("r+b")
        try:
            fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(SyncExecutionError, "already active"):
                execute_sync(FakeBox(), self.root, self.db, [],
                             baseline_generation=self.generation, box_items_by_path={})
        finally:
            descriptor.close()

        other_db = Path(self.temp.name) / "other-state.sqlite3"
        other_generation = replace_baseline(
            other_db, local_root=str(self.root), box_root_id="0",
            local_items=self.baseline_local, box_items=self.baseline_box,
        )
        root_descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            fcntl.flock(root_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(SyncExecutionError, "already active"):
                execute_sync(FakeBox(), self.root, other_db, [],
                             baseline_generation=other_generation, box_items_by_path={})
        finally:
            os.close(root_descriptor)

    def test_locked_plan_revalidation_refuses_concurrent_change(self) -> None:
        folder = self.root / "folder"
        folder.mkdir()
        local_now = scan_local(self.root, hash_files=True)
        plan = self.plan(local_now, self.baseline_box)
        changed_plan = self.plan(self.baseline_local, self.baseline_box)
        fake = FakeBox()
        with self.assertRaisesRegex(SyncExecutionError, "plan changed"):
            self.execute_plan(
                plan,
                fake,
                self.baseline_box,
                revalidate_plan=lambda: changed_plan,
            )
        self.assertEqual(fake.calls, [])
        self.assertEqual(load_baseline(self.db)[0], self.generation)

    def test_interruption_resume_and_verified_baseline_commit(self) -> None:
        remote = self.baseline_box + [
            InventoryItem("p", "folder", content_id="20", etag="1"),
            InventoryItem("p/c", "folder", content_id="21", etag="1"),
        ]
        plan = self.plan(self.baseline_local, remote)
        import sync_box.sync_engine as engine
        original = engine._execute_one
        calls = 0

        def interrupt_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("interrupted")
            return original(*args, **kwargs)

        with patch("sync_box.sync_engine._execute_one", side_effect=interrupt_second):
            with self.assertRaises(SyncExecutionError):
                self.execute_plan(plan, FakeBox(), remote)
        self.assertEqual(load_baseline(self.db)[0], self.generation)
        self.assertTrue((self.root / "p").is_dir())

        local_now = scan_local(self.root, hash_files=True)
        remaining = self.plan(local_now, remote)
        result = self.execute_plan(
            remaining,
            FakeBox(),
            remote,
            verify_inventories=lambda: (scan_local(self.root, hash_files=True), remote),
        )
        self.assertEqual(result.new_baseline_generation, self.generation + 1)
        self.assertTrue((self.root / "p/c").is_dir())

    def test_safe_resume_after_interruption_after_each_folder_step(self) -> None:
        import sync_box.sync_engine as engine
        original = engine._execute_one
        for interrupt_after in (1, 2):
            with self.subTest(interrupt_after=interrupt_after), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary) / "root"
                root.mkdir()
                db = Path(temporary) / "state.sqlite3"
                baseline_local = scan_local(root, hash_files=True)
                baseline_box = remote_tree(baseline_local)
                generation = replace_baseline(
                    db, local_root=str(root), box_root_id="0",
                    local_items=baseline_local, box_items=baseline_box,
                )
                remote = baseline_box + [
                    InventoryItem("p", "folder", content_id="20", etag="1"),
                    InventoryItem("p/c", "folder", content_id="21", etag="1"),
                ]
                plan = build_sync_plan(load_baseline(db)[3], baseline_local, remote)
                calls = 0

                def interrupt_after_action(*args, **kwargs):
                    nonlocal calls
                    result = original(*args, **kwargs)
                    calls += 1
                    if calls == interrupt_after:
                        raise RuntimeError("simulated process interruption")
                    return result

                with patch("sync_box.sync_engine._execute_one", side_effect=interrupt_after_action):
                    with self.assertRaises(SyncExecutionError):
                        execute_sync(
                            FakeBox(), root, db, plan,
                            baseline_generation=generation,
                            box_items_by_path={item.relative_path: item for item in remote},
                        )
                self.assertEqual(load_baseline(db)[0], generation)
                current_local = scan_local(root, hash_files=True)
                remaining = build_sync_plan(load_baseline(db)[3], current_local, remote)
                result = execute_sync(
                    FakeBox(), root, db, remaining,
                    baseline_generation=generation,
                    box_items_by_path={item.relative_path: item for item in remote},
                    verify_inventories=lambda: (scan_local(root, hash_files=True), remote),
                )
                self.assertEqual(result.new_baseline_generation, generation + 1)
                self.assertTrue((root / "p/c").is_dir())

    def test_verification_failure_leaves_baseline_unchanged(self) -> None:
        remote = self.baseline_box + [InventoryItem("p", "folder", content_id="20", etag="1")]
        plan = self.plan(self.baseline_local, remote)
        with self.assertRaises(SyncExecutionError):
            self.execute_plan(
                plan,
                FakeBox(),
                remote,
                verify_inventories=lambda: (scan_local(self.root, hash_files=True), self.baseline_box),
            )
        self.assertEqual(load_baseline(self.db)[0], self.generation)

    def test_box_folder_delete_is_non_recursive(self) -> None:
        folders = Mock()
        client = SimpleNamespace(folders=folders)
        BoxMutations(client, "0").delete("folder", "42", "7")
        folders.delete_folder_by_id.assert_called_once_with(
            "42", recursive=False, if_match="7"
        )


if __name__ == "__main__":
    unittest.main()
