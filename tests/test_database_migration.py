from contextlib import closing
from importlib.resources import files
from pathlib import Path
import sqlite3
import tempfile
import unittest

from sync_box.database import initialize_database, save_inventory
from sync_box.inventory import InventoryItem


class DatabaseMigrationTests(unittest.TestCase):
    def test_migrates_v1_without_losing_baselines(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                with connection:
                    connection.execute(
                        "CREATE TABLE item_baselines(relative_path TEXT PRIMARY KEY)"
                    )
                    connection.execute(
                        "CREATE TABLE sync_runs("
                        "id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, "
                        "dry_run INTEGER NOT NULL, outcome TEXT, summary TEXT)"
                    )
                    connection.execute("INSERT INTO item_baselines VALUES ('kept.txt')")
                    connection.execute("PRAGMA user_version = 1")

            initialize_database(database)

            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                baseline = connection.execute(
                    "SELECT relative_path FROM item_baselines"
                ).fetchone()[0]
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
            self.assertEqual(version, 6)
            self.assertEqual(baseline, "kept.txt")
            self.assertIn("inventory_items", tables)

    def test_saves_inventory_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            run_id = save_inventory(
                database,
                source="local",
                root_identifier="/data",
                items=[InventoryItem("file.txt", "file", size=12, inode=7)],
            )
            with closing(sqlite3.connect(database)) as connection:
                row = connection.execute(
                    "SELECT relative_path, size, inode FROM inventory_items WHERE run_id=?",
                    (run_id,),
                ).fetchone()
            self.assertEqual(row, ("file.txt", 12, 7))

    def test_migrates_v4_sync_journal_to_generation_bound_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "state.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                for resource in (
                    "schema.sql",
                    "migrations/0002_inventory.sql",
                    "migrations/0003_sync_state.sql",
                    "migrations/0004_conflict_resolution.sql",
                ):
                    connection.executescript(
                        files("sync_box").joinpath(*resource.split("/")).read_text(
                            encoding="utf-8"
                        )
                    )
                connection.execute(
                    "INSERT INTO sync_runs(started_at, dry_run, outcome) "
                    "VALUES ('before-v5', 0, 'completed')"
                )
                connection.executemany(
                    "INSERT INTO sync_runs(started_at, dry_run, outcome) VALUES (?, 0, 'running')",
                    [("stale-one",), ("stale-two",)],
                )
                connection.commit()

            initialize_database(database)

            with closing(sqlite3.connect(database)) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                columns = {
                    row[1] for row in connection.execute("PRAGMA table_info(sync_runs)")
                }
                preserved = connection.execute(
                    "SELECT started_at, outcome FROM sync_runs WHERE started_at='before-v5'"
                ).fetchone()
                stale_outcomes = connection.execute(
                    "SELECT DISTINCT outcome FROM sync_runs "
                    "WHERE started_at LIKE 'stale-%'"
                ).fetchall()
                indexes = {
                    row[1] for row in connection.execute("PRAGMA index_list(sync_runs)")
                }
            self.assertEqual(version, 6)
            self.assertTrue({"baseline_generation", "local_root", "plan_json"} <= columns)
            self.assertEqual(preserved, ("before-v5", "completed"))
            self.assertEqual(stale_outcomes, [("failed",)])
            self.assertIn("one_running_sync", indexes)


if __name__ == "__main__":
    unittest.main()
