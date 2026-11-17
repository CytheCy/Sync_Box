from contextlib import closing, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sync_box.cli import _handle_run
from sync_box.config import AppConfig
from sync_box.database import load_baseline, record_upload_receipt, replace_baseline
from sync_box.inventory import InventoryItem
from sync_box.local_inventory import scan_local
from sync_box.sync_engine import complete_unchanged_sync
from sync_box.two_way import inventories_match_baseline


class NoChangeSyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / 'root'
        self.root.mkdir()
        (self.root / 'file.txt').write_bytes(b'original')
        self.config = AppConfig(self.root, '0', base / 'state.db', base / 'sync.log', (), ())
        self.local = scan_local(self.root, hash_files=True)
        self.remote = [
            InventoryItem('.', 'folder', content_id='0'),
            InventoryItem('file.txt', 'file', content_id='10', version_id='1', etag='1',
                          size=self.local[1].size, sha1=self.local[1].sha1),
        ]
        self.generation = self.save_baseline(self.local, self.remote)
        self.args = SimpleNamespace(dry_run=False, initial_download_from_box=False,
                                    summary_only=True, json=False, limit=None)

    def save_baseline(self, local, remote):
        return replace_baseline(self.config.state_database, local_root=str(self.root),
                                box_root_id='0', local_items=local, box_items=remote)

    def run_sync(self, local=None, remote=None):
        output = StringIO()
        with (
            patch('sync_box.box_auth.build_authenticated_client') as read_client,
            patch('sync_box.box_auth.build_write_authenticated_client') as write_client,
            patch('sync_box.cli.scan_local', return_value=local or self.local) as local_scan,
            patch('sync_box.cli.scan_box', return_value=remote or self.remote) as box_scan,
            redirect_stdout(output),
        ):
            result = _handle_run(self.args, self.config)
        return result, output.getvalue(), read_client, write_client, local_scan, box_scan

    def test_unchanged_run_scans_once_without_write_token_or_new_baseline(self):
        # Round-trip through SQLite: derived local fields are deliberately absent.
        self.assertIsNone(load_baseline(self.config.state_database)[3][1][0].modified_at)
        result, output, read, write, local_scan, box_scan = self.run_sync()
        self.assertEqual(result, 0)
        self.assertIn('no changes', output)
        read.assert_called_once()
        write.assert_not_called()
        local_scan.assert_called_once()
        self.assertTrue(local_scan.call_args.kwargs['hash_files'])
        box_scan.assert_called_once()
        self.assertEqual(load_baseline(self.config.state_database)[0], self.generation)
        with closing(sqlite3.connect(self.config.state_database)) as connection:
            self.assertEqual(connection.execute(
                'SELECT outcome, summary FROM sync_runs').fetchall(),
                [('completed', 'completed=0; no changes')])

    def test_identical_edits_on_both_sides_still_verify_and_commit(self):
        (self.root / 'file.txt').write_bytes(b'new common content')
        changed_local = scan_local(self.root, hash_files=True)
        changed_box = [self.remote[0], replace(self.remote[1], version_id='2',
                       size=changed_local[1].size, sha1=changed_local[1].sha1)]
        result, output, read, write, local_scan, box_scan = self.run_sync(changed_local, changed_box)
        self.assertEqual(result, 0)
        self.assertNotIn('no changes', output)
        write.assert_called_once()
        self.assertEqual(local_scan.call_count, 3)
        self.assertEqual(box_scan.call_count, 3)
        self.assertGreater(load_baseline(self.config.state_database)[0], self.generation)

    def test_recovered_upload_is_committed_instead_of_fast_path(self):
        self.generation = self.save_baseline(self.local[:1], self.remote[:1])
        record_upload_receipt(self.config.state_database, self.generation, str(self.root),
                              '0', self.local[1], self.remote[1])
        result, output, read, write, local_scan, box_scan = self.run_sync()
        self.assertEqual(result, 0)
        write.assert_called_once()
        self.assertEqual(local_scan.call_count, 3)
        self.assertEqual(len(load_baseline(self.config.state_database)[3]), 2)

    def test_local_metadata_change_uses_full_verification(self):
        changed = [self.local[0], replace(self.local[1], inode=self.local[1].inode + 1)]
        result, output, read, write, local_scan, box_scan = self.run_sync(changed)
        write.assert_called_once()
        self.assertEqual(local_scan.call_count, 3)

    def test_one_way_box_only_content_and_folder_summaries_do_not_rescan(self):
        folder = InventoryItem('uploads', 'folder', device=7, inode=88, mtime_ns=10)
        remote_folder = InventoryItem('uploads', 'folder', content_id='20', size=1,
                                      modified_at='old', etag='1')
        self.local.append(folder)
        self.remote.append(remote_folder)
        self.generation = self.save_baseline(self.local, self.remote)
        self.config = replace(self.config, folder_modes=(('uploads', 'one-way'),))
        current_local = self.local[:-1] + [replace(folder, mtime_ns=20)]
        current_box = self.remote[:-1] + [
            replace(remote_folder, size=1000, modified_at='new', etag='2'),
            InventoryItem('uploads/box-only.txt', 'file', content_id='30', sha1='ignored'),
        ]
        result, output, read, write, local_scan, box_scan = self.run_sync(current_local, current_box)
        self.assertEqual(result, 0)
        self.assertIn('no changes', output)
        write.assert_not_called()
        local_scan.assert_called_once()
        box_scan.assert_called_once()
        self.assertEqual(load_baseline(self.config.state_database)[0], self.generation)

    def test_folder_identity_and_file_metadata_still_must_match(self):
        baseline = load_baseline(self.config.state_database)[3]
        for field, value in [('content_id', 'different'), ('etag', '2'),
                             ('modified_at', 'new'), ('sha1', 'different')]:
            with self.subTest(field=field):
                remote = [self.remote[0], replace(self.remote[1], **{field: value})]
                self.assertFalse(inventories_match_baseline(baseline, self.local, remote))
        self.assertFalse(inventories_match_baseline(
            baseline, self.local, [replace(self.remote[0], content_id='different'), self.remote[1]],
        ))
        self.assertFalse(inventories_match_baseline(
            baseline, [replace(self.local[0], inode=0), self.local[1]], self.remote,
        ))

    def test_dry_run_does_not_journal_or_obtain_write_token(self):
        self.args.dry_run = True
        result, output, read, write, local_scan, box_scan = self.run_sync()
        write.assert_not_called()
        local_scan.assert_called_once()
        with closing(sqlite3.connect(self.config.state_database)) as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM sync_runs').fetchone()[0], 0)

    def test_stale_generation_cannot_record_unchanged_success(self):
        self.save_baseline(self.local, self.remote)
        with self.assertRaisesRegex(RuntimeError, 'Baseline generation changed'):
            complete_unchanged_sync(self.root, self.config.state_database,
                                    baseline_generation=self.generation)

    def test_interrupted_resolution_blocks_unchanged_success(self):
        with closing(sqlite3.connect(self.config.state_database)) as connection:
            connection.execute(
                "INSERT INTO conflict_resolution_runs "
                "(baseline_generation, resolution_key, policy, original_path, conflict_copy_path, plan_json, outcome) "
                "VALUES (?, 'test', 'keep-both', 'file.txt', 'copy.txt', '{}', 'in_progress')",
                (self.generation,),
            )
            connection.commit()
        with self.assertRaisesRegex(RuntimeError, 'resolution.*incomplete'):
            self.run_sync()


if __name__ == '__main__':
    unittest.main()
