from contextlib import closing, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from sync_box.inventory import InventoryItem

from sync_box.cli import main
from sync_box.config import load_config
from sync_box.database import initialize_database


def write_config(path: Path, state_dir: Path, local_root: Path) -> None:
    path.write_text(
        f"""
[local]
root = "{local_root}"
[box]
folder_id = "12345"
[sync]
exclude = [".Trash-1000"]
exclude_names = ["Thumbs.db"]
[storage]
state_database = "{state_dir / 'state.sqlite3'}"
log_file = "{state_dir / 'sync.log'}"
""".strip(),
        encoding="utf-8",
    )


class FoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.temporary_directory.name)
        self.local_root = self.tmp_path / "local"
        self.local_root.mkdir()
        self.git_check = patch("sync_box.config._find_git_root", return_value=None)
        self.git_check.start()

    def tearDown(self) -> None:
        self.git_check.stop()
        self.temporary_directory.cleanup()

    def test_load_config(self) -> None:
        config_path = self.tmp_path / "config.toml"
        state_dir = self.tmp_path / "state"
        write_config(config_path, state_dir, self.local_root)

        config = load_config(config_path)

        self.assertEqual(config.local_root, self.local_root)
        self.assertEqual(config.box_folder_id, "12345")
        self.assertEqual(config.excluded_paths, (".Trash-1000",))
        self.assertEqual(config.excluded_names, ("Thumbs.db",))

    def test_database_initializes_empty_schema(self) -> None:
        database_path = self.tmp_path / "state" / "state.sqlite3"

        initialize_database(database_path)

        with closing(sqlite3.connect(database_path)) as connection:
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0], 4
            )
            for table in (
                "item_baselines",
                "sync_runs",
                "conflicts",
                "inventory_runs",
                "inventory_items",
                "conflict_resolution_runs",
                "conflict_resolution_operations",
            ):
                count = connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0]
                self.assertEqual(count, 0)
        self.assertEqual(database_path.stat().st_mode & 0o777, 0o600)

    def test_dry_run_creates_no_state_or_log_files(self) -> None:
        config_path = self.tmp_path / "config.toml"
        state_dir = self.tmp_path / "state"
        write_config(config_path, state_dir, self.local_root)

        with (
            patch(
                "sync_box.cli.scan_local",
                return_value=[InventoryItem(".", "folder")],
            ) as local_scan,
            patch(
                "sync_box.cli.scan_box",
                return_value=[InventoryItem(".", "folder")],
            ),
            patch("sync_box.box_auth.build_authenticated_client", return_value=object()),
        ):
            result = main(
                [
                    "--config",
                    str(config_path),
                    "run",
                    "--dry-run",
                    "--summary-only",
                ]
            )

        self.assertEqual(result, 0)
        local_scan.assert_called_once_with(
            self.local_root,
            hash_files=True,
            excluded_paths=(".Trash-1000",),
            excluded_names=("Thumbs.db",),
        )
        self.assertFalse(state_dir.exists())

    def test_non_dry_run_is_refused(self) -> None:
        config_path = self.tmp_path / "config.toml"
        state_dir = self.tmp_path / "state"
        write_config(config_path, state_dir, self.local_root)

        result = main(["--config", str(config_path), "run"])

        self.assertEqual(result, 2)
        self.assertFalse(state_dir.exists())

    def test_initial_download_dry_run_only_builds_plan(self) -> None:
        config_path = self.tmp_path / "config.toml"
        state_dir = self.tmp_path / "state"
        write_config(config_path, state_dir, self.local_root)

        with (
            patch(
                "sync_box.cli.scan_local",
                return_value=[InventoryItem(".", "folder")],
            ) as local_scan,
            patch(
                "sync_box.cli.scan_box",
                return_value=[
                    InventoryItem(".", "folder"),
                    InventoryItem("remote.txt", "file", size=7, sha1="abc"),
                ],
            ),
            patch("sync_box.box_auth.build_authenticated_client", return_value=object()),
        ):
            result = main(
                [
                    "--config",
                    str(config_path),
                    "run",
                    "--dry-run",
                    "--initial-download-from-box",
                    "--limit",
                    "1",
                ]
            )

        self.assertEqual(result, 0)
        local_scan.assert_called_once_with(
            self.local_root,
            hash_files=True,
            excluded_paths=(".Trash-1000",),
            excluded_names=("Thumbs.db",),
        )
        self.assertFalse(state_dir.exists())

    def test_initial_download_non_dry_executes_the_complete_plan(self) -> None:
        config_path = self.tmp_path / "config.toml"
        state_dir = self.tmp_path / "state"
        write_config(config_path, state_dir, self.local_root)
        client = object()

        with (
            patch(
                "sync_box.cli.scan_local",
                return_value=[InventoryItem(".", "folder")],
            ),
            patch(
                "sync_box.cli.scan_box",
                return_value=[
                    InventoryItem(".", "folder"),
                    InventoryItem(
                        "remote.txt",
                        "file",
                        size=7,
                        content_id="11",
                        version_id="12",
                        sha1="abc",
                    ),
                ],
            ),
            patch(
                "sync_box.box_auth.build_authenticated_client",
                return_value=client,
            ),
            patch("sync_box.cli.execute_initial_download") as execute,
        ):
            execute.return_value = SimpleNamespace(
                downloaded=1,
                already_verified=0,
                failed_conflicting=0,
                folders_created=0,
                folders_reused=0,
                bytes=7,
                verified_sha1=1,
            )
            output = StringIO()
            with redirect_stdout(output):
                result = main(
                    [
                        "--config",
                        str(config_path),
                        "run",
                        "--initial-download-from-box",
                    ]
                )

        self.assertEqual(result, 0)
        execute.assert_called_once()
        self.assertIs(execute.call_args.args[0], client)
        self.assertEqual(execute.call_args.args[1], self.local_root)
        self.assertEqual(execute.call_args.args[2][0].relative_path, "remote.txt")
        self.assertTrue(callable(execute.call_args.kwargs["refresh_client"]))
        self.assertIn("downloaded=1", output.getvalue())
        self.assertIn("already_verified_skipped=0", output.getvalue())
        self.assertIn("failed_conflicting=0", output.getvalue())
        self.assertFalse(state_dir.exists())


if __name__ == "__main__":
    unittest.main()
