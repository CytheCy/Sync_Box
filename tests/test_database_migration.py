from contextlib import closing
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
            self.assertEqual(version, 4)
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


if __name__ == "__main__":
    unittest.main()
