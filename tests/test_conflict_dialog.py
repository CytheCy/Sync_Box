import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import unittest

try:
    from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
except ImportError:
    raise unittest.SkipTest("PySide6 not installed")

from sync_box.app_status import ConflictDetail
from sync_box.gui import ConflictDialog
from sync_box.setup_config import create_initial_config
from test_upload_recovery import Fixture


class ConflictDialogTests(Fixture):
    def setUp(self):
        super().setUp()
        self.__class__.app = QApplication.instance() or QApplication([])
        (self.root / "a.txt").write_bytes(b"local version\n")
        self.cloud.put("a.txt", b"Box version\n")
        self.config_path = self.directory / "config.toml"
        create_initial_config(self.root, config_path=self.config_path, state_directory=self.directory)
        self.config_path.write_text(self.config_path.read_text().replace("state.sqlite3", "state.db"))
        from sync_box.file_resolution import preview_file_conflict
        self.preview = preview_file_conflict(self.config, "a.txt", inventories=self.inventories, box=self.cloud)
        self.window = QMainWindow()
        self.window.provider = SimpleNamespace(config_path=self.config_path)
        self.window.snapshot = SimpleNamespace(local_folder=self.root,
            database=SimpleNamespace(conflicts=(ConflictDetail("a.txt", "both sides changed since baseline"),)))
        self.window.refresh_status = Mock()
        self.window.sync_now = Mock()
        self.window._resolution_busy = False

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate() and time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(predicate())

    def dialog(self):
        with patch("sync_box.file_resolution.preview_file_conflict", return_value=self.preview):
            dialog = ConflictDialog(self.window)
            self.wait_for(lambda: dialog._preview is not None)
        self.addCleanup(dialog.close)
        self.addCleanup(self.window.close)
        return dialog

    def test_preview_and_keep_both_execute_the_reviewed_token(self):
        from sync_box.file_resolution import execute_file_resolution
        dialog = self.dialog()
        self.assertIn("local version", dialog.preview_fields["local_preview"].toPlainText())
        self.assertIn("Box version", dialog.preview_fields["box_preview"].toPlainText())
        self.assertTrue(dialog.keep_local_button.isEnabled())
        self.assertTrue(dialog.keep_box_button.isEnabled())
        self.assertTrue(dialog.suggested_button.isEnabled())
        def execute(config, token, policy):
            return execute_file_resolution(config, token, policy, inventories=self.inventories, box=self.cloud)
        with patch("sync_box.file_resolution.execute_file_resolution", side_effect=execute) as operation, \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.Yes), \
             patch.object(QMessageBox, "information"):
            dialog.suggested_button.click()
            self.wait_for(lambda: self.window.sync_now.called)
            operation.assert_called_once()
            self.assertEqual(operation.call_args.args[1:], (self.preview["token"], "keep_both"))
        self.assertEqual((self.root / "a.txt").read_bytes(), b"Box version\n")
        self.assertEqual(len(list(self.root.glob("*conflict*"))), 1)
        self.window.refresh_status.assert_called_once()

    def test_declining_a_choice_does_not_execute_it(self):
        dialog = self.dialog()
        with patch("sync_box.file_resolution.execute_file_resolution") as operation, \
             patch.object(QMessageBox, "question", return_value=QMessageBox.StandardButton.No):
            dialog.keep_local_button.click()
            operation.assert_not_called()
        self.assertEqual(self.cloud.files["a.txt"][1], b"Box version\n")
        self.assertFalse(self.window._resolution_busy)
