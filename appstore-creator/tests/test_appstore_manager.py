from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appstore_manager import (
    AppStoreManager,
    extract_icon_from_apk,
    find_entry_by_id,
    infer_existing_code,
    normalize_incompatibility,
)


class AppStoreManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = Path(tempfile.mkdtemp(prefix="appstore-tests-"))
        self.json_path = self.tmp_root / "appstore.json"
        self.apks_dir = self.tmp_root / "apks"
        self.manager = AppStoreManager(self.json_path, self.apks_dir)
        self.manager.save_data(
            {
                "notice": "",
                "categories": ["工具", "娱乐", "工具"],
                "apps": [
                    {
                        "id": "entry001",
                        "name": "SFGJ",
                        "package": "com.demo.sfgj",
                        "category": "工具",
                        "apk": "/sfgj-abcd12/SFGJ.apk",
                    }
                ],
            }
        )

    def test_normalize_incompatibility(self) -> None:
        result = normalize_incompatibility(["EP41", "ALL", "EP41", "INVALID"])
        self.assertEqual(result, ["ALL", "EP41"])

    def test_infer_existing_code(self) -> None:
        entry = {"apk": "/demo-xxxxxx/app.apk"}
        self.assertEqual(infer_existing_code(entry), "demo-xxxxxx")

    def test_resolve_code_reuse_preferred(self) -> None:
        code = self.manager.resolve_code("com.demo.sfgj", "SFGJ", preferred_code="sfgj-abcd12")
        self.assertEqual(code, "sfgj-abcd12")

    def test_resolve_code_unique(self) -> None:
        code = self.manager.resolve_code("com.example.music", "Music")
        self.assertTrue(code.startswith("music-"))
        self.assertNotIn(code, {"sfgj-abcd12"})

    def test_upsert_entry(self) -> None:
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self.manager.upsert_entry(data, {"id": "entry002", "name": "New App", "package": "com.demo.new", "category": "娱乐"})
        self.assertEqual(len(data["apps"]), 2)

    def test_upsert_entry_allow_same_package(self) -> None:
        data = self.manager.load_data()
        self.manager.upsert_entry(data, {"id": "entry002", "name": "SFGJ-2", "package": "com.demo.sfgj", "category": "工具"})
        self.assertEqual(len(data["apps"]), 2)
        self.assertEqual(sum(1 for item in data["apps"] if item.get("package") == "com.demo.sfgj"), 2)

    def test_load_data_dedup_categories(self) -> None:
        data = self.manager.load_data()
        self.assertEqual(data["categories"], ["工具", "娱乐"])

    def test_ensure_category(self) -> None:
        data = self.manager.load_data()
        self.manager.ensure_category(data, "系统")
        self.assertEqual(data["categories"], ["工具", "娱乐", "系统"])

    def test_replace_categories_keep_order(self) -> None:
        data = self.manager.load_data()
        self.manager.replace_categories(data, ["影音", "工具", "影音", "", "系统"])
        self.assertEqual(data["categories"], ["影音", "工具", "系统"])

    def test_delete_entry(self) -> None:
        data = self.manager.load_data()
        deleted = self.manager.delete_entry(data, "entry001")
        self.assertTrue(deleted)
        self.assertEqual(data["apps"], [])

    def test_backfill_entry_metadata_fill_code_only(self) -> None:
        app_dir = self.apks_dir / "demo-100001"
        app_dir.mkdir(parents=True, exist_ok=True)
        apk_file = app_dir / "demo.apk"
        apk_file.write_bytes(b"dummy")
        entry = {"apk": "/demo-100001/demo.apk"}
        changed = self.manager.backfill_entry_metadata(entry)
        self.assertTrue(changed)
        self.assertEqual(entry["code"], "demo-100001")
        self.assertTrue(entry["md5sum"])
        self.assertEqual(entry["filesize"], 5)

    def test_extract_icon_from_apk(self) -> None:
        class FakeAPK:
            def __init__(self, _: str) -> None:
                pass

            def get_app_icon(self) -> str:
                return "res/mipmap-xxhdpi/ic_launcher.png"

            def get_files(self) -> list[str]:
                return ["res/mipmap-xxhdpi/ic_launcher.png"]

            def get_file(self, _: str) -> bytes:
                return b"icon-bytes"

        output_dir = self.tmp_root / "icons"
        with patch("appstore_manager.APK", FakeAPK):
            icon_path = extract_icon_from_apk(Path("fake.apk"), output_dir)
        self.assertIsNotNone(icon_path)
        assert icon_path is not None
        self.assertTrue(icon_path.is_file())
        self.assertEqual(icon_path.read_bytes(), b"icon-bytes")

    def test_find_entry_by_id(self) -> None:
        data = self.manager.load_data()
        entry = find_entry_by_id(data["apps"], "entry001")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["package"], "com.demo.sfgj")

    def test_ensure_entry_ids(self) -> None:
        data = {"apps": [{"package": "com.a"}, {"id": "dup", "package": "com.b"}, {"id": "dup", "package": "com.c"}]}
        changed = self.manager.ensure_entry_ids(data)
        self.assertTrue(changed)
        ids = [item.get("id") for item in data["apps"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
