from __future__ import annotations

import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from sync_box.app_status import AuthenticationState, DatabaseState, SystemdState, UnitState
from sync_box.database import load_baseline
from sync_box.dependencies import BoxCliManager, BoxCliStatus, DependencyError
from sync_box.initial_download import InitialDownloadError
from sync_box.inventory import InventoryItem
from sync_box.local_inventory import scan_local
from sync_box.requirements import FolderKind, RequirementStatus, inspect_folder
from sync_box.setup_config import create_initial_config
from sync_box.setup_controller import SetupController, SetupStep


def box_item(path: str, kind: str, *, data: bytes = b"", content_id: str = "0") -> InventoryItem:
    return InventoryItem(
        path, kind, size=len(data) if kind == "file" else None,
        content_id=content_id, version_id="v1" if kind == "file" else None,
        sha1=hashlib.sha1(data).hexdigest() if kind == "file" else None,
    )


class FakeDownloads:
    def __init__(self, content: dict[str, bytes]) -> None:
        self.content = content

    def download_file(self, item_id: str, *, version: str | None):
        del version
        return BytesIO(self.content[item_id])


class FirstRunControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.local = self.base / "Box"
        self.config = self.base / "config" / "config.toml"
        self.state = self.base / "state"
        self.systemd = Mock()
        self.systemd.snapshot.return_value = SystemdState(
            UnitState(load_state="loaded"),
            UnitState(load_state="loaded", active_state="inactive", unit_file_state="disabled"),
        )
        self.dependencies = Mock()
        self.dependencies.status.return_value = BoxCliStatus(True, True, Path("/usr/bin/box"), (4, 9, 2))
        self.controller = SetupController(
            self.config, dependencies=self.dependencies, systemd=self.systemd
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def configure(self) -> None:
        self.local.mkdir(exist_ok=True)
        create_initial_config(
            self.local, config_path=self.config, state_directory=self.state,
            box_folder_id="0",
        )

    def test_completely_fresh_install_and_folder_creation(self) -> None:
        kind = self.controller.prepare_local_folder(self.local, create=True)
        self.assertEqual(kind, FolderKind.EMPTY)
        self.assertTrue(self.local.is_dir())
        self.assertEqual(self.local.stat().st_uid, os.geteuid())
        self.assertEqual(self.config.stat().st_uid, os.geteuid())
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)

    def test_authentication_success_failure_and_interruption(self) -> None:
        with patch("sync_box.setup_controller.authorize") as authorize, patch(
            "sync_box.setup_controller.test_authentication", return_value=("1", "Person")
        ):
            self.assertEqual(self.controller.authenticate(), ("1", "Person"))
            authorize.assert_called_once_with(reauthorize=False)
        with patch("sync_box.setup_controller.authorize", side_effect=RuntimeError("cancelled")):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self.controller.authenticate()

    def test_empty_identical_and_differing_folders_are_classified_read_only(self) -> None:
        self.configure()
        root = box_item(".", "folder")
        remote_file = box_item("a.txt", "file", data=b"remote", content_id="1")
        with patch.object(self.controller, "_fresh_inventories", return_value=([root], [root, remote_file])):
            analysis = self.controller.analyze()
            self.assertTrue(analysis.local_empty)
            self.assertFalse(analysis.identical)

        same_local = InventoryItem("a.txt", "file", size=6, content_id="dev:ino", sha1=remote_file.sha1)
        with patch.object(self.controller, "_fresh_inventories", return_value=([root, same_local], [root, remote_file])):
            analysis = self.controller.analyze()
            self.assertTrue(analysis.identical)
            self.assertEqual(analysis.differences["review"], 0)

        changed = InventoryItem("a.txt", "file", size=5, content_id="dev:ino", sha1=hashlib.sha1(b"other").hexdigest())
        with patch.object(self.controller, "_fresh_inventories", return_value=([root, changed], [root, remote_file])):
            analysis = self.controller.analyze()
            self.assertFalse(analysis.identical)
            self.assertEqual(analysis.differences["content_mismatch"], 1)
        self.assertFalse(self.state.exists())

    def test_unsafe_symlink_folder_is_refused(self) -> None:
        target = self.base / "real"
        target.mkdir()
        link = self.base / "link"
        link.symlink_to(target, target_is_directory=True)
        self.assertEqual(inspect_folder(link)[0], FolderKind.UNSAFE)
        with self.assertRaisesRegex(Exception, "symbolic"):
            self.controller.prepare_local_folder(link)

    def test_fresh_bootstrap_verifies_and_establishes_baseline(self) -> None:
        self.configure()
        data = b"verified"
        remote = [box_item(".", "folder"), box_item("file.txt", "file", data=data, content_id="11")]
        client = Mock(downloads=FakeDownloads({"11": data}))

        def scan(_config, _client):
            return scan_local(self.local, hash_files=True), remote

        with patch("sync_box.setup_controller.build_authenticated_client", return_value=client), patch.object(
            self.controller, "_scan", side_effect=scan
        ):
            result, generation = self.controller.bootstrap_from_box()

        self.assertEqual(result.downloaded, 1)
        self.assertEqual((self.local / "file.txt").read_bytes(), data)
        baseline = load_baseline(self.state / "state.sqlite3")
        self.assertIsNotNone(baseline)
        self.assertEqual(baseline[0], generation)

    def test_interrupted_bootstrap_has_no_baseline_and_can_resume(self) -> None:
        self.configure()
        data = b"good"
        remote = [box_item(".", "folder"), box_item("file.txt", "file", data=data, content_id="11")]
        broken = Mock(downloads=FakeDownloads({"11": b"bad"}))
        good = Mock(downloads=FakeDownloads({"11": data}))

        def scan(_config, _client):
            return scan_local(self.local, hash_files=True), remote

        with patch("sync_box.setup_controller.build_authenticated_client", return_value=broken), patch.object(
            self.controller, "_scan", side_effect=scan
        ):
            with self.assertRaises(InitialDownloadError):
                self.controller.bootstrap_from_box()
        self.assertIsNone(load_baseline(self.state / "state.sqlite3"))
        with patch("sync_box.setup_controller.build_authenticated_client", return_value=good), patch.object(
            self.controller, "_scan", side_effect=scan
        ):
            result, _generation = self.controller.bootstrap_from_box()
        self.assertEqual(result.downloaded, 1)
        self.assertIsNotNone(load_baseline(self.state / "state.sqlite3"))

    def test_ambiguous_baseline_is_refused(self) -> None:
        self.configure()
        local = [box_item(".", "folder"), box_item("local.txt", "file", data=b"x", content_id="l")]
        remote = [box_item(".", "folder"), box_item("box.txt", "file", data=b"x", content_id="b")]
        with patch.object(self.controller, "_fresh_inventories", return_value=(local, remote)):
            with self.assertRaisesRegex(Exception, "Baseline refused"):
                self.controller.establish_identical_baseline()
        self.assertIsNone(load_baseline(self.state / "state.sqlite3"))

    def test_timer_requires_baseline_and_verifies_activation(self) -> None:
        self.configure()
        ok, message = self.controller.enable_automatic_sync()
        self.assertFalse(ok)
        self.assertIn("baseline", message)
        self.systemd.set_timer_enabled.assert_not_called()

        root = box_item(".", "folder")
        with patch.object(self.controller, "_fresh_inventories", return_value=([root], [root])):
            self.controller.establish_identical_baseline()
        active = SystemdState(
            UnitState(load_state="loaded"),
            UnitState(load_state="loaded", active_state="active", sub_state="waiting", unit_file_state="enabled"),
        )
        self.systemd.snapshot.side_effect = [
            SystemdState(UnitState(load_state="loaded"), UnitState(load_state="loaded")), active
        ]
        self.systemd.set_timer_enabled.return_value = (True, "enabled")
        with patch.object(self.controller, "test_authentication", return_value=("1", "Person")):
            self.assertEqual(self.controller.enable_automatic_sync(), (True, "enabled"))
        self.systemd.set_timer_enabled.assert_called_once_with(True)

    def test_timer_activation_failure_remains_incomplete(self) -> None:
        self.configure()
        root = box_item(".", "folder")
        with patch.object(self.controller, "_fresh_inventories", return_value=([root], [root])):
            self.controller.establish_identical_baseline()
        self.systemd.set_timer_enabled.return_value = (False, "user manager unavailable")
        with patch.object(self.controller, "test_authentication", return_value=("1", "Person")):
            self.assertEqual(
                self.controller.enable_automatic_sync(), (False, "user manager unavailable")
            )

    def test_setup_resume_is_derived_from_existing_state(self) -> None:
        report = RequirementStatus(
            True, True, True, True,
            BoxCliStatus(True, True, Path("/usr/bin/box"), (4, 9, 2)),
            None, None, None, None, DatabaseState(), self.systemd.snapshot(),
        )
        self.controller.requirements.check = Mock(return_value=report)
        self.assertEqual(self.controller.setup_step(AuthenticationState.CONNECTED), SetupStep.LOCAL_FOLDER)
        self.assertEqual(self.controller.setup_step(AuthenticationState.REQUIRED), SetupStep.AUTH)


