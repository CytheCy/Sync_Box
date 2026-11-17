"""Folder directions across configuration, planning and verified execution."""
from dataclasses import replace
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from sync_box.config import ConfigError, folder_mode, load_config, save_folder_mode
from sync_box.database import load_baseline, replace_baseline
from sync_box.inventory import InventoryItem
from sync_box.local_inventory import scan_local
from sync_box.setup_config import create_initial_config
from sync_box.sync_engine import execute_sync
from sync_box.two_way import build_sync_plan
from test_two_way import local, box


class DirectionTests(unittest.TestCase):
    modes = (("notes", "one-way"),)

    def test_local_wins_even_after_both_sides_changed(self):
        plan = build_sync_plan([(local("notes/a"), box("notes/a"))],
            [local("notes/a", b"local")], [box("notes/a", b"cloud", version="2")], self.modes)
        self.assertEqual([(a.action, a.sha1, a.box_version_id) for a in plan],
                         [("upload_version", hashlib.sha1(b"local").hexdigest(), "2")])

    def test_cloud_edit_is_replaced_even_when_local_unchanged(self):
        plan = build_sync_plan([(local("notes/a"), box("notes/a"))],
            [local("notes/a")], [box("notes/a", b"cloud", version="2")], self.modes)
        self.assertEqual([a.action for a in plan], ["upload_version"])

    def test_local_deletion_leaves_cloud_copy_and_never_downloads(self):
        self.assertEqual(build_sync_plan([(local("notes/a"), box("notes/a"))],
            [], [box("notes/a", b"changed", version="2"), box("notes/new", item_id="20")], self.modes), [])

    def test_remote_deletion_is_reuploaded(self):
        plan = build_sync_plan([(local("notes/a"), box("notes/a"))], [local("notes/a")], [], self.modes)
        self.assertEqual([a.action for a in plan], ["upload_new"])

    def test_two_way_subfolder_override_and_sibling_unchanged(self):
        modes = self.modes + (("notes/shared", "two-way"),)
        plan = build_sync_plan([], [], [box("notes/shared/a"), box("notes/private", item_id="20"),
                                      box("notes2/a", item_id="30")], modes)
        self.assertEqual({(a.action, a.relative_path) for a in plan},
                         {("download_new", "notes/shared/a"), ("download_new", "notes2/a")})
        self.assertEqual(folder_mode("notes/shared/a", modes), "two-way")
        self.assertEqual(folder_mode("notes/private", modes), "one-way")

    def test_type_and_case_collisions_still_require_review(self):
        for remote in [box("notes/a", kind="folder"), box("notes/A")]:
            with self.subTest(remote=remote):
                plan = build_sync_plan([], [local("notes/a")], [remote], self.modes)
                self.assertEqual([a.action for a in plan], ["conflict"])

    def test_recursive_parent_deletion_cannot_delete_one_way_subtree(self):
        parent = (local("parent", kind="folder"), box("parent", kind="folder", item_id="1"))
        child = (local("parent/notes/a"), box("parent/notes/a"))
        plan = build_sync_plan([parent, child], [], [parent[1], child[1]],
                               (("parent/notes", "one-way"),))
        self.assertEqual([a.action for a in plan], ["conflict"])

    def test_cloud_move_across_direction_boundary_requires_review(self):
        plan = build_sync_plan([(local("shared/a"), box("shared/a"))],
            [local("shared/a")], [box("notes/a")], self.modes)
        self.assertEqual([a.action for a in plan], ["conflict"])
        self.assertIn("different sync directions", plan[0].reason)

    def test_root_one_way_with_nested_override(self):
        plan = build_sync_plan([], [local("a")],
            [box("a", b"cloud"), box("cloud", item_id="20"), box("shared/b", item_id="30")],
            ((".", "one-way"), ("shared", "two-way")))
        self.assertEqual({(a.action, a.relative_path) for a in plan},
                         {("upload_version", "a"), ("download_new", "shared/b")})

    def test_execution_baselines_without_downloading_cloud_only_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tree = root / "local"
            (tree / "notes").mkdir(parents=True)
            (tree / "notes/a").write_bytes(b"local")
            items = scan_local(tree, hash_files=True)
            remote = [InventoryItem(i.relative_path, i.item_type, size=i.size, sha1=i.sha1,
                       content_id="0" if i.relative_path == "." else "id:" + i.relative_path,
                       version_id="1") for i in items]
            db = root / "state.db"
            gen = replace_baseline(db, local_root=str(tree), box_root_id="0", local_items=items, box_items=remote)
            remote = [replace(i, sha1=hashlib.sha1(b"cloud").hexdigest(), size=5, version_id="2")
                      if i.relative_path == "notes/a" else i for i in remote]
            remote.append(box("notes/cloud-only", item_id="99"))
            plan = build_sync_plan(load_baseline(db)[3], items, remote, self.modes)
            fake = Mock(root_folder_id="0")
            def upload(file_id, name, source, etag, sha1):
                data = source.read()
                self.assertEqual(data, b"local")
                remote[:] = [replace(i, sha1=sha1, size=len(data), version_id="3")
                             if i.content_id == file_id else i for i in remote]
            fake.upload_version.side_effect = upload
            execute_sync(fake, tree, db, plan, baseline_generation=gen, folder_modes=self.modes,
                         box_items_by_path={i.relative_path: i for i in remote},
                         verify_inventories=lambda: (scan_local(tree, hash_files=True), remote))
            self.assertEqual(build_sync_plan(load_baseline(db)[3], scan_local(tree, hash_files=True), remote, self.modes), [])
            self.assertFalse((tree / "notes/cloud-only").exists())
            fake.download.assert_not_called()
            fake.delete.assert_not_called()


class SettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.local = self.root / "local"
        self.local.mkdir()
        self.path = self.root / "config.toml"
        create_initial_config(self.local, config_path=self.path, state_directory=self.root / "state")

    def test_round_trip_preserves_other_sections_and_replaces_settings(self):
        original = self.path.read_text()
        save_folder_mode(self.path, "notes", "one-way")
        self.path.write_text(self.path.read_text() + '\n[oauth]\nredirect_uri = "http://localhost"\n')
        save_folder_mode(self.path, "notes/shared", "two-way")
        save_folder_mode(self.path, "notes", "two-way")
        config = load_config(self.path)
        self.assertEqual(dict(config.folder_modes), {"notes": "two-way", "notes/shared": "two-way"})
        self.assertIn(original.strip(), self.path.read_text())
        self.assertIn('[oauth]\nredirect_uri = "http://localhost"', self.path.read_text())
        self.assertEqual(self.path.read_text().count('[folder_modes]'), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_invalid_setting_leaves_configuration_unchanged(self):
        original = self.path.read_bytes()
        for folder, mode in [("../escape", "one-way"), ("/absolute", "one-way"), ("notes", "wrong")]:
            with self.subTest(folder=folder, mode=mode), self.assertRaises(ConfigError):
                save_folder_mode(self.path, folder, mode)
            self.assertEqual(self.path.read_bytes(), original)

    def test_gui_lists_folders_and_saves_direction_with_inheritance(self):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            from PySide6.QtWidgets import QApplication, QMainWindow
        except ImportError:
            self.skipTest("PySide6 not installed")
        from sync_box.gui import FolderSettingsDialog, FolderModeDialog
        app = QApplication.instance() or QApplication([])
        self.__class__.app = app
        (self.local / "notes/shared").mkdir(parents=True)
        window = QMainWindow()
        window.provider = Mock(config_path=self.path)
        window.refresh_status = Mock()
        settings = FolderSettingsDialog(window)
        self.assertEqual(settings.folders, [".", "notes", "notes/shared"])
        editor = FolderModeDialog(settings, "notes")
        editor.direction.setCurrentIndex(editor.direction.findData("one-way"))
        editor._save()
        self.assertEqual(dict(load_config(self.path).folder_modes), {"notes": "one-way"})
        self.assertIn("One way", settings.table.item(2, 1).text())
        self.assertIn("inherited", settings.table.item(2, 1).text())
        window.refresh_status.assert_called_once()
        settings.close()
        window.close()
        window.deleteLater()
        app.processEvents()
