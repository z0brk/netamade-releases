from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from soundstore_manager import SoundStoreManager, normalize_tags


class SoundStoreManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.manager = SoundStoreManager(root / "sounds.json", root / "sounds")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_create_entry_and_reload(self) -> None:
        source = Path(self.tmp.name) / "sample.mp3"
        source.write_bytes(b"sample audio")
        entry, target_dir = self.manager.create_entry(source, "锁车.mp3", ["锁车", "锁车"])
        data = {"schemaVersion": 1, "sounds": [entry]}
        self.manager.save_data(data)

        loaded = self.manager.load_data()
        self.assertEqual(loaded["sounds"][0]["filename"], "锁车.mp3")
        self.assertEqual(loaded["sounds"][0]["tags"], ["锁车"])
        self.assertTrue((target_dir / entry["file"].split("/")[-1]).is_file())

    def test_reorder_requires_complete_unique_ids(self) -> None:
        data = {"schemaVersion": 1, "sounds": [{"id": "a"}, {"id": "b"}]}
        self.assertFalse(self.manager.reorder_entries(data, ["b"]))
        self.assertFalse(self.manager.reorder_entries(data, ["a", "a"]))
        self.assertTrue(self.manager.reorder_entries(data, ["b", "a"]))
        self.assertEqual([item["id"] for item in data["sounds"]], ["b", "a"])

    def test_bulk_tags_and_delete_cleanup(self) -> None:
        source = Path(self.tmp.name) / "sample.wav"
        source.write_bytes(b"wave")
        first, first_dir = self.manager.create_entry(source, "first.wav", ["提示"])
        second, _ = self.manager.create_entry(source, "second.wav", [])
        data = {"schemaVersion": 1, "sounds": [first, second]}

        changed = self.manager.update_tags(data, [first["id"], second["id"]], ["锁车"], ["提示"])
        self.assertEqual(changed, 2)
        self.assertEqual(first["tags"], ["锁车"])
        deleted = self.manager.delete_entries(data, [first["id"]])
        self.manager.cleanup_entry_assets(data, deleted)
        self.assertFalse(first_dir.exists())
        self.assertEqual([item["id"] for item in data["sounds"]], [second["id"]])

    def test_normalize_tags(self) -> None:
        self.assertEqual(normalize_tags(" 锁车,提示音,锁车, "), ["锁车", "提示音"])


if __name__ == "__main__":
    unittest.main()
