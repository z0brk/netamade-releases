from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Any


class EnhancementManager:
    def __init__(self, json_path: Path, assets_dir: Path):
        self.json_path = json_path
        self.assets_dir = assets_dir

    def load_data(self) -> dict[str, Any]:
        if not self.json_path.exists():
            return {"schemaVersion": 1, "enhancements": []}
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        data.setdefault("schemaVersion", 1)
        data.setdefault("enhancements", [])
        changed = False
        for item in data["enhancements"]:
            for version in item.setdefault("versions", []):
                if not version.get("id"):
                    version["id"] = uuid.uuid4().hex[:12]
                    changed = True
        if changed:
            self.save_data(data)
        return data

    def save_data(self, data: dict[str, Any]) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.json_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, self.json_path)

    def add_upload(self, data: dict[str, Any], source: Path, name: str, package: str, target: str, update_time: str, description: str, car_code: str, assembly_version: str, stock_hash: str) -> None:
        if source.suffix.lower() != ".apk":
            raise ValueError("仅支持 APK")
        if not re.fullmatch(r"[a-zA-Z0-9._]+", package.strip()):
            raise ValueError("包名无效")
        if not target.startswith(("/system/app/", "/system/priv-app/", "/product/app/", "/product/priv-app/")):
            raise ValueError("目标路径不在允许范围")
        if car_code.strip() not in {"0", "4", "5", "9"}:
            raise ValueError("车型无效")
        if not assembly_version.strip():
            raise ValueError("系统版本不能为空")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", stock_hash.strip()):
            raise ValueError("原厂 APK SHA-256 无效")
        digest = self.sha256(source)
        existing = next((item for item in data.setdefault("enhancements", [])
                         if item.get("package") == package.strip() and item.get("targetApkPath") == target.strip()), None)
        entry_id = str(existing.get("id")) if existing else uuid.uuid4().hex[:12]
        directory = self.assets_dir / entry_id
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{digest[:16]}.apk"
        shutil.copyfile(source, directory / filename)
        version_item = {
            "id": uuid.uuid4().hex[:12],
            "updateTime": update_time.strip(),
            "description": description.strip(),
            "apk": f"/enhancements/{entry_id}/{filename}",
            "sha256": digest,
            "filesize": source.stat().st_size,
            "compatibility": {
                "allOf": [
                    {"property": "ro.hozon.car.code", "equals": car_code.strip()},
                    {"property": "ro.hozon.car.assembly.version", "equals": assembly_version.strip()},
                ],
                "stockApkSha256": stock_hash.strip().lower(),
            },
        }
        if existing:
            existing.setdefault("versions", []).append(version_item)
            return
        item = {
            "id": entry_id,
            "name": name.strip() or package.strip(),
            "package": package.strip(),
            "targetApkPath": target.strip(),
            "versions": [version_item],
        }
        data.setdefault("enhancements", []).append(item)

    def update_entry(self, data: dict[str, Any], entry_id: str, name: str, package: str, target: str, simulation: bool) -> None:
        entry = self.find(data, entry_id)
        if not entry:
            raise ValueError("未找到应用增强")
        if not re.fullmatch(r"[a-zA-Z0-9._]+", package.strip()):
            raise ValueError("包名无效")
        if not target.startswith(("/system/app/", "/system/priv-app/", "/product/app/", "/product/priv-app/")):
            raise ValueError("目标路径不在允许范围")
        entry.update({"name": name.strip() or package.strip(), "package": package.strip(), "targetApkPath": target.strip(), "simulation": bool(simulation)})

    def add_simulation_version(self, data: dict[str, Any], entry_id: str, update_time: str, description: str, car_code: str, assembly_version: str, stock_package_version: str, stock_hash: str) -> None:
        entry = self.find(data, entry_id)
        if not entry or not entry.get("simulation"):
            raise ValueError("模拟应用才能新增无 APK 版本")
        version_item = {"id": uuid.uuid4().hex[:12], "apk": "", "sha256": stock_hash.strip().lower(), "filesize": 0}
        entry.setdefault("versions", []).append(version_item)
        self.update_version(data, entry_id, version_item["id"], update_time, description, car_code, assembly_version, stock_package_version, stock_hash)

    def update_version(self, data: dict[str, Any], entry_id: str, version_id: str, update_time: str, description: str, car_code: str, assembly_version: str, stock_package_version: str, stock_hash: str) -> None:
        entry = self.find(data, entry_id)
        target = next((v for v in (entry or {}).get("versions", []) if str(v.get("id", "")) == version_id), None)
        if not entry or not target:
            raise ValueError("未找到增强版本")
        if car_code.strip() not in {"0", "4", "5", "9"} or not assembly_version.strip():
            raise ValueError("车型或系统版本无效")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", stock_hash.strip()):
            raise ValueError("原厂 APK SHA-256 无效")
        target.update({"updateTime": update_time.strip(), "description": description.strip()})
        compatibility = target.setdefault("compatibility", {})
        compatibility["allOf"] = [{"property": "ro.hozon.car.code", "equals": car_code.strip()}, {"property": "ro.hozon.car.assembly.version", "equals": assembly_version.strip()}]
        compatibility["stockPackageVersionName"] = stock_package_version.strip()
        compatibility["stockApkSha256"] = stock_hash.strip().lower()

    def delete_version(self, data: dict[str, Any], entry_id: str, version_id: str) -> bool:
        entry = self.find(data, entry_id)
        if not entry:
            return False
        removed = next((v for v in entry.get("versions", []) if str(v.get("id", "")) == version_id), None)
        before = len(entry.get("versions", []))
        entry["versions"] = [v for v in entry.get("versions", []) if str(v.get("id", "")) != version_id]
        if removed and removed.get("apk") and not any(v.get("apk") == removed["apk"] for item in data.get("enhancements", []) for v in item.get("versions", [])):
            (self.assets_dir.parent / str(removed["apk"]).lstrip("/")).unlink(missing_ok=True)
        return len(entry["versions"]) != before

    def replace_version_apk(self, data: dict[str, Any], entry_id: str, version_id: str, source: Path) -> None:
        entry = self.find(data, entry_id)
        target = next((v for v in (entry or {}).get("versions", []) if str(v.get("id", "")) == version_id), None)
        if not entry or not target:
            raise ValueError("未找到增强版本")
        digest = self.sha256(source)
        directory = self.assets_dir / entry_id
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{digest[:16]}.apk"
        shutil.copyfile(source, directory / filename)
        old_path = str(target.get("apk", ""))
        target.update({"apk": f"/enhancements/{entry_id}/{filename}", "sha256": digest, "filesize": source.stat().st_size})
        if old_path and old_path != target["apk"] and not any(v.get("apk") == old_path for item in data.get("enhancements", []) for v in item.get("versions", [])):
            old_file = self.assets_dir.parent / old_path.lstrip("/")
            old_file.unlink(missing_ok=True)

    @staticmethod
    def find(data: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
        return next((item for item in data.get("enhancements", []) if str(item.get("id", "")) == entry_id), None)

    def delete(self, data: dict[str, Any], entry_id: str) -> bool:
        entries = data.setdefault("enhancements", [])
        selected = [item for item in entries if str(item.get("id", "")) == entry_id]
        if not selected:
            return False
        data["enhancements"] = [item for item in entries if str(item.get("id", "")) != entry_id]
        shutil.rmtree(self.assets_dir / entry_id, ignore_errors=True)
        return True

    @staticmethod
    def sha256(source: Path) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
