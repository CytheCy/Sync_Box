from contextlib import closing, redirect_stdout
from io import StringIO
import hashlib
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from sync_box.cli import _handle_run
from sync_box.config import AppConfig
from sync_box.database import initialize_database, load_baseline, replace_baseline
from sync_box.hash_cache import HASH_MAX_AGE_NS
from sync_box.inventory import InventoryItem, ScanChangedError
from sync_box.local_inventory import scan_local


class HashCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name)
        self.root = base / 'root'
        self.root.mkdir()
        self.file = self.root / 'a.txt'
        self.file.write_bytes(b'first')
        self.database = base / 'state.db'

    def scan(self, **kwargs):
        return scan_local(self.root, hash_files=True,
                          hash_cache_database=self.database, **kwargs)

    def rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute('SELECT * FROM local_hash_cache ORDER BY relative_path').fetchall()

    def test_second_scan_reuses_hash_and_does_not_extend_verification_deadline(self):
        with patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
            first = self.scan()
            rows = self.rows()
            second = self.scan()
        self.assertEqual(first, second)
        self.assertEqual(digest.call_count, 1)
        self.assertEqual(self.rows(), rows)

    def test_edit_with_restored_mtime_is_invalidated_by_ctime(self):
        first = self.scan()
        before = self.file.stat()
        self.file.write_bytes(b'other')
        os.utime(self.file, ns=(before.st_atime_ns, before.st_mtime_ns))
        with patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
            second = self.scan()
        digest.assert_called_once()
        self.assertNotEqual(first[1].sha1, second[1].sha1)

    def test_replacement_with_same_size_and_mtime_is_rehashed(self):
        self.scan()
        before = self.file.stat()
        replacement = self.root / 'replacement'
        replacement.write_bytes(b'other')
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(replacement, self.file)
        with patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
            items = self.scan()
        digest.assert_called_once()
        self.assertEqual(items[1].sha1, hashlib.sha1(b'other').hexdigest())

    def test_each_fingerprint_field_invalidates_cache(self):
        self.scan()
        for field in ('device', 'inode', 'size', 'mtime_ns', 'ctime_ns', 'mode'):
            with self.subTest(field=field):
                with closing(sqlite3.connect(self.database)) as connection, connection:
                    connection.execute(f'UPDATE local_hash_cache SET {field}={field}+1')
                with patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
                    self.scan()
                digest.assert_called_once()

    def test_expired_future_and_malformed_entries_are_rehashed(self):
        with patch('sync_box.hash_cache.time_ns', return_value=HASH_MAX_AGE_NS):
            self.scan()
        for now in (2 * HASH_MAX_AGE_NS, 0):
            with self.subTest(now=now):
                with patch('sync_box.hash_cache.time_ns', return_value=now), \
                     patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
                    self.scan()
                digest.assert_called_once()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute("UPDATE local_hash_cache SET sha1='invalid'")
        with patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
            self.scan()
        digest.assert_called_once()

    def test_forced_and_uncached_scans_always_read_contents(self):
        self.scan()
        with patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
            self.scan(force_hashes=True)
            scan_local(self.root, hash_files=True)
        self.assertEqual(digest.call_count, 2)

    def test_deleted_excluded_renamed_and_non_file_paths_are_pruned(self):
        for name in ('deleted', 'excluded', 'renamed', 'symlink', 'directory'):
            (self.root / name).write_bytes(b'data')
        self.scan()
        (self.root / 'deleted').unlink()
        (self.root / 'renamed').rename(self.root / 'new-name')
        (self.root / 'symlink').unlink()
        (self.root / 'symlink').symlink_to(self.file)
        (self.root / 'directory').unlink()
        (self.root / 'directory').mkdir()
        self.scan(excluded_names=('excluded',))
        self.assertEqual({row[1] for row in self.rows()}, {'a.txt', 'new-name'})
        with patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
            self.scan()
        digest.assert_called_once()  # Exclusion removed: previously excluded file is new.

    def test_failed_scan_never_commits_partial_entries_or_pruning(self):
        self.scan()
        before = self.rows()
        (self.root / 'new').write_bytes(b'new')
        with patch('sync_box.local_inventory._sha1_file', side_effect=ScanChangedError('changed')):
            with self.assertRaises(ScanChangedError):
                self.scan()
        self.assertEqual(self.rows(), before)

    def test_cached_file_is_still_opened_and_checked_for_replacement(self):
        self.scan()
        original_open = os.open

        def replace_then_open(path, flags, *args, **kwargs):
            if Path(path) == self.file:
                replacement = self.root / 'new'
                replacement.write_bytes(b'other')
                os.replace(replacement, self.file)
            return original_open(path, flags, *args, **kwargs)

        with patch('sync_box.local_inventory.os.open', side_effect=replace_then_open):
            with self.assertRaises(ScanChangedError):
                self.scan()

    def test_same_size_edit_during_hash_with_restored_mtime_is_rejected(self):
        before = self.file.stat()
        original_digest = hashlib.file_digest

        def edit_after_digest(stream, algorithm):
            digest = original_digest(stream, algorithm)
            self.file.write_bytes(b'other')
            os.utime(self.file, ns=(before.st_atime_ns, before.st_mtime_ns))
            return digest

        with patch('sync_box.local_inventory.hashlib.file_digest', side_effect=edit_after_digest):
            with self.assertRaises(ScanChangedError):
                self.scan()
        self.assertEqual(self.rows(), [])

    def test_roots_are_isolated_and_metadata_only_scan_never_creates_cache(self):
        scan_local(self.root, hash_cache_database=self.database)
        self.assertFalse(self.database.exists())
        self.scan()
        other = self.root.parent / 'other'
        other.mkdir()
        os.link(self.file, other / 'a.txt')
        with patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest:
            scan_local(other, hash_files=True, hash_cache_database=self.database)
        digest.assert_called_once()
        self.assertEqual(len(self.rows()), 2)

    def test_normal_run_reuses_persistent_cache_but_dry_run_hashes_without_writing(self):
        local = scan_local(self.root, hash_files=True)
        remote = [InventoryItem('.', 'folder', content_id='0'),
                  InventoryItem('a.txt', 'file', size=5, content_id='10',
                                version_id='1', sha1=local[1].sha1)]
        config = AppConfig(self.root, '0', self.database, self.root.parent / 'sync.log', (), ())
        replace_baseline(self.database, local_root=str(self.root), box_root_id='0',
                         local_items=local, box_items=remote)
        args = SimpleNamespace(dry_run=False, initial_download_from_box=False,
                               summary_only=True, json=False, limit=None)
        with patch('sync_box.box_auth.build_authenticated_client'), \
             patch('sync_box.box_auth.build_write_authenticated_client') as write, \
             patch('sync_box.cli.scan_box', return_value=remote) as box_scan, \
             patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest, \
             redirect_stdout(StringIO()):
            self.assertEqual(_handle_run(args, config), 0)
            self.assertEqual(digest.call_count, 1)
            self.assertEqual(_handle_run(args, config), 0)
            self.assertEqual(digest.call_count, 1)
            write.assert_not_called()
            self.assertEqual(box_scan.call_count, 2)
            before = self.database.read_bytes()
            args.dry_run = True
            self.assertEqual(_handle_run(args, config), 0)
            self.assertEqual(digest.call_count, 2)
            self.assertEqual(self.database.read_bytes(), before)

    def test_v7_migration_preserves_baseline(self):
        local = scan_local(self.root, hash_files=True)
        remote = [InventoryItem('.', 'folder', content_id='0'),
                  InventoryItem('a.txt', 'file', size=5, content_id='10', sha1=local[1].sha1)]
        replace_baseline(self.database, local_root=str(self.root), box_root_id='0',
                         local_items=local, box_items=remote)
        before = load_baseline(self.database)
        with closing(sqlite3.connect(self.database)) as connection, connection:
            connection.execute('DROP TABLE local_hash_cache')
            connection.execute('PRAGMA user_version=7')
        initialize_database(self.database)
        self.assertEqual(load_baseline(self.database), before)
        self.assertEqual(self.rows(), [])

    def test_final_verification_hashes_even_after_cached_preflight(self):
        local = scan_local(self.root, hash_files=True)
        remote = [InventoryItem('.', 'folder', content_id='0'),
                  InventoryItem('a.txt', 'file', size=5, content_id='10',
                                version_id='1', sha1=local[1].sha1)]
        config = AppConfig(self.root, '0', self.database, self.root.parent / 'sync.log', (), ())
        generation = replace_baseline(self.database, local_root=str(self.root), box_root_id='0',
                                      local_items=local, box_items=remote)
        # Identical edits yield an empty plan, but must commit a fresh baseline.
        self.file.write_bytes(b'other')
        remote[1] = InventoryItem('a.txt', 'file', size=5, content_id='10',
                                 version_id='2', sha1=hashlib.sha1(b'other').hexdigest())
        args = SimpleNamespace(dry_run=False, initial_download_from_box=False,
                               summary_only=True, json=False, limit=None)
        with patch('sync_box.box_auth.build_authenticated_client'), \
             patch('sync_box.box_auth.build_write_authenticated_client'), \
             patch('sync_box.cli.scan_box', return_value=remote) as box_scan, \
             patch('sync_box.local_inventory.hashlib.file_digest', wraps=hashlib.file_digest) as digest, \
             redirect_stdout(StringIO()):
            self.assertEqual(_handle_run(args, config), 0)
        self.assertEqual(box_scan.call_count, 3)
        self.assertEqual(digest.call_count, 2)  # Initial hash + forced final verification.
        self.assertGreater(load_baseline(self.database)[0], generation)


if __name__ == '__main__':
    unittest.main()
