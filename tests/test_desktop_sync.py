import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from sync_box.desktop_sync import Preferences, calendar_for_interval, transfer_history, load_preferences, save_preferences, set_sync_interval


class DesktopSyncTests(unittest.TestCase):
    def test_preferences_round_trip_and_invalid_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'desktop.json'
            with patch('sync_box.desktop_sync.preferences_path', return_value=path):
                self.assertEqual(load_preferences().interval, 30)
                wanted = Preferences(120, False, False)
                save_preferences(wanted)
                self.assertEqual(load_preferences(), wanted)
                path.write_text('{broken')
                self.assertEqual(load_preferences(), Preferences())

    def test_calendar_intervals(self):
        self.assertEqual(calendar_for_interval(30), '*-*-* *:0/30:00')
        self.assertEqual(calendar_for_interval(120), '*-*-* 0/2:00:00')
        self.assertEqual(calendar_for_interval(1440), '*-*-* 00:00:00')
        with self.assertRaises(ValueError):
            calendar_for_interval(0)

    def test_schedule_changes_reload_and_restart_only_when_enabled(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch('sync_box.desktop_sync.user_unit_directory', return_value=Path(directory)), patch('sync_box.desktop_sync.subprocess.run', return_value=Mock(returncode=0)) as run:
                set_sync_interval(15, enabled=True)
                self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'restart', 'sync-box.timer'])
                self.assertIn('OnCalendar=\nOnCalendar=*-*-* *:0/15:00', (Path(directory) / 'sync-box.timer.d/schedule.conf').read_text())
                run.reset_mock()
                set_sync_interval(60, enabled=False)
                self.assertEqual(run.call_count, 1)

    def test_history_filters_old_runs_and_dry_runs_keeps_failed_transfers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'state.db'
            with sqlite3.connect(path) as db:
                db.executescript('''
                    CREATE TABLE sync_runs(id INTEGER, started_at TEXT, finished_at TEXT, dry_run INTEGER);
                    CREATE TABLE sync_operations(id INTEGER,run_id INTEGER,action TEXT,relative_path TEXT,status TEXT);
                    INSERT INTO sync_runs VALUES(1,datetime('now','-49 hours'),NULL,0),(2,datetime('now'),NULL,0),(3,datetime('now'),NULL,1);
                    INSERT INTO sync_operations VALUES(1,1,'upload_new','old','completed'),(2,2,'download_new','new','completed'),(3,2,'upload_version','failed','failed'),(4,3,'upload_new','dry','completed'),(5,2,'delete_local','deleted','completed');
                ''')
            rows = transfer_history(path)
            self.assertEqual({row[2] for row in rows}, {'new', 'failed'})

    def test_recursive_watcher_detects_edits_but_ignores_reads(self):
        try:
            from PySide6.QtWidgets import QApplication
            from sync_box.folder_watch import FolderWatcher
        except ImportError:
            self.skipTest('GUI dependencies unavailable')
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sub = root / 'sub'
            sub.mkdir()
            file = sub / 'file.txt'
            file.write_text('before')
            watcher = FolderWatcher()
            changes = []
            watcher.changed.connect(lambda: changes.append(True))
            watcher.start(root)
            try:
                file.read_text()
                for _ in range(10):
                    app.processEvents()
                    time.sleep(.02)
                self.assertEqual(changes, [])
                file.write_text('after')
                for _ in range(50):
                    app.processEvents()
                    if changes:
                        break
                    time.sleep(.02)
                self.assertTrue(changes)
            finally:
                watcher.stop()
