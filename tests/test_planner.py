from pathlib import Path
import tempfile
import unittest

from sync_box.inventory import InventoryItem, ScanError
from sync_box.local_inventory import scan_local
from sync_box.planner import (
    build_comparison_plan,
    build_initial_download_plan,
    render_initial_download_plan,
    render_plan,
    summarize_initial_download,
    summarize_plan,
)


class PlannerTests(unittest.TestCase):
    def test_classifies_comparison_without_choosing_directions(self) -> None:
        local = [
            InventoryItem(".", "folder"),
            InventoryItem("same.txt", "file", sha1="abc"),
            InventoryItem("changed.txt", "file", sha1="111"),
            InventoryItem("local.txt", "file", sha1="222"),
            InventoryItem("kind", "folder"),
        ]
        box = [
            InventoryItem(".", "folder"),
            InventoryItem("same.txt", "file", sha1="ABC"),
            InventoryItem("changed.txt", "file", sha1="333"),
            InventoryItem("box.txt", "file", sha1="444"),
            InventoryItem("kind", "file", sha1="555"),
        ]

        plan = build_comparison_plan(local, box)
        by_path = {item.relative_path: item.status for item in plan}

        self.assertEqual(by_path["same.txt"], "same")
        self.assertEqual(by_path["changed.txt"], "content_mismatch")
        self.assertEqual(by_path["local.txt"], "local_only")
        self.assertEqual(by_path["box.txt"], "box_only")
        self.assertEqual(by_path["kind"], "type_mismatch")
        self.assertEqual(summarize_plan(plan)["review"], 4)
        self.assertNotIn("same.txt", render_plan(plan))

    def test_local_hash_matches_known_sha1(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "hello.txt").write_bytes(b"hello")

            items = scan_local(root, hash_files=True)

        by_path = {item.relative_path: item for item in items}
        self.assertEqual(
            by_path["hello.txt"].sha1,
            "aaf4c61ddcc5e8a2dabede0f3b482cd9aea9434d",
        )

    def test_initial_download_plan_counts_files_folders_and_bytes(self) -> None:
        local = [InventoryItem(".", "folder")]
        box = [
            InventoryItem(".", "folder"),
            InventoryItem("Docs", "folder", content_id="10"),
            InventoryItem(
                "Docs/readme.txt",
                "file",
                size=12,
                content_id="11",
                sha1="abc",
            ),
            InventoryItem("shortcut", "web_link", content_id="12"),
        ]

        plan = build_initial_download_plan(local, box)

        self.assertEqual(
            summarize_initial_download(plan),
            {
                "total": 3,
                "files": 1,
                "folders": 1,
                "already_verified": 0,
                "reused_folders": 0,
                "bytes": 12,
                "unknown_size_files": 0,
                "unsupported": 1,
            },
        )
        rendered = render_initial_download_plan(plan, limit=2)
        self.assertIn("create_folder", rendered)
        self.assertIn("download_file", rendered)
        self.assertNotIn("shortcut", rendered)

    def test_initial_download_plan_refuses_unexpected_local_file(self) -> None:
        local = [
            InventoryItem(".", "folder"),
            InventoryItem("existing.txt", "file"),
        ]

        with self.assertRaisesRegex(ScanError, "unexpected local path"):
            build_initial_download_plan(local, [InventoryItem(".", "folder")])

    def test_initial_download_plan_accepts_verified_resume_items(self) -> None:
        local = [
            InventoryItem(".", "folder"),
            InventoryItem("Docs", "folder"),
            InventoryItem("Docs/done.txt", "file", size=4, sha1="abcd"),
        ]
        box = [
            InventoryItem(".", "folder"),
            InventoryItem("Docs", "folder", content_id="10"),
            InventoryItem(
                "Docs/done.txt", "file", size=4, content_id="11", sha1="ABCD"
            ),
            InventoryItem("Docs/todo.txt", "file", size=2, content_id="12"),
        ]

        plan = build_initial_download_plan(local, box)
        actions = {entry.relative_path: entry.action for entry in plan}

        self.assertEqual(actions["Docs"], "reuse_folder")
        self.assertEqual(actions["Docs/done.txt"], "skip_verified_file")
        self.assertEqual(actions["Docs/todo.txt"], "download_file")

    def test_initial_download_plan_rejects_mismatched_resume_file(self) -> None:
        local = [
            InventoryItem(".", "folder"),
            InventoryItem("done.txt", "file", size=4, sha1="bad"),
        ]
        box = [
            InventoryItem(".", "folder"),
            InventoryItem("done.txt", "file", size=4, content_id="11", sha1="good"),
        ]

        with self.assertRaisesRegex(ScanError, "SHA-1 differs"):
            build_initial_download_plan(local, box)

    def test_initial_download_plan_rejects_unverifiable_resume_file(self) -> None:
        local = [
            InventoryItem(".", "folder"),
            InventoryItem("done.txt", "file", size=4, sha1="abcd"),
        ]
        box = [
            InventoryItem(".", "folder"),
            InventoryItem("done.txt", "file", content_id="11"),
        ]

        with self.assertRaisesRegex(ScanError, "cannot be verified"):
            build_initial_download_plan(local, box)


if __name__ == "__main__":
    unittest.main()
