from dataclasses import replace
from io import BytesIO
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from sync_box.config import AppConfig
from sync_box.database import load_baseline, replace_baseline, upload_recovered_pairs
from sync_box.inventory import InventoryItem
from sync_box.local_inventory import scan_local
from sync_box.sync_engine import execute_sync, SyncRetryNeeded
from sync_box.two_way import build_sync_plan, SyncAction
from sync_box.app_status import read_database_state


def sha(data):
    return hashlib.sha1(data).hexdigest()


class Cloud:
    root_folder_id = "0"

    def __init__(self):
        self.files = {}
        self.history = {}
        self.counter = 0
        self.uploads = 0

    def put(self, path, data):
        self.counter += 1
        previous = self.files.get(path)
        item = InventoryItem(path, "file", content_id=previous[0].content_id if previous else f"id{self.counter}",
                             version_id=str(self.counter), etag=str(self.counter), sha1=sha(data), size=len(data))
        self.files[path] = (item, data)
        self.history[(item.content_id, item.version_id)] = data
        return item

    def inventory(self):
        return [InventoryItem(".", "folder", content_id="0")] + [item for item, data in self.files.values()]

    def upload_version(self, item_id, name, source, etag, digest):
        path, (item, _) = next((path, value) for path, value in self.files.items() if value[0].content_id == item_id)
        if item.etag != etag:
            raise RuntimeError("412 precondition failed")
        data = source.read()
        assert digest == sha(data)
        self.uploads += 1
        return self.put(path, data)

    def upload_new(self, parent, name, source, digest):
        assert parent == "0"
        if name in self.files:
            raise RuntimeError("409 name in use")
        self.uploads += 1
        data = source.read()
        assert digest == sha(data)
        return self.put(name, data)

    def delete(self, kind, item_id, etag):
        path, (item, _) = next((path, value) for path, value in self.files.items() if value[0].content_id == item_id)
        if item.etag != etag:
            raise RuntimeError("412 precondition failed")
        del self.files[path]

    def download(self, item_id, version):
        return BytesIO(self.history[(item_id, version)])


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.root = self.directory / "local"
        self.root.mkdir()
        self.db = self.directory / "state.db"
        self.cloud = Cloud()
        (self.root / "a.txt").write_bytes(b"original")
        self.cloud.put("a.txt", b"original")
        self.generation = replace_baseline(self.db, local_root=str(self.root), box_root_id="0",
            local_items=self.scan(), box_items=self.cloud.inventory())
        self.config = AppConfig(self.root, "0", self.db, self.directory / "sync.log", (), ())

    def scan(self):
        return scan_local(self.root, hash_files=True)

    def inventories(self):
        return self.scan(), self.cloud.inventory()

    def recovered(self):
        gen, root, boxroot, pairs = load_baseline(self.db)
        return upload_recovered_pairs(self.db, gen, root, boxroot, pairs)

    def plan(self):
        return build_sync_plan(self.recovered(), self.scan(), self.cloud.inventory())

    def execute(self, verify=None):
        return execute_sync(self.cloud, self.root, self.db, self.plan(),
            baseline_generation=load_baseline(self.db)[0],
            box_items_by_path={i.relative_path: i for i in self.cloud.inventory()},
            verify_inventories=verify or self.inventories)


