from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from screensaver_manager import ScreensaverManager
from scripts.import_screensavers import parse_classification


class ScreensaverManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.manager = ScreensaverManager(root / "screensavers.json", root / "screensavers")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @patch.object(ScreensaverManager, "generate_preview")
    @patch.object(ScreensaverManager, "probe_media", return_value={"type": "IMAGE", "width": 1920, "height": 1080})
    def test_prepare_and_commit_batch(self, _probe, preview) -> None:
        preview.side_effect = lambda _source, target: target.write_bytes(b"webp")
        source = Path(self.tmp.name) / "山水_[适用-L主副全屏]_[1920x1080].jpg"
        source.write_bytes(b"image")
        staging = Path(self.tmp.name) / "staging"
        staging.mkdir()
        entry, staged_dir = self.manager.prepare_entry(source, source.name, "SUITABLE", ["L_MAIN_FULL", "L_SECONDARY_FULL"], staging)
        data = {"schemaVersion": 1, "screensavers": []}
        self.manager.commit_batch(data, [entry], [staged_dir])
        self.assertEqual(entry["name"], "山水")
        self.assertEqual(entry["width"], 1920)
        self.assertTrue((self.manager.screensavers_dir / staged_dir.name / "preview.webp").is_file())
        self.assertEqual(len(self.manager.load_data()["screensavers"]), 1)

    def test_commit_batch_rolls_back_moved_assets_when_manifest_save_fails(self) -> None:
        staging = Path(self.tmp.name) / "staging"
        staged_dir = staging / "screensaver-12345678"
        staged_dir.mkdir(parents=True)
        (staged_dir / "preview.webp").write_bytes(b"webp")
        with patch.object(self.manager, "save_data", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.manager.commit_batch({"screensavers": []}, [{"id": "x"}], [staged_dir])
        self.assertFalse((self.manager.screensavers_dir / staged_dir.name).exists())

    def test_reorder_and_bulk_delete(self) -> None:
        data = {"screensavers": [{"id": "a", "file": "/screensaver-a/a.jpg"}, {"id": "b", "file": "/screensaver-b/b.jpg"}]}
        self.assertFalse(self.manager.reorder_entries(data, ["a"]))
        self.assertTrue(self.manager.reorder_entries(data, ["b", "a"]))
        deleted = self.manager.delete_entries(data, ["a"])
        self.assertEqual([item["id"] for item in deleted], ["a"])
        self.assertEqual([item["id"] for item in data["screensavers"]], ["b"])

    def test_parse_import_classification(self) -> None:
        self.assertEqual(parse_classification("山水_[适用-L主副全屏]_[1920x1080].jpg"), ("SUITABLE", ["L_MAIN_FULL", "L_SECONDARY_FULL"]))
        self.assertEqual(parse_classification("人物_[候选-S猎装GT主屏-需裁剪]_[1080x1920].mp4"), ("CANDIDATE", ["S_GT_MAIN"]))
        self.assertEqual(parse_classification("长图_[不建议-比例不匹配]_[10000x1218].jpg"), ("UNSUITABLE", []))

    @patch.object(ScreensaverManager, "probe_media", return_value={"type": "IMAGE", "width": 1920, "height": 1080})
    def test_suitable_entry_requires_target(self, _probe) -> None:
        source = Path(self.tmp.name) / "sample.jpg"
        source.write_bytes(b"image")
        with self.assertRaisesRegex(ValueError, "至少一个目标"):
            self.manager.prepare_entry(source, source.name, "SUITABLE", [], Path(self.tmp.name) / "staging")


if __name__ == "__main__":
    unittest.main()
