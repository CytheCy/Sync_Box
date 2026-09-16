from __future__ import annotations

import hashlib
from io import BytesIO
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import traceback
import unittest
from unittest.mock import Mock, patch

from box_sdk_gen.box.errors import BoxSDKError

from sync_box.initial_download import InitialDownloadError, execute_initial_download
from sync_box.inventory import InventoryItem
from sync_box.local_inventory import scan_local
from sync_box.planner import InitialDownloadItem
from sync_box.planner import build_initial_download_plan


def item(
    path: str,
    action: str,
    *,
    content: bytes = b"",
    item_id: str | None = None,
    version_id: str | None = None,
    sha1: str | None = None,
) -> InitialDownloadItem:
    return InitialDownloadItem(
        relative_path=path,
        action=action,
        size=(
            len(content)
            if action in ("download_file", "skip_verified_file")
            else None
        ),
        box_item_id=item_id,
        box_version_id=version_id,
        box_sha1=sha1,
    )


class FakeDownloads:
    def __init__(self, content: dict[str, bytes]) -> None:
        self.content = content
        self.calls: list[tuple[str, str | None]] = []

    def download_file(self, file_id: str, *, version: str | None) -> BytesIO:
        self.calls.append((file_id, version))
        return BytesIO(self.content[file_id])


class FailingDownloads:
    def download_file(self, file_id: str, *, version: str | None) -> BytesIO:
        del file_id, version
        error = RuntimeError("Authorization: Bearer do-not-disclose")
        error.response_info = SimpleNamespace(  # type: ignore[attr-defined]
            status_code=403,
            code="access_denied_insufficient_permissions",
            body={"message": "Access denied - insufficient permission"},
            request_id="safe-request-id",
        )
        raise error


def expired_token_error() -> BoxSDKError:
    return BoxSDKError(
        message="Developer token has expired. Please provide a new one."
    )


class InitialDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_downloads_version_and_verifies_before_publishing(self) -> None:
        content = b"verified content"
        downloads = FakeDownloads({"11": content})
        plan = [
            item("Docs", "create_folder"),
            item(
                "Docs/readme.txt",
                "download_file",
                content=content,
                item_id="11",
                version_id="99",
                sha1=hashlib.sha1(content).hexdigest(),
            ),
        ]

        result = execute_initial_download(
            SimpleNamespace(downloads=downloads), self.root, plan
        )

        self.assertEqual((self.root / "Docs/readme.txt").read_bytes(), content)
        self.assertEqual(downloads.calls, [("11", "99")])
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.already_verified, 0)
        self.assertEqual(result.failed_conflicting, 0)
        self.assertEqual(result.folders_created, 1)
        self.assertEqual(result.bytes, len(content))
        self.assertEqual(result.verified_sha1, 1)
        self.assertEqual(list((self.root / "Docs").glob(".sync-box-download-*")), [])

    def test_existing_file_is_never_opened_or_overwritten(self) -> None:
        destination = self.root / "existing.txt"
        destination.write_bytes(b"keep me")
        downloads = FakeDownloads({"11": b"replacement"})
        plan = [
            item("NewFolder", "create_folder"),
            item(
                "existing.txt",
                "download_file",
                content=b"replacement",
                item_id="11",
            )
        ]

        with self.assertRaisesRegex(InitialDownloadError, "refusing to overwrite"):
            execute_initial_download(
                SimpleNamespace(downloads=downloads), self.root, plan
            )

        self.assertEqual(destination.read_bytes(), b"keep me")
        self.assertFalse((self.root / "NewFolder").exists())
        self.assertEqual(downloads.calls, [])

    def test_size_failure_removes_temporary_file(self) -> None:
        downloads = FakeDownloads({"11": b"short"})
        planned = item("sized.bin", "download_file", content=b"longer", item_id="11")

        with self.assertRaisesRegex(InitialDownloadError, "Size verification failed"):
            execute_initial_download(
                SimpleNamespace(downloads=downloads), self.root, [planned]
            )

        self.assertFalse((self.root / "sized.bin").exists())
        self.assertEqual(list(self.root.glob(".sync-box-download-*")), [])

    def test_box_failure_includes_sanitized_api_detail(self) -> None:
        planned = item("failed.bin", "download_file", item_id="11")
        refresh = Mock()

        with self.assertRaises(InitialDownloadError) as raised:
            execute_initial_download(
                SimpleNamespace(downloads=FailingDownloads()),
                self.root,
                [planned],
                refresh_client=refresh,
            )

        message = str(raised.exception)
        self.assertIn("status='403'", message)
        self.assertIn("code='access_denied_insufficient_permissions'", message)
        self.assertIn("request_id='safe-request-id'", message)
        self.assertNotIn("do-not-disclose", message)
        refresh.assert_not_called()
        self.assertFalse((self.root / "failed.bin").exists())
        self.assertEqual(list(self.root.glob(".sync-box-download-*")), [])

    def test_expired_token_refreshes_client_and_retries_interrupted_file(self) -> None:
        first = b"already completed"
        second = b"retried safely"
        third = b"uses refreshed client"
        plan = [
            item("1-first.txt", "download_file", content=first, item_id="11"),
            item("2-second.txt", "download_file", content=second, item_id="12"),
            item("3-third.txt", "download_file", content=third, item_id="13"),
        ]

        class ExpiringDownloads(FakeDownloads):
            def download_file(self, file_id: str, *, version: str | None) -> BytesIO:
                self.calls.append((file_id, version))
                if file_id == "12":
                    raise expired_token_error()
                return BytesIO(self.content[file_id])

        original_downloads = ExpiringDownloads({"11": first})
        refreshed_downloads = FakeDownloads({"12": second, "13": third})
        refresh = Mock(
            return_value=SimpleNamespace(downloads=refreshed_downloads)
        )

        result = execute_initial_download(
            SimpleNamespace(downloads=original_downloads),
            self.root,
            plan,
            refresh_client=refresh,
        )

        refresh.assert_called_once_with()
        self.assertEqual(original_downloads.calls, [("11", None), ("12", None)])
        self.assertEqual(refreshed_downloads.calls, [("12", None), ("13", None)])
        self.assertEqual(result.downloaded, 3)
        self.assertEqual((self.root / "1-first.txt").read_bytes(), first)
        self.assertEqual((self.root / "2-second.txt").read_bytes(), second)
        self.assertEqual((self.root / "3-third.txt").read_bytes(), third)

    def test_expired_token_retry_is_bounded_to_one_refresh_per_file(self) -> None:
        class AlwaysExpiredDownloads:
            def __init__(self) -> None:
                self.calls = 0

            def download_file(
                self, file_id: str, *, version: str | None
            ) -> BytesIO:
                del file_id, version
                self.calls += 1
                raise expired_token_error()

        original = AlwaysExpiredDownloads()
        replacement = AlwaysExpiredDownloads()
        refresh = Mock(return_value=SimpleNamespace(downloads=replacement))

        with self.assertRaisesRegex(
            InitialDownloadError, "Developer token has expired"
        ):
            execute_initial_download(
                SimpleNamespace(downloads=original),
                self.root,
                [item("bounded.txt", "download_file", item_id="11")],
                refresh_client=refresh,
            )

        refresh.assert_called_once_with()
        self.assertEqual(original.calls, 1)
        self.assertEqual(replacement.calls, 1)
        self.assertFalse((self.root / "bounded.txt").exists())
        self.assertEqual(list(self.root.glob(".sync-box-download-*")), [])

    def test_refresh_failure_does_not_disclose_credentials(self) -> None:
        class ExpiredDownloads:
            def download_file(
                self, file_id: str, *, version: str | None
            ) -> BytesIO:
                del file_id, version
                raise expired_token_error()

        secret = "super-secret-access-token"
        refresh = Mock(
            side_effect=RuntimeError(
                f"access_token={secret} Authorization: Bearer {secret}"
            )
        )

        with self.assertRaises(InitialDownloadError) as raised:
            execute_initial_download(
                SimpleNamespace(downloads=ExpiredDownloads()),
                self.root,
                [item("secret.txt", "download_file", item_id="11")],
                refresh_client=refresh,
            )

        self.assertEqual(
            str(raised.exception),
            "Box download authentication refresh failed for 'secret.txt'",
        )
        self.assertNotIn(secret, str(raised.exception))
        rendered_traceback = "".join(
            traceback.format_exception(raised.exception)
        )
        self.assertNotIn(secret, rendered_traceback)
        refresh.assert_called_once_with()

    def test_sha1_failure_removes_temporary_file_and_stops(self) -> None:
        downloads = FakeDownloads({"11": b"wrong", "12": b"later"})
        plan = [
            item(
                "first.txt",
                "download_file",
                content=b"wrong",
                item_id="11",
                sha1=hashlib.sha1(b"expected").hexdigest(),
            ),
            item("later.txt", "download_file", content=b"later", item_id="12"),
        ]

        with self.assertRaisesRegex(InitialDownloadError, "SHA-1 verification failed"):
            execute_initial_download(
                SimpleNamespace(downloads=downloads), self.root, plan
            )

        self.assertFalse((self.root / "first.txt").exists())
        self.assertFalse((self.root / "later.txt").exists())
        self.assertEqual(downloads.calls, [("11", None)])
        self.assertEqual(list(self.root.glob(".sync-box-download-*")), [])

    def test_atomic_publish_refuses_a_racing_destination(self) -> None:
        content = b"box bytes"
        downloads = FakeDownloads({"11": content})
        destination = self.root / "race.txt"
        real_link = os.link

        def create_conflict_then_link(source: Path, target: Path, **kwargs: object) -> None:
            destination.write_bytes(b"local winner")
            real_link(source, target, **kwargs)

        plan = [item("race.txt", "download_file", content=content, item_id="11")]
        with (
            patch("sync_box.initial_download.os.link", side_effect=create_conflict_then_link),
            self.assertRaisesRegex(InitialDownloadError, "refusing to overwrite"),
        ):
            execute_initial_download(
                SimpleNamespace(downloads=downloads), self.root, plan
            )

        self.assertEqual(destination.read_bytes(), b"local winner")
        self.assertEqual(list(self.root.glob(".sync-box-download-*")), [])

    def test_unsupported_item_stops_before_any_local_change(self) -> None:
        plan = [
            item("Folder", "create_folder"),
            item("shortcut", "skip_unsupported"),
        ]

        with self.assertRaisesRegex(InitialDownloadError, "Unsupported Box item"):
            execute_initial_download(SimpleNamespace(), self.root, plan)

        self.assertEqual(list(self.root.iterdir()), [])

    def test_resume_reuses_safe_expected_directory(self) -> None:
        docs = self.root / "Docs"
        docs.mkdir()
        content = b"new"
        plan = [
            item("Docs", "reuse_folder"),
            item("Docs/new.txt", "download_file", content=content, item_id="11"),
        ]
        downloads = FakeDownloads({"11": content})

        result = execute_initial_download(
            SimpleNamespace(downloads=downloads), self.root, plan
        )

        self.assertEqual(result.folders_reused, 1)
        self.assertEqual(result.folders_created, 0)
        self.assertEqual((docs / "new.txt").read_bytes(), content)

    def test_resume_rechecks_existing_file_before_any_new_local_change(self) -> None:
        existing = self.root / "done.txt"
        existing.write_bytes(b"changed")
        expected = b"expected"
        plan = [
            item("NewFolder", "create_folder"),
            item(
                "done.txt",
                "skip_verified_file",
                content=expected,
                item_id="11",
                sha1=hashlib.sha1(expected).hexdigest(),
            ),
        ]

        with self.assertRaisesRegex(InitialDownloadError, "size differs") as raised:
            execute_initial_download(SimpleNamespace(), self.root, plan)

        self.assertEqual(raised.exception.result.failed_conflicting, 1)
        self.assertFalse((self.root / "NewFolder").exists())
        self.assertEqual(existing.read_bytes(), b"changed")

    def test_interrupted_download_can_resume_without_redownloading_completed_file(
        self,
    ) -> None:
        first = b"first file"
        second = b"second file"
        box_items = [
            InventoryItem(".", "folder"),
            InventoryItem(
                "first.txt",
                "file",
                size=len(first),
                content_id="11",
                version_id="101",
                sha1=hashlib.sha1(first).hexdigest(),
            ),
            InventoryItem(
                "second.txt",
                "file",
                size=len(second),
                content_id="12",
                version_id="102",
                sha1=hashlib.sha1(second).hexdigest(),
            ),
        ]
        first_plan = build_initial_download_plan(
            scan_local(self.root, hash_files=True), box_items
        )

        class InterruptedDownloads(FakeDownloads):
            def download_file(self, file_id: str, *, version: str | None) -> BytesIO:
                if file_id == "12":
                    self.calls.append((file_id, version))
                    raise ConnectionError("interrupted")
                return super().download_file(file_id, version=version)

        interrupted = InterruptedDownloads({"11": first})
        with self.assertRaises(InitialDownloadError) as raised:
            execute_initial_download(
                SimpleNamespace(downloads=interrupted), self.root, first_plan
            )

        self.assertEqual(raised.exception.result.downloaded, 1)
        self.assertEqual(raised.exception.result.failed_conflicting, 1)
        self.assertEqual((self.root / "first.txt").read_bytes(), first)
        self.assertFalse((self.root / "second.txt").exists())

        resumed_plan = build_initial_download_plan(
            scan_local(self.root, hash_files=True), box_items
        )
        actions = {entry.relative_path: entry.action for entry in resumed_plan}
        self.assertEqual(actions["first.txt"], "skip_verified_file")
        self.assertEqual(actions["second.txt"], "download_file")

        resumed = FakeDownloads({"12": second})
        result = execute_initial_download(
            SimpleNamespace(downloads=resumed), self.root, resumed_plan
        )

        self.assertEqual(resumed.calls, [("12", "102")])
        self.assertEqual(result.downloaded, 1)
        self.assertEqual(result.already_verified, 1)
        self.assertEqual(result.failed_conflicting, 0)
        self.assertEqual((self.root / "first.txt").read_bytes(), first)
        self.assertEqual((self.root / "second.txt").read_bytes(), second)


if __name__ == "__main__":
    unittest.main()