class BoxCliDependencyTests(unittest.TestCase):
    def test_already_installed_cli_is_version_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            executable = Path(temporary) / "box"
            executable.touch(mode=0o700)
            runner = Mock(return_value=subprocess.CompletedProcess([], 0, "@box/cli/4.9.2 linux-x64\n", ""))
            with patch("sync_box.dependencies.shutil.which", return_value=str(executable)):
                status = BoxCliManager(runner=runner).status()
            self.assertTrue(status.usable)
            self.assertEqual(status.version, (4, 9, 2))

    @patch("sync_box.dependencies.shutil.which", return_value=None)
    def test_missing_cli_and_installation_failure(self, _which: Mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = BoxCliManager(install_path=Path(temporary) / "box")
            self.assertFalse(manager.status().installed)
            manager._json = Mock(side_effect=DependencyError("network unavailable"))
            with self.assertRaisesRegex(DependencyError, "network unavailable"):
                manager.install()

    @patch("sync_box.dependencies.platform.system", return_value="Linux")
    @patch("sync_box.dependencies.platform.machine", return_value="x86_64")
    @patch("sync_box.dependencies.shutil.which", return_value=None)
    def test_official_archive_digest_is_required(self, _which: Mock, _machine: Mock, _system: Mock) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manager = BoxCliManager(install_path=Path(temporary) / "box")
            manager._json = Mock(return_value={
                "tag_name": "v4.9.2",
                "assets": [{
                    "name": "box-v4.9.2-linux-x64.tar.gz",
                    "browser_download_url": "https://github.com/box/boxcli/releases/download/v4.9.2/box-v4.9.2-linux-x64.tar.gz",
                }],
            })
            with self.assertRaisesRegex(DependencyError, "SHA-256"):
                manager.install()


if __name__ == "__main__":
    unittest.main()
