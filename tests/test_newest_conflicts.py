from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sync_box.cli import _handle_run
from sync_box.config import ConfigError, load_config, save_conflict_policy
from sync_box.database import active_file_resolution
from sync_box.file_resolution import FileResolutionError, execute_file_resolution, preview_file_conflict
from sync_box.newest_conflicts import newer_file_policy, resolve_newer_conflicts
from sync_box.setup_config import create_initial_config
from test_upload_recovery import Fixture


class NewestConflictTests(Fixture):
    def setUp(self):
        super().setUp()
        self.config = replace(self.config, conflict_policy="prefer_newest")
        (self.root / "a.txt").write_bytes(b"local edit")
        local_time = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp())
        os.utime(self.root / "a.txt", (local_time, local_time))
        self.cloud.put("a.txt", b"Box edit")
        self.remote_time("2026-01-01T00:00:00Z")

    def remote_time(self, timestamp):
        item, data = self.cloud.files["a.txt"]
        self.cloud.files["a.txt"] = (replace(item, modified_at=timestamp), data)

    def resolve(self, inventories=None):
        return resolve_newer_conflicts(self.config, self.plan(), *self.inventories(),
                                       inventories=inventories or self.inventories, box=self.cloud)

    def test_local_newer_is_kept_with_both_verified_backups_and_manifest(self):
        resolved = self.resolve()
        self.assertEqual(len(resolved), 1)
        self.assertEqual(self.cloud.files["a.txt"][1], b"local edit")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"local edit")
        directory = Path(resolved[0]["backup_directory"])
        self.assertEqual((directory / "local").read_bytes(), b"local edit")
        self.assertEqual((directory / "box").read_bytes(), b"Box edit")
        manifest = json.loads((directory / "manifest.json").read_text())
        self.assertEqual(manifest["automatic_policy"], "keep_local")
        self.assertEqual(manifest["box"]["a.txt"]["modified_at"], "2026-01-01T00:00:00Z")
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.resolve(), [])

    def test_box_newer_is_kept_and_backup_retained(self):
        self.remote_time("2026-01-03T00:00:00Z")
        result = self.resolve()[0]
        self.assertEqual(result["policy"], "keep_box")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"Box edit")
        self.assertEqual(Path(result["backup_directory"], "local").read_bytes(), b"local edit")
        self.assertEqual(self.plan(), [])

    def test_ties_missing_invalid_or_timezone_less_timestamps_require_review(self):
        for value in (None, "", "bad", "2026-01-02T00:00:00", "2026-01-02T00:00:00.9Z", "2026-01-01T16:00:00-08:00"):
            with self.subTest(value=value):
                self.remote_time(value)
                self.assertEqual(self.resolve(), [])
                self.assertEqual([a.action for a in self.plan()], ["conflict"])
        self.assertFalse((self.directory / "conflict-backups").exists())

    def test_default_manual_policy_does_not_resolve(self):
        self.config = replace(self.config, conflict_policy="manual")
        self.assertEqual(self.resolve(), [])
        self.assertEqual(self.cloud.uploads, 0)
        self.assertFalse((self.directory / "conflict-backups").exists())

    def test_other_conflict_types_are_never_selected(self):
        action = self.plan()[0]
        local, remote = self.scan()[1], self.cloud.files["a.txt"][0]
        for reason in ("Box deleted while local changed", "different new items occupy the same path",
                       "ambiguous or simultaneous rename/change", "item types differ",
                       "baseline Box item was replaced by a different item ID"):
            self.assertIsNone(newer_file_policy(replace(action, reason=reason), local, remote))
        self.assertIsNone(newer_file_policy(action, None, remote))
        self.assertIsNone(newer_file_policy(action, local, replace(remote, relative_path="moved.txt")))
        self.assertIsNone(newer_file_policy(action, replace(local, modified_at=None), remote))

    def test_fresh_inventory_recomputes_winner_before_backing_up(self):
        def fresh():
            self.remote_time("2026-01-03T00:00:00Z")
            return self.inventories()
        self.assertEqual(self.resolve(fresh)[0]["policy"], "keep_box")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"Box edit")

    def test_fresh_tie_leaves_conflict_without_backups(self):
        def fresh():
            self.remote_time("2026-01-02T00:00:00Z")
            return self.inventories()
        self.assertEqual(self.resolve(fresh), [])
        self.assertFalse((self.directory / "conflict-backups").exists())

    def test_edit_after_backup_stops_automatic_overwrite(self):
        original = execute_file_resolution
        def changed(config, token, policy, **kwargs):
            (self.root / "a.txt").write_bytes(b"edit after preview")
            return original(config, token, policy, **kwargs)
        with patch("sync_box.newest_conflicts.execute_file_resolution", side_effect=changed):
            with self.assertRaisesRegex(FileResolutionError, "changed after the preview"):
                self.resolve()
        self.assertEqual(self.cloud.files["a.txt"][1], b"Box edit")
        self.assertEqual((self.root / "a.txt").read_bytes(), b"edit after preview")

    def test_interrupted_automatic_choice_resumes_without_reupload(self):
        original = execute_file_resolution
        def stop(*args, **kwargs):
            def interrupted(side, path):
                raise RuntimeError("interrupted")
            return original(*args, **kwargs, after_step=interrupted)
        with patch("sync_box.newest_conflicts.execute_file_resolution", side_effect=stop):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.resolve()
        self.assertIsNotNone(active_file_resolution(self.db))
        uploads = self.cloud.uploads
        result = self.resolve()
        self.assertEqual(result[0]["policy"], "keep_local")
        self.assertEqual(self.cloud.uploads, uploads)
        self.assertIsNone(active_file_resolution(self.db))
        self.assertEqual(self.plan(), [])

    def test_interrupted_manual_choice_is_never_automatically_resumed(self):
        preview = preview_file_conflict(self.config, "a.txt", inventories=self.inventories, box=self.cloud)
        def stop(side, path):
            raise RuntimeError("interrupted")
        with self.assertRaises(RuntimeError):
            execute_file_resolution(self.config, preview["token"], "keep_local", inventories=self.inventories,
                                    box=self.cloud, after_step=stop)
        with self.assertRaisesRegex(FileResolutionError, "manual resolution"):
            self.resolve()

    def test_cli_resolves_then_reloads_baseline_and_finishes_normal_sync(self):
        args = SimpleNamespace(dry_run=False, initial_download_from_box=False,
                               summary_only=True, json=False, limit=None)
        with patch("sync_box.box_auth.build_authenticated_client"), \
             patch("sync_box.box_auth.build_write_authenticated_client"), \
             patch("sync_box.cli._fresh_inventories", side_effect=lambda *a, **k: self.inventories()), \
             patch("sync_box.sync_engine.BoxMutations", return_value=self.cloud), \
             patch("sync_box.cli.BoxMutations", return_value=self.cloud), redirect_stdout(StringIO()) as out:
            self.assertEqual(_handle_run(args, self.config), 0)
        self.assertIn("backups:", out.getvalue())
        self.assertIn("no changes", out.getvalue())
        self.assertEqual(self.plan(), [])

    def test_dry_run_obtains_no_write_token_and_creates_no_backups(self):
        args = SimpleNamespace(dry_run=True, initial_download_from_box=False,
                               summary_only=True, json=False, limit=None)
        with patch("sync_box.box_auth.build_authenticated_client"), \
             patch("sync_box.box_auth.build_write_authenticated_client") as write, \
             patch("sync_box.cli._fresh_inventories", return_value=self.inventories()), redirect_stdout(StringIO()) as out:
            self.assertEqual(_handle_run(args, self.config), 0)
        write.assert_not_called()
        self.assertIn("conflicts=1", out.getvalue())
        self.assertFalse((self.directory / "conflict-backups").exists())

    def test_config_policy_round_trip_preserves_other_settings(self):
        path = self.directory / "config.toml"
        create_initial_config(self.root, config_path=path, state_directory=self.directory)
        self.assertEqual(load_config(path).conflict_policy, "manual")
        for policy in ("prefer_newest", "manual", "prefer_newest"):
            save_conflict_policy(path, policy)
            config = load_config(path)
            self.assertEqual(config.conflict_policy, policy)
            self.assertEqual(config.local_root, self.root)
            self.assertEqual(config.state_database, self.directory / "state.sqlite3")
        before = path.read_text()
        with self.assertRaises(ConfigError):
            save_conflict_policy(path, "creation_time")
        self.assertEqual(path.read_text(), before)

    def test_multiple_conflicts_use_each_updated_baseline(self):
        from sync_box.database import replace_baseline
        (self.root / "b.txt").write_bytes(b"original b")
        self.cloud.put("b.txt", b"original b")
        # Preserve a common ancestor for both paths before diverging again.
        replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
                         local_items=self.scan(), box_items=self.cloud.inventory())
        (self.root / "a.txt").write_bytes(b"local next")
        self.cloud.put("a.txt", b"Box next")
        self.remote_time("2026-01-01T00:00:00Z")
        (self.root / "b.txt").write_bytes(b"local b edit")
        item = self.cloud.put("b.txt", b"Box b edit")
        self.cloud.files["b.txt"] = (replace(item, modified_at="2026-01-01T00:00:00Z"), b"Box b edit")
        self.assertEqual(len(self.resolve()), 2)
        self.assertEqual(self.plan(), [])
        self.assertEqual(self.cloud.files["a.txt"][1], b"local next")
        self.assertEqual(self.cloud.files["b.txt"][1], b"local b edit")

    def test_existing_empty_policy_section_and_other_tables_are_preserved(self):
        path = self.directory / "config.toml"
        create_initial_config(self.root, config_path=path, state_directory=self.directory)
        path.write_text(path.read_text() + '\n[conflicts]\n# retained comment\n[folder_modes]\n"." = "two-way"')
        save_conflict_policy(path, "prefer_newest")
        self.assertEqual(load_config(path).conflict_policy, "prefer_newest")
        self.assertEqual(load_config(path).folder_modes, ((".", "two-way"),))
        self.assertIn("# retained comment", path.read_text())
