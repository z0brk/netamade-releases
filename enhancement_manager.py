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
        return data

    def save_data(self, data: dict[str, Any]) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.json_path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, self.json_path)

    def add_upload(self, data: dict[str, Any], source: Path, name: str, package: str, target: str, version: str, description: str, car_code: str, assembly_version: str, stock_hash: str) -> None:
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
            "version": version.strip() or "1.0.0",
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
