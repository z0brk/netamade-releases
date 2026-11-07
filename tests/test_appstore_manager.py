from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from appstore_manager import (
    AppStoreManager,
    WorkflowManager,
    extract_icon_from_apk,
    find_entry_by_id,
    infer_existing_code,
    infer_workflow_code,
    normalize_incompatibility,
    normalize_message_board,
    parse_workflow_bundle,
)


class AppStoreManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_root = Path(tempfile.mkdtemp(prefix="appstore-tests-"))
        self.json_path = self.tmp_root / "appstore.json"
        self.apks_dir = self.tmp_root / "apks"
        self.workflows_json_path = self.tmp_root / "workflows.json"
        self.workflows_dir = self.tmp_root / "workflows"
        self.manager = AppStoreManager(self.json_path, self.apks_dir)
        self.workflow_manager = WorkflowManager(self.workflows_json_path, self.workflows_dir)
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
        self.workflow_manager.save_data(
            {
                "categories": ["提醒", "自动化", "提醒"],
                "workflows": [
                    {
                        "id": "workflow001",
                        "name": "锁车提醒",
                        "category": "提醒",
                        "author": "tester",
                        "description": "",
                        "file": "/workflow-demo-001/demo.json",
                    }
                ]
            }
        )

    def test_normalize_incompatibility(self) -> None:
        result = normalize_incompatibility(["EP41", "ALL", "EP41", "INVALID"])
        self.assertEqual(result, ["ALL", "EP41"])

    def test_normalize_message_board(self) -> None:
        result = normalize_message_board(
            [
                {
                    "title": "  安装说明 ",
                    "contents": [
                        {"text": "  打开后授权  ", "canCopy": True, "extra": "ignored"},
                        {"text": "", "canCopy": True},
                    ],
                    "extra": "ignored",
                },
                {"title": "", "contents": []},
                "invalid",
            ]
        )
        self.assertEqual(
            result,
            [
                {
                    "title": "安装说明",
                    "contents": [{"text": "打开后授权", "canCopy": True}],
                }
            ],
        )

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
        self.assertEqual([item["id"] for item in data["apps"]], ["entry001", "entry002"])

    def test_upsert_entry_allow_same_package(self) -> None:
        data = self.manager.load_data()
        self.manager.upsert_entry(data, {"id": "entry002", "name": "SFGJ-2", "package": "com.demo.sfgj", "category": "工具"})
        self.assertEqual(len(data["apps"]), 2)
        self.assertEqual(sum(1 for item in data["apps"] if item.get("package") == "com.demo.sfgj"), 2)

    def test_reorder_entries(self) -> None:
        data = self.manager.load_data()
        self.manager.upsert_entry(data, {"id": "entry002", "name": "New App", "package": "com.demo.new", "category": "娱乐"})
        changed = self.manager.reorder_entries(data, ["entry002", "entry001"])
        self.assertTrue(changed)
        self.assertEqual([item["id"] for item in data["apps"]], ["entry002", "entry001"])

    def test_reorder_entries_reject_missing_id(self) -> None:
        data = self.manager.load_data()
        self.manager.upsert_entry(data, {"id": "entry002", "name": "New App", "package": "com.demo.new", "category": "娱乐"})
        changed = self.manager.reorder_entries(data, ["entry002"])
        self.assertFalse(changed)
        self.assertEqual([item["id"] for item in data["apps"]], ["entry001", "entry002"])

    def test_load_data_dedup_categories(self) -> None:
        data = self.manager.load_data()
        self.assertEqual(data["categories"], ["工具", "娱乐"])
        self.assertEqual(data["messageBoard"], [])

    def test_load_data_normalize_message_board(self) -> None:
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        data["messageBoard"] = [
            {"title": " 提示 ", "contents": [{"text": " 可复制内容 ", "canCopy": True, "extra": "ignored"}]},
            {"title": "", "contents": []},
        ]
        self.manager.save_data(data)

        loaded = self.manager.load_data()
        self.assertEqual(
            loaded["messageBoard"],
            [{"title": "提示", "contents": [{"text": "可复制内容", "canCopy": True}]}],
        )

    def test_ensure_category(self) -> None:
        data = self.manager.load_data()
        self.manager.ensure_category(data, "系统")
        self.assertEqual(data["categories"], ["工具", "娱乐", "系统"])

    def test_replace_categories_keep_order(self) -> None:
        data = self.manager.load_data()
        self.manager.replace_categories(data, ["影音", "工具", "影音", "", "系统"])
        self.assertEqual(data["categories"], ["影音", "工具", "系统"])

    def test_workflow_manager_load_data_dedup_categories(self) -> None:
        data = self.workflow_manager.load_data()
        self.assertEqual(data["categories"], ["提醒", "自动化"])

    def test_workflow_manager_ensure_category(self) -> None:
        data = self.workflow_manager.load_data()
        self.workflow_manager.ensure_category(data, "场景优化")
        self.assertEqual(data["categories"], ["提醒", "自动化", "场景优化"])

    def test_workflow_manager_replace_categories_keep_order(self) -> None:
        data = self.workflow_manager.load_data()
        self.workflow_manager.replace_categories(data, ["优化", "提醒", "优化", "", "自动化"])
        self.assertEqual(data["categories"], ["优化", "提醒", "自动化"])

    def test_delete_entry(self) -> None:
        data = self.manager.load_data()
        deleted = self.manager.delete_entry(data, "entry001")
        self.assertTrue(deleted)
        self.assertEqual(data["apps"], [])

    def test_cleanup_entry_assets(self) -> None:
        data = self.manager.load_data()
        app_dir = self.apks_dir / "sfgj-abcd12"
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "demo.apk").write_bytes(b"1")
        entry = data["apps"][0]
        data["apps"].clear()
        removed = self.manager.cleanup_entry_assets(data, entry)
        self.assertTrue(removed)
        self.assertFalse(app_dir.exists())

    def test_cleanup_orphan_asset_dirs(self) -> None:
        data = self.manager.load_data()
        keep = self.apks_dir / "sfgj-abcd12"
        keep.mkdir(parents=True, exist_ok=True)
        (keep / "keep.apk").write_bytes(b"1")
        orphan = self.apks_dir / "orphan-1"
        orphan.mkdir(parents=True, exist_ok=True)
        (orphan / "orphan.apk").write_bytes(b"1")
        removed = self.manager.cleanup_orphan_asset_dirs(data)
        self.assertIn("orphan-1", removed)
        self.assertFalse(orphan.exists())
        self.assertTrue(keep.exists())

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

    def test_extract_icon_from_apk_adaptive_icon_xml(self) -> None:
        class FakeRes:
            def get_res_id_by_key(self, package: str, type_name: str, name: str):
                mapping = {
                    ("com.demo.app", "mipmap", "ic_launcher"): 0x7F080001,
                    ("com.demo.app", "mipmap", "ic_launcher_foreground"): 0x7F080002,
                }
                return mapping.get((package, type_name, name))

            def get_resolved_res_configs(self, res_id: int):
                if res_id == 0x7F080001:
                    return [(object(), "res/mipmap-anydpi-v26/ic_launcher.xml")]
                if res_id == 0x7F080002:
                    return [(object(), "res/mipmap-xxhdpi/ic_launcher_foreground.png")]
                return []

        class FakeAPK:
            package = "com.demo.app"

            def __init__(self, _: str) -> None:
                pass

            def get_android_resources(self):
                return FakeRes()

            def get_main_activity(self):
                return None

            def get_attribute_value(self, kind: str, key: str, name: str | None = None):
                if kind == "application" and key == "icon":
                    return "@mipmap/ic_launcher"
                return None

            def get_app_icon(self):
                return None

            def get_files(self):
                return []

            def get_file(self, filename: str) -> bytes:
                if filename == "res/mipmap-anydpi-v26/ic_launcher.xml":
                    return (
                        b'<?xml version="1.0" encoding="utf-8"?>'
                        b'<adaptive-icon xmlns:android="http://schemas.android.com/apk/res/android">'
                        b'<foreground android:drawable="@mipmap/ic_launcher_foreground"/>'
                        b"</adaptive-icon>"
                    )
                if filename == "res/mipmap-xxhdpi/ic_launcher_foreground.png":
                    return b"fg-icon"
                return b""

        output_dir = self.tmp_root / "icons2"
        with patch("appstore_manager.APK", FakeAPK):
            icon_path = extract_icon_from_apk(Path("fake.apk"), output_dir)
        self.assertIsNotNone(icon_path)
        assert icon_path is not None
        self.assertEqual(icon_path.name, "icon.png")
        self.assertEqual(icon_path.read_bytes(), b"fg-icon")

    def test_find_entry_by_id(self) -> None:
        data = self.manager.load_data()
        entry = find_entry_by_id(data["apps"], "entry001")
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry["package"], "com.demo.sfgj")

    def test_parse_workflow_bundle(self) -> None:
        workflow_file = self.tmp_root / "sample-workflow.json"
        workflow_file.write_text(
            json.dumps(
                [
                    {
                        "workflow": {
                            "name": "锁车提醒",
                            "config": {"tags": ["📱 推送提醒", "🏠 解/锁车"]},
                        }
                    },
                    {
                        "workflow": {
                            "name": "短期解车提醒",
                            "config": {"tags": ["🏠 解/锁车", "⭐ 无感优化"]},
                        }
                    },
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        metadata = parse_workflow_bundle(workflow_file)
        self.assertEqual(metadata.workflow_count, 2)
        self.assertEqual(metadata.workflow_names, ["锁车提醒", "短期解车提醒"])
        self.assertEqual(metadata.tags, ["📱 推送提醒", "🏠 解/锁车", "⭐ 无感优化"])

    def test_workflow_manager_backfill_entry_metadata(self) -> None:
        workflow_dir = self.workflows_dir / "workflow-demo-001"
        workflow_dir.mkdir(parents=True, exist_ok=True)
        workflow_file = workflow_dir / "demo.json"
        workflow_file.write_text(
            json.dumps(
                [
                    {
                        "workflow": {
                            "name": "锁车关闭无感",
                            "config": {"tags": ["⭐ 无感优化"]},
                        }
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        entry = {"file": "/workflow-demo-001/demo.json"}
        changed = self.workflow_manager.backfill_entry_metadata(entry)
        self.assertTrue(changed)
        self.assertEqual(entry["code"], "workflow-demo-001")
        self.assertEqual(entry["filename"], "demo.json")
        self.assertEqual(entry["name"], "demo")
        self.assertEqual(entry["workflowCount"], 1)
        self.assertEqual(entry["workflowNames"], ["锁车关闭无感"])
        self.assertEqual(entry["tags"], ["⭐ 无感优化"])
        self.assertEqual(entry["category"], "未分类")
        self.assertTrue(entry["md5sum"])
        self.assertGreater(entry["filesize"], 0)

    def test_workflow_manager_delete_and_cleanup(self) -> None:
        data = self.workflow_manager.load_data()
        workflow_dir = self.workflows_dir / "workflow-demo-001"
        workflow_dir.mkdir(parents=True, exist_ok=True)
        (workflow_dir / "demo.json").write_text("[]", encoding="utf-8")

        deleted = self.workflow_manager.delete_entry(data, "workflow001")
        self.assertTrue(deleted)
        entry = {
            "id": "workflow001",
            "file": "/workflow-demo-001/demo.json",
        }
        removed = self.workflow_manager.cleanup_entry_assets(data, entry)
        self.assertTrue(removed)
        self.assertFalse(workflow_dir.exists())

    def test_infer_workflow_code(self) -> None:
        entry = {"file": "/workflow-demo-001/demo.json"}
        self.assertEqual(infer_workflow_code(entry), "workflow-demo-001")

    def test_ensure_entry_ids(self) -> None:
        data = {"apps": [{"package": "com.a"}, {"id": "dup", "package": "com.b"}, {"id": "dup", "package": "com.c"}]}
        changed = self.manager.ensure_entry_ids(data)
        self.assertTrue(changed)
        ids = [item.get("id") for item in data["apps"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_workflow_manager_ensure_entry_ids(self) -> None:
        data = {"workflows": [{"file": "/a/demo.json"}, {"id": "dup", "file": "/b/demo.json"}, {"id": "dup", "file": "/c/demo.json"}]}
        changed = self.workflow_manager.ensure_entry_ids(data)
        self.assertTrue(changed)
        ids = [item.get("id") for item in data["workflows"]]
        self.assertEqual(len(ids), len(set(ids)))


if __name__ == "__main__":
    unittest.main()