class UploadRecoveryTests(Fixture):
    def test_changed_local_after_upload_is_uploaded_again_after_restart(self):
        (self.root / "a.txt").write_bytes(b"first upload")
        def changed_during_verify():
            (self.root / "a.txt").write_bytes(b"new edit")
            return self.inventories()
        with self.assertRaises(SyncRetryNeeded):
            self.execute(changed_during_verify)
        self.assertEqual(load_baseline(self.db)[0], self.generation)
        self.assertEqual(next(l.sha1 for l, r in self.recovered() if l.relative_path == "a.txt"), sha(b"first upload"))
        self.assertEqual([a.action for a in self.plan()], ["upload_version"])
        self.execute()
        self.assertEqual(self.cloud.files["a.txt"][1], b"new edit")
        self.assertGreater(load_baseline(self.db)[0], self.generation)

    def test_external_box_version_is_not_mistaken_for_own_upload(self):
        (self.root / "a.txt").write_bytes(b"first")
        def verify():
            (self.root / "a.txt").write_bytes(b"local next")
            self.cloud.put("a.txt", b"external edit")
            return self.inventories()
        with self.assertRaises(SyncRetryNeeded):
            self.execute(verify)
        self.assertEqual([a.action for a in self.plan()], ["conflict"])
        self.assertEqual(self.cloud.files["a.txt"][1], b"external edit")

    def test_receipt_survives_failure_after_success_before_verification(self):
        (self.root / "a.txt").write_bytes(b"uploaded")
        def fail():
            raise RuntimeError("process stopped")
        with self.assertRaises(RuntimeError):
            self.execute(fail)
        self.assertEqual(self.plan(), [])
        (self.root / "a.txt").write_bytes(b"later")
        self.assertEqual([a.action for a in self.plan()], ["upload_version"])
        self.assertEqual(len(upload_recovered_pairs(self.db, self.generation, "/different", "0", [])), 0)

    def test_new_file_receipt_prevents_different_new_item_conflict(self):
        (self.root / "new.txt").write_bytes(b"first")
        def verify():
            (self.root / "new.txt").write_bytes(b"second")
            return self.inventories()
        with self.assertRaises(SyncRetryNeeded):
            self.execute(verify)
        self.assertEqual([(a.action, a.relative_path) for a in self.plan()], [("upload_version", "new.txt")])
        self.execute()
        self.assertEqual(self.cloud.files["new.txt"][1], b"second")

    def test_preflight_change_records_retry_without_mutations(self):
        with self.assertRaises(SyncRetryNeeded):
            execute_sync(self.cloud, self.root, self.db, [], baseline_generation=self.generation,
                box_items_by_path={}, revalidate_plan=lambda: [SyncAction("upload_new", "new.txt")])
        self.assertEqual(self.cloud.uploads, 0)
        self.assertTrue(read_database_state(self.db).latest_summary.startswith("retry:"))

    def test_cli_retries_only_retryable_failures_after_quiet_period(self):
        from sync_box.cli import _run_with_retries
        with patch("sync_box.cli._handle_run", side_effect=[SyncRetryNeeded("changed"), 0]) as run, \
             patch("sync_box.cli._wait_for_quiet", return_value=True) as quiet:
            self.assertEqual(_run_with_retries(SimpleNamespace(), self.config), 0)
            self.assertEqual(run.call_count, 2)
            quiet.assert_called_once()
        with patch("sync_box.cli._handle_run", side_effect=RuntimeError("conflict")), \
             patch("sync_box.cli._wait_for_quiet") as quiet:
            with self.assertRaises(RuntimeError):
                _run_with_retries(SimpleNamespace(), self.config)
            quiet.assert_not_called()

    def test_changed_file_while_hashing_is_retryable_and_receipt_survives(self):
        from sync_box.inventory import ScanChangedError
        (self.root / "a.txt").write_bytes(b"uploaded")
        def verify():
            raise ScanChangedError("Local file changed while hashing")
        with self.assertRaises(SyncRetryNeeded):
            self.execute(verify)
        self.assertEqual(self.plan(), [])
        self.assertTrue(read_database_state(self.db).latest_summary.startswith("retry:"))

    def test_scan_retry_exhaustion_is_visible_as_pending(self):
        from sync_box.cli import _run_with_retries
        from sync_box.inventory import ScanChangedError
        with patch("sync_box.cli._handle_run", side_effect=ScanChangedError("changed")), \
             patch("sync_box.cli._wait_for_quiet", return_value=False):
            self.assertEqual(_run_with_retries(SimpleNamespace(), self.config), 0)
        self.assertTrue(read_database_state(self.db).latest_summary.startswith("retry:"))

    def test_quiet_period_resets_on_local_changes(self):
        from sync_box.cli import _wait_for_quiet
        clock = [0]
        def tick(seconds):
            clock[0] += seconds
        samples = [[], ["changed"], ["changed"], ["changed"]]
        with patch("sync_box.cli.monotonic", side_effect=lambda: clock[0]), \
             patch("sync_box.cli.sleep", side_effect=tick), \
             patch("sync_box.cli.scan_local", side_effect=samples):
            self.assertTrue(_wait_for_quiet(self.config, quiet_seconds=2, timeout=10))
        self.assertEqual(clock[0], 3)
