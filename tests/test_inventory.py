from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest

from sync_box.box_inventory import scan_box
from sync_box.inventory import ScanError, normalize_relative_path, summarize
from sync_box.local_inventory import scan_local


class FakeFolders:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def get_folder_by_id(self, folder_id: str, **kwargs: object) -> object:
        self.calls.append(("root", folder_id, kwargs))
        return SimpleNamespace(
            id=folder_id,
            type="folder",
            name="Remote",
            size=None,
            modified_at="2026-01-01T00:00:00Z",
            etag="7",
        )

    def get_folder_items(self, folder_id: str, **kwargs: object) -> object:
        self.calls.append(("items", folder_id, kwargs))
        marker = kwargs.get("marker")
        if folder_id == "10" and marker is None:
            return SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        id="11",
                        type="folder",
                        name="Docs",
                        size=None,
                        modified_at="2026-01-02T00:00:00Z",
                        etag="1",
                    )
                ],
                next_marker="next",
            )
        if folder_id == "10":
            return SimpleNamespace(
                entries=[
                    SimpleNamespace(
                        id="12",
                        type="file",
                        name="root.txt",
                        size=4,
                        modified_at="2026-01-03T00:00:00Z",
                        etag="2",
                        sha_1="abc",
                        sequence_id="3",
                        file_version=SimpleNamespace(id="99"),
                    )
                ],
                next_marker=None,
            )
        return SimpleNamespace(
            entries=[
                SimpleNamespace(
                    id="13",
                    type="file",
                    name="note.txt",
                    size=8,
                    modified_at="2026-01-04T00:00:00Z",
                    etag="4",
                    sha_1="def",
                    sequence_id="5",
                    file_version=SimpleNamespace(id="100"),
                )
            ],
            next_marker=None,
        )


class InventoryTests(unittest.TestCase):
    def test_local_scan_collects_metadata_without_following_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "folder").mkdir()
            (root / "folder" / "file.txt").write_text("hello", encoding="utf-8")
            os.symlink(root / "folder", root / "folder-link")

            items = scan_local(root)
            by_path = {item.relative_path: item for item in items}

            self.assertEqual(by_path["folder/file.txt"].size, 5)
            self.assertEqual(by_path["folder-link"].item_type, "symlink")
            self.assertEqual(summarize(items), {"total": 4, "folder": 2, "symlink": 1, "file": 1})

    def test_box_scan_uses_pagination_and_recurses(self) -> None:
        folders = FakeFolders()
        client = SimpleNamespace(folders=folders)

        items = scan_box(client, "10")
        by_path = {item.relative_path: item for item in items}

        self.assertEqual(set(by_path), {".", "Docs", "Docs/note.txt", "root.txt"})
        self.assertEqual(by_path["root.txt"].version_id, "99")
        self.assertEqual(by_path["Docs/note.txt"].sha1, "def")
        item_calls = [call for call in folders.calls if call[0] == "items"]
        self.assertEqual(len(item_calls), 3)
        self.assertTrue(all(call[2]["usemarker"] for call in item_calls))
        self.assertEqual(item_calls[1][2]["marker"], "next")

    def test_normalization_rejects_parent_segment(self) -> None:
        with self.assertRaises(ScanError):
            normalize_relative_path(("..", "secret"))

    def test_normalization_uses_nfc(self) -> None:
        self.assertEqual(normalize_relative_path(("Cafe\u0301",)), "Caf\u00e9")

    def test_box_scan_rejects_normalized_name_collision(self) -> None:
        class CollisionFolders(FakeFolders):
            def get_folder_items(self, folder_id: str, **kwargs: object) -> object:
                return SimpleNamespace(
                    entries=[
                        SimpleNamespace(id="1", type="file", name="Caf\u00e9"),
                        SimpleNamespace(id="2", type="file", name="Cafe\u0301"),
                    ],
                    next_marker=None,
                )

        with self.assertRaisesRegex(ScanError, "normalization collision"):
            scan_box(SimpleNamespace(folders=CollisionFolders()), "10")

    def test_local_scan_excludes_path_and_entire_subtree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            trash = root / ".Trash-1000"
            trash.mkdir()
            (trash / "deleted.txt").write_text("deleted", encoding="utf-8")
            docs = root / "Docs"
            docs.mkdir()
            (docs / "Thumbs.db").write_bytes(b"thumbnail cache")
            (root / "keep.txt").write_text("keep", encoding="utf-8")

            items = scan_local(
                root,
                excluded_paths=(".Trash-1000",),
                excluded_names=("Thumbs.db",),
            )

        self.assertEqual(
            {item.relative_path for item in items}, {".", "Docs", "keep.txt"}
        )

    def test_box_scan_excludes_folder_without_traversing_it(self) -> None:
        class ExclusionFolders(FakeFolders):
            def get_folder_items(self, folder_id: str, **kwargs: object) -> object:
                if folder_id == "20":
                    raise AssertionError("excluded Box folder was traversed")
                if folder_id == "30":
                    return SimpleNamespace(
                        entries=[
                            SimpleNamespace(
                                id="31", type="file", name="Thumbs.db"
                            ),
                            SimpleNamespace(
                                id="32", type="file", name="keep.txt"
                            ),
                        ],
                        next_marker=None,
                    )
                return SimpleNamespace(
                    entries=[
                        SimpleNamespace(
                            id="20", type="folder", name=".Trash-1000"
                        ),
                        SimpleNamespace(
                            id="30", type="folder", name="Docs"
                        ),
                    ],
                    next_marker=None,
                )

        items = scan_box(
            SimpleNamespace(folders=ExclusionFolders()),
            "10",
            excluded_paths=(".Trash-1000",),
            excluded_names=("Thumbs.db",),
        )

        self.assertEqual(
            {item.relative_path for item in items},
            {".", "Docs", "Docs/keep.txt"},
        )


if __name__ == "__main__":
    unittest.main()
