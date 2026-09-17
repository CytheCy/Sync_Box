from __future__ import annotations

from contextlib import closing
from io import BytesIO
import hashlib
from pathlib import Path, PurePosixPath
import sqlite3
import tempfile
from unittest.mock import patch
import unittest

from box_sdk_gen.box.errors import BoxSDKError

from sync_box.conflict_resolution import (
    ConflictResolutionError,
    ConflictResolutionPolicy,
    RESOLUTION_STEPS,
    build_conflict_resolution_plan,
    deterministic_conflict_copy_path,
    execute_conflict_resolution,
)
from sync_box.database import load_baseline, load_incomplete_resolution, replace_baseline
from sync_box.inventory import InventoryItem
from sync_box.local_inventory import scan_local
from sync_box.sync_engine import SyncExecutionError, execute_sync


BASE = b"base\n"
LOCAL = b"local divergent\n"
REMOTE = b"Box divergent\n"


def sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


class RemoteState:
    def __init__(self) -> None:
        self.files: dict[str, dict[str, object]] = {
            "canary.txt": {
                "id": "10",
                "version": "1",
                "etag": "1",
                "content": BASE,
            }
        }
        self.next_id = 20
        self.upload_calls = 0
        self.download_calls = 0
        self.upload_behavior = "normal"
        self.download_behavior = "normal"

    def change_original(self, content: bytes, *, version: str = "2", etag: str = "2") -> None:
        self.files["canary.txt"].update(
            content=content, version=version, etag=etag
        )

    def inventory(self) -> list[InventoryItem]:
        result = [InventoryItem(".", "folder", content_id="0")]
        for path, record in sorted(self.files.items()):
            content = record["content"]
            assert isinstance(content, bytes)
            result.append(
                InventoryItem(
                    path,
                    "file",
                    size=len(content),
                    content_id=str(record["id"]),
                    version_id=str(record["version"]),
                    etag=str(record["etag"]),
                    sha1=sha(content),
                )
            )
        return result

    def create(self, name: str, content: bytes) -> None:
        if name in self.files:
            raise RuntimeError("destination exists")
        actual = content + b"corrupt" if self.upload_behavior == "corrupt" else content
        self.files[name] = {
            "id": str(self.next_id),
            "version": "1",
            "etag": "0",
            "content": actual,
        }
        self.next_id += 1


class FakeBox:
    root_folder_id = "0"

    def __init__(self, state: RemoteState, *, expire_download: bool = False) -> None:
        self.state = state
        self.expire_download = expire_download

    def upload_new(self, parent_id, name, source, expected_sha1):
        self.state.upload_calls += 1
        data = source.read()
        if sha(data) != expected_sha1:
            raise RuntimeError("source checksum mismatch")
        if self.state.upload_behavior == "raise_before":
            raise RuntimeError("ambiguous upload")
        self.state.create(name, data)
        if self.state.upload_behavior == "raise_after":
            raise RuntimeError("ambiguous upload")

    def download(self, item_id, version_id):
        self.state.download_calls += 1
        if self.expire_download:
            self.expire_download = False
            raise BoxSDKError(
                message="Developer token has expired. Please provide a new one."
            )
        for record in self.state.files.values():
            if str(record["id"]) == item_id and str(record["version"]) == version_id:
                content = record["content"]
                assert isinstance(content, bytes)
                if self.state.download_behavior == "corrupt":
                    content += b"corrupt"
                return BytesIO(content)
        raise RuntimeError("missing version")


