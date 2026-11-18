"""Picking a folder on the app's computer instead of typing its path."""

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from lncrawl.utils import folders


class FolderListTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in ("Novels", "anime", "Zeta", ".hidden"):
            (self.root / name).mkdir()
        (self.root / "notes.txt").write_text("a file, not a folder")
        library = self.root / "Novels" / "LNCrawl"
        (library / "novels").mkdir(parents=True)
        (library / "sqlite.db").write_bytes(b"")

    def test_only_folders_are_listed_in_name_order_without_hidden_ones(self) -> None:
        listed = folders.list_folder(str(self.root))
        self.assertEqual([f["name"] for f in listed["folders"]], ["anime", "Novels", "Zeta"])
        self.assertEqual(listed["path"], str(self.root))
        self.assertEqual(listed["parent"], str(self.root.parent))
        self.assertIsNotNone(listed["free"])

    def test_a_folder_holding_a_library_is_marked(self) -> None:
        listed = folders.list_folder(str(self.root / "Novels"))
        self.assertEqual(
            listed["folders"],
            [{"name": "LNCrawl", "path": str(self.root / "Novels" / "LNCrawl"), "library": True}],
        )
        self.assertTrue(folders.list_folder(str(self.root / "Novels" / "LNCrawl"))["library"])

    def test_with_no_path_the_drives_and_own_folders_are_the_places_to_start(self) -> None:
        with patch.object(folders, "drive_roots", return_value=[self.root]):
            listed = folders.list_folder("")
        self.assertIsNone(listed["path"])
        self.assertIn(str(self.root), [place["path"] for place in listed["places"]])

    def test_a_long_folder_is_cut_short_and_says_how_many_more(self) -> None:
        with patch.object(folders, "MOST_FOLDERS", 2):
            listed = folders.list_folder(str(self.root))
        self.assertEqual(len(listed["folders"]), 2)
        self.assertEqual(listed["more"], 1)

    def test_what_cannot_be_listed_says_why(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot be found"):
            folders.list_folder(str(self.root / "missing"))
        with self.assertRaisesRegex(ValueError, "not a full path"):
            folders.list_folder("relative/folder")

    def test_a_new_folder_is_made_where_asked(self) -> None:
        made = folders.new_folder(str(self.root), "Library 2")
        self.assertEqual(made, str(self.root / "Library 2"))
        self.assertTrue((self.root / "Library 2").is_dir())

    def test_a_name_that_cannot_be_a_folder_is_refused(self) -> None:
        for name in ("", "..", "a/b", "a\\b", "what?", "Zeta"):
            with self.assertRaises(ValueError, msg=name):
                folders.new_folder(str(self.root), name)


if __name__ == "__main__":
    unittest.main()
