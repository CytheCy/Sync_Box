from dataclasses import replace
from pathlib import Path
import sqlite3
import unittest

from sync_box.database import active_file_resolution, begin_sync_run, finish_sync_run, load_baseline
from sync_box.file_resolution import preview_file_conflict, execute_file_resolution, FileResolutionError
from sync_box.app_status import read_database_state
from sync_box.sync_engine import SyncExecutionError
from test_upload_recovery import Fixture, sha


class FileResolutionTests(Fixture):
    def divergent(self):
        (self.root / "a.txt").write_bytes(b"local version\n")
        self.cloud.put("a.txt", b"Box version\n")

    def preview(self):
        return preview_file_conflict(self.config, "a.txt", inventories=self.inventories, box=self.cloud)

    def resolve(self, preview, policy, after_step=None):
        return execute_file_resolution(self.config, preview["token"], policy,
            inventories=self.inventories, box=self.cloud, after_step=after_step)

    def test_each_choice_preserves_backups_and_clears_only_selected_conflict(self):
        for policy in ("keep_local", "keep_box", "keep_both"):
            with self.subTest(policy=policy):
                # Restore a separate common ancestor for each choice.
                if policy != "keep_local":
                    for path in list(self.root.iterdir()):
                        path.unlink()
                    (self.root / "a.txt").write_bytes(b"original")
                    self.cloud.files.clear()
                    self.cloud.put("a.txt", b"original")
                    from sync_box.database import replace_baseline
                    replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                        local_items=self.scan(), box_items=self.cloud.inventory())
                self.divergent()
                self.cloud.put("unrelated.txt", b"cloud only")
                preview = self.preview()
                self.assertIn("local version", preview["local_preview"])
                self.assertIn("Box version", preview["box_preview"])
                self.assertIn("-local version", preview["diff"])
                backup = Path(preview["backup_directory"])
                self.assertEqual((backup / "local").read_bytes(), b"local version\n")
                self.assertEqual((backup / "box").read_bytes(), b"Box version\n")
                self.resolve(preview, policy)
                expected = b"local version\n" if policy == "keep_local" else b"Box version\n"
                self.assertEqual((self.root / "a.txt").read_bytes(), expected)
                self.assertEqual(self.cloud.files["a.txt"][1], expected)
                copies = list(self.root.glob("*conflict*"))
                self.assertEqual(len(copies), 1 if policy == "keep_both" else 0)
                if copies:
                    self.assertEqual(copies[0].read_bytes(), b"local version\n")
                    self.assertEqual(self.cloud.files[copies[0].name][1], b"local version\n")
                self.assertFalse((self.root / "unrelated.txt").exists())
                self.assertIsNone(active_file_resolution(self.db))
                self.assertEqual((backup / "local").stat().st_mode & 0o777, 0o600)

    def test_local_deletion_keep_box_restores_and_keep_local_deletes_cloud(self):
        self.cloud.put("a.txt", b"Box changed")
        (self.root / "a.txt").unlink()
        preview = self.preview()
        self.assertIn("missing", preview["local_preview"])
        self.resolve(preview, "keep_box")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"Box changed")
        (self.root / "a.txt").unlink()
        self.cloud.put("a.txt", b"Box changed again")
        preview = self.preview()
        self.resolve(preview, "keep_local")
        self.assertNotIn("a.txt", self.cloud.files)
        self.assertNotIn("a.txt", {l.relative_path for l, r in load_baseline(self.db)[3]})

    def test_box_deletion_keep_local_restores_and_keep_box_deletes_local(self):
        (self.root / "a.txt").write_bytes(b"local changed")
        self.cloud.files.clear()
        preview = self.preview()
        self.resolve(preview, "keep_local")
        self.assertEqual(self.cloud.files["a.txt"][1], b"local changed")
        (self.root / "a.txt").write_bytes(b"changed again")
        self.cloud.files.clear()
        preview = self.preview()
        self.resolve(preview, "keep_box")
        self.assertFalse((self.root / "a.txt").exists())

    def test_keep_both_restores_existing_version_when_other_side_deleted(self):
        self.cloud.put("a.txt", b"Box changed")
        (self.root / "a.txt").unlink()
        self.resolve(self.preview(), "keep_both")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"Box changed")

    def test_stale_preview_is_rejected_before_mutation(self):
        self.divergent()
        preview = self.preview()
        (self.root / "a.txt").write_bytes(b"new local edit")
        with self.assertRaisesRegex(FileResolutionError, "changed after the preview"):
            self.resolve(preview, "keep_box")
        self.assertEqual(self.cloud.files["a.txt"][1], b"Box version\n")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"new local edit")
        self.assertIsNone(active_file_resolution(self.db))

    def test_corrupt_backup_prevents_overwrite(self):
        self.divergent()
        preview = self.preview()
        Path(preview["backup_directory"], "box").write_bytes(b"corrupt")
        with self.assertRaises(SyncExecutionError):
            self.resolve(preview, "keep_local")
        self.assertEqual(self.cloud.files["a.txt"][1], b"Box version\n")
        self.assertIsNone(active_file_resolution(self.db))

    def test_resume_after_upload_does_not_upload_again_and_blocks_normal_sync(self):
        self.divergent()
        preview = self.preview()
        def stop(side, path):
            raise RuntimeError("interrupted")
        with self.assertRaisesRegex(RuntimeError, "interrupted"):
            self.resolve(preview, "keep_local", stop)
        count = self.cloud.uploads
        with self.assertRaisesRegex(SyncExecutionError, "interrupted file resolution"):
            self.execute()
        state = read_database_state(self.db)
        self.assertTrue(state.conflict)
        self.assertEqual(state.conflicts[0].relative_path, "a.txt")
        resumed = self.preview()
        self.assertEqual(resumed["token"], preview["token"])
        self.assertTrue(resumed["resume"])
        self.resolve(resumed, "keep_local")
        self.assertEqual(self.cloud.uploads, count)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"local version\n")
        self.assertIsNone(active_file_resolution(self.db))
        self.assertEqual(self.plan(), [])

    def test_external_edit_after_interruption_is_preserved(self):
        self.divergent()
        preview = self.preview()
        def stop(side, path):
            raise RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.resolve(preview, "keep_local", stop)
        self.cloud.put("a.txt", b"external after interruption")
        with self.assertRaisesRegex(FileResolutionError, "changed during resolution"):
            self.resolve(preview, "keep_local")
        self.assertEqual(self.cloud.files["a.txt"][1], b"external after interruption")
        self.assertIsNotNone(active_file_resolution(self.db))

    def test_second_conflict_does_not_block_selected_resolution(self):
        (self.root / "b.txt").write_bytes(b"local new")
        self.cloud.put("b.txt", b"Box new")
        self.divergent()
        run = begin_sync_run(self.db, self.plan(), baseline_generation=self.generation, local_root=str(self.root))
        finish_sync_run(self.db, run, "failed", "conflicts=2")
        self.resolve(self.preview(), "keep_box")
        self.assertEqual([(a.action, a.relative_path) for a in self.plan()], [("conflict", "b.txt")])
        self.assertEqual([c.relative_path for c in read_database_state(self.db).conflicts], ["b.txt"])

    def test_box_move_plus_local_deletion_keep_box_preserves_move(self):
        (self.root / "a.txt").unlink()
        item, data = self.cloud.files.pop("a.txt")
        self.cloud.files["moved.txt"] = (replace(item, relative_path="moved.txt"), data)
        preview = self.preview()
        self.assertEqual(preview["box_path"], "moved.txt")
        self.resolve(preview, "keep_box")
        self.assertFalse((self.root / "a.txt").exists())
        self.assertEqual((self.root / "moved.txt").read_bytes(), b"original")
        self.assertEqual(self.plan(), [])

    def test_resolution_is_idempotent_after_commit(self):
        self.divergent()
        preview = self.preview()
        self.resolve(preview, "keep_both")
        generation = load_baseline(self.db)[0]
        count = self.cloud.uploads
        self.resolve(preview, "keep_both")
        self.assertEqual(load_baseline(self.db)[0], generation)
        self.assertEqual(self.cloud.uploads, count)


    def test_refreshing_an_interrupted_choice_retains_backups_and_unblocks_sync(self):
        from sync_box.file_resolution import cancel_file_resolution
        self.divergent()
        preview = self.preview()
        def stop(side, path):
            raise RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.resolve(preview, "keep_local", stop)
        (self.root / "a.txt").write_bytes(b"new local edit")
        cancel_file_resolution(self.config, preview["token"])
        self.assertIsNone(active_file_resolution(self.db))
        self.assertTrue(Path(preview["backup_directory"], "box").is_file())
        with self.assertRaisesRegex(FileResolutionError, "cancelled"):
            self.resolve(preview, "keep_local")
        self.assertEqual([a.action for a in self.plan()], ["upload_version"])
        self.execute()
        self.assertEqual(self.cloud.files["a.txt"][1], b"new local edit")

    def test_box_conditional_write_rejects_edit_after_last_inventory(self):
        self.divergent()
        preview = self.preview()
        original_upload = self.cloud.upload_version
        def raced(*args):
            self.cloud.put("a.txt", b"racing Box edit")
            return original_upload(*args)
        self.cloud.upload_version = raced
        with self.assertRaisesRegex(RuntimeError, "412"):
            self.resolve(preview, "keep_local")
        self.assertEqual(self.cloud.files["a.txt"][1], b"racing Box edit")
        self.assertIsNotNone(active_file_resolution(self.db))


    def test_interrupted_choice_cannot_silently_change_policy(self):
        self.divergent()
        preview = self.preview()
        def stop(side, path):
            raise RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            self.resolve(preview, "keep_local", stop)
        with self.assertRaisesRegex(FileResolutionError, "original resolution choice"):
            self.resolve(preview, "keep_box")
        self.assertEqual(active_file_resolution(self.db)["policy"], "keep_local")