class ResolutionHarness:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.root.mkdir()
        self.db = Path(self.temporary.name) / "state.sqlite3"
        self.path = self.root / "canary.txt"
        self.path.write_bytes(BASE)
        self.remote = RemoteState()
        self.generation = replace_baseline(
            self.db,
            local_root=str(self.root),
            box_root_id="0",
            local_items=scan_local(self.root, hash_files=True),
            box_items=self.remote.inventory(),
        )
        self.path.write_bytes(LOCAL)
        self.remote.change_original(REMOTE)

    def close(self) -> None:
        self.temporary.cleanup()

    def inventory(self):
        return scan_local(self.root, hash_files=True), self.remote.inventory()

    def plan(self):
        baseline = load_baseline(self.db)
        assert baseline is not None
        local_items, box_items = self.inventory()
        return build_conflict_resolution_plan(
            policy=ConflictResolutionPolicy.KEEP_BOX_AT_ORIGINAL,
            expected_baseline_generation=self.generation,
            baseline_generation=baseline[0],
            baseline_pairs=baseline[3],
            local_items=local_items,
            box_items=box_items,
            conflict_path="canary.txt",
        )

    def execute(self, plan=None, **kwargs):
        return execute_conflict_resolution(
            kwargs.pop("box", FakeBox(self.remote)),
            self.root,
            self.db,
            plan or self.plan(),
            inventory_provider=self.inventory,
            **kwargs,
        )


class ConflictResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = ResolutionHarness()

    def tearDown(self) -> None:
        self.h.close()

    def assert_baseline_unchanged(self) -> None:
        self.assertEqual(load_baseline(self.h.db)[0], self.h.generation)

    def test_successful_keep_both_resolution_and_baseline_advance(self) -> None:
        plan = self.h.plan()
        result = self.h.execute(plan)

        self.assertEqual(result.status, "completed")
        self.assertEqual(result.completed_steps, RESOLUTION_STEPS)
        self.assertEqual(result.baseline_generation_before, self.h.generation)
        self.assertEqual(result.baseline_generation_after, self.h.generation + 1)
        self.assertEqual(self.h.path.read_bytes(), REMOTE)
        self.assertEqual((self.h.root / plan.conflict_copy_path).read_bytes(), LOCAL)
        self.assertEqual(self.h.remote.files["canary.txt"]["content"], REMOTE)
        self.assertEqual(self.h.remote.files[plan.conflict_copy_path]["content"], LOCAL)
        baseline = load_baseline(self.h.db)
        self.assertEqual(baseline[0], self.h.generation + 1)
        self.assertEqual(self.h.remote.upload_calls, 1)

    def test_structured_plan_and_deterministic_safe_name(self) -> None:
        first = self.h.plan()
        second = self.h.plan()
        self.assertEqual(first, second)
        self.assertEqual(first.resolution_key, second.resolution_key)
        self.assertEqual(
            first.conflict_copy_path,
            f"canary (local conflict {sha(LOCAL)[:12]}).txt",
        )
        long_name = "é" * 200 + ".txt"
        generated = deterministic_conflict_copy_path(
            long_name, source="local", sha1=sha(LOCAL)
        )
        self.assertLessEqual(len(PurePosixPath(generated).name.encode("utf-8")), 255)
        self.assertEqual(first.to_dict()["policy"], "keep_box_at_original")

    def test_local_fingerprint_change_before_execution_is_refused(self) -> None:
        plan = self.h.plan()
        self.h.path.write_bytes(b"changed again\n")
        with self.assertRaises(ConflictResolutionError):
            self.h.execute(plan)
        self.assertIsNone(load_incomplete_resolution(self.h.db))
        self.assert_baseline_unchanged()
        self.assertEqual(self.h.remote.upload_calls, 0)

    def test_box_fingerprint_etag_or_version_change_is_refused(self) -> None:
        for version, etag in (("3", "2"), ("2", "3")):
            with self.subTest(version=version, etag=etag):
                h = ResolutionHarness()
                try:
                    plan = h.plan()
                    h.remote.change_original(REMOTE, version=version, etag=etag)
                    with self.assertRaises(ConflictResolutionError):
                        h.execute(plan)
                    self.assertEqual(load_baseline(h.db)[0], h.generation)
                    self.assertEqual(h.remote.upload_calls, 0)
                finally:
                    h.close()

    def test_baseline_generation_mismatch_is_refused(self) -> None:
        plan = self.h.plan()
        replace_baseline(
            self.h.db,
            local_root=str(self.h.root),
            box_root_id="0",
            local_items=scan_local(self.h.root, hash_files=True),
            box_items=self.h.remote.inventory(),
        )
        with self.assertRaisesRegex(ConflictResolutionError, "Baseline generation changed"):
            self.h.execute(plan)
        self.assertEqual(self.h.remote.upload_calls, 0)

    def test_conflict_copy_destination_existing_locally_is_refused(self) -> None:
        destination = deterministic_conflict_copy_path(
            "canary.txt", source="local", sha1=sha(LOCAL)
        )
        (self.h.root / destination).write_bytes(b"occupied")
        with self.assertRaisesRegex(ConflictResolutionError, "already exists locally"):
            self.h.plan()
        self.assert_baseline_unchanged()

    def test_conflict_copy_destination_existing_on_box_is_refused(self) -> None:
        destination = deterministic_conflict_copy_path(
            "canary.txt", source="local", sha1=sha(LOCAL)
        )
        self.h.remote.create(destination, b"occupied")
        with self.assertRaisesRegex(ConflictResolutionError, "already exists on Box"):
            self.h.plan()
        self.assert_baseline_unchanged()

    def test_interruption_after_each_step_and_safe_resume(self) -> None:
        for interrupted_step in RESOLUTION_STEPS:
            with self.subTest(step=interrupted_step):
                h = ResolutionHarness()
                try:
                    plan = h.plan()

                    def interrupt(step):
                        if step == interrupted_step:
                            raise RuntimeError("injected interruption")

                    with self.assertRaises(ConflictResolutionError) as raised:
                        h.execute(plan, interruption_hook=interrupt)
                    if interrupted_step == RESOLUTION_STEPS[-1]:
                        self.assertEqual(raised.exception.result.status, "completed")
                        self.assertEqual(load_baseline(h.db)[0], h.generation + 1)
                    else:
                        self.assertEqual(raised.exception.result.status, "incomplete")
                        self.assertEqual(load_baseline(h.db)[0], h.generation)
                    resumed = h.execute(plan)
                    self.assertEqual(resumed.status, "completed")
                    self.assertEqual(load_baseline(h.db)[0], h.generation + 1)
                    self.assertEqual(h.remote.upload_calls, 1)
                finally:
                    h.close()

    def test_ambiguous_upload_after_commit_is_reconciled_without_retry(self) -> None:
        self.h.remote.upload_behavior = "raise_after"
        result = self.h.execute()
        self.assertEqual(result.status, "completed")
        self.assertEqual(self.h.remote.upload_calls, 1)

    def test_ambiguous_upload_without_visible_copy_is_not_retried(self) -> None:
        plan = self.h.plan()
        self.h.remote.upload_behavior = "raise_before"
        with self.assertRaisesRegex(ConflictResolutionError, "ambiguous"):
            self.h.execute(plan)
        self.assertEqual(self.h.remote.upload_calls, 1)
        self.assert_baseline_unchanged()
        with self.assertRaisesRegex(ConflictResolutionError, "refusing automatic retry"):
            self.h.execute(plan)
        self.assertEqual(self.h.remote.upload_calls, 1)
        self.assert_baseline_unchanged()

    def test_upload_checksum_or_size_verification_failure_keeps_baseline(self) -> None:
        self.h.remote.upload_behavior = "corrupt"
        with self.assertRaisesRegex(ConflictResolutionError, "size/SHA-1"):
            self.h.execute()
        self.assert_baseline_unchanged()
        self.assertEqual(self.h.path.read_bytes(), LOCAL)

    def test_download_checksum_failure_keeps_baseline_and_original(self) -> None:
        self.h.remote.download_behavior = "corrupt"
        with self.assertRaises(ConflictResolutionError):
            self.h.execute()
        self.assert_baseline_unchanged()
        self.assertEqual(self.h.path.read_bytes(), LOCAL)

    def test_local_filesystem_error_and_no_clobber_keep_baseline(self) -> None:
        plan = self.h.plan()
        destination = self.h.root / plan.conflict_copy_path

        def race(source, target, **kwargs):
            destination.write_bytes(b"racer")
            raise FileExistsError("racing destination")

        with patch("sync_box.conflict_resolution.os.link", side_effect=race):
            with self.assertRaises(ConflictResolutionError):
                self.h.execute(plan)
        self.assertEqual(destination.read_bytes(), b"racer")
        self.assertEqual(self.h.path.read_bytes(), LOCAL)
        self.assert_baseline_unchanged()

    def test_unsafe_path_and_symlink_are_refused(self) -> None:
        baseline = load_baseline(self.h.db)
        local_items, box_items = self.h.inventory()
        with self.assertRaises(ConflictResolutionError):
            build_conflict_resolution_plan(
                policy=ConflictResolutionPolicy.KEEP_BOX_AT_ORIGINAL,
                expected_baseline_generation=self.h.generation,
                baseline_generation=baseline[0],
                baseline_pairs=baseline[3],
                local_items=local_items,
                box_items=box_items,
                conflict_path="../canary.txt",
            )
        plan = self.h.plan()
        self.h.path.unlink()
        self.h.path.symlink_to(Path(self.h.temporary.name) / "outside")
        with self.assertRaises(ConflictResolutionError):
            self.h.execute(plan)
        self.assert_baseline_unchanged()

    def test_expired_download_token_recovers_but_upload_is_not_retried(self) -> None:
        refreshes = []

        def refresh():
            refreshes.append(True)
            return FakeBox(self.h.remote)

        result = self.h.execute(
            box=FakeBox(self.h.remote, expire_download=True), refresh_box=refresh
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(refreshes), 1)
        self.assertEqual(self.h.remote.upload_calls, 1)

    def test_ordinary_sync_refuses_while_resolution_is_incomplete(self) -> None:
        plan = self.h.plan()

        def interrupt(step):
            if step == RESOLUTION_STEPS[0]:
                raise RuntimeError("stop")

        with self.assertRaises(ConflictResolutionError):
            self.h.execute(plan, interruption_hook=interrupt)
        with self.assertRaisesRegex(SyncExecutionError, "resolution.*incomplete"):
            execute_sync(
                FakeBox(self.h.remote), self.h.root, self.h.db, [],
                box_items_by_path={},
            )
        self.assert_baseline_unchanged()

    def test_direct_baseline_replacement_is_refused_while_incomplete(self) -> None:
        plan = self.h.plan()

        def interrupt(step):
            if step == RESOLUTION_STEPS[0]:
                raise RuntimeError("stop")

        with self.assertRaises(ConflictResolutionError):
            self.h.execute(plan, interruption_hook=interrupt)
        local_items, box_items = self.h.inventory()
        with self.assertRaisesRegex(RuntimeError, "resolution.*incomplete"):
            replace_baseline(
                self.h.db,
                local_root=str(self.h.root),
                box_root_id="0",
                local_items=local_items,
                box_items=box_items,
            )
        self.assert_baseline_unchanged()

    def test_atomic_baseline_and_completion_roll_back_together(self) -> None:
        with closing(sqlite3.connect(self.h.db)) as connection:
            with connection:
                connection.execute(
                    """
                    CREATE TRIGGER reject_resolution_completion
                    BEFORE UPDATE OF outcome ON conflict_resolution_runs
                    WHEN NEW.outcome='completed'
                    BEGIN SELECT RAISE(ABORT, 'stop'); END
                    """
                )
        with self.assertRaises(ConflictResolutionError):
            self.h.execute()
        self.assert_baseline_unchanged()
        incomplete = load_incomplete_resolution(self.h.db)
        self.assertIsNotNone(incomplete)
        self.assertEqual(incomplete["outcome"], "in_progress")
        statuses = {item["operation"]: item["status"] for item in incomplete["operations"]}
        self.assertEqual(statuses[RESOLUTION_STEPS[-1]], "started")
if __name__ == "__main__":
    unittest.main()
