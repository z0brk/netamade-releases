from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_SOUND_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"}
MAX_SOUND_FILE_SIZE = 50 * 1024 * 1024


def normalize_tags(values: list[str] | str) -> list[str]:
    raw_values = values.split(",") if isinstance(values, str) else values
    result: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        value = re.sub(r"\s+", " ", str(raw_value)).strip()
        if not value or value in seen:
            continue
        result.append(value)
        seen.add(value)
    return result


class SoundStoreManager:
    def __init__(self, json_path: Path, sounds_dir: Path) -> None:
        self.json_path = json_path
        self.sounds_dir = sounds_dir

    def ensure_initialized(self) -> None:
        self.sounds_dir.mkdir(parents=True, exist_ok=True)
        if not self.json_path.exists():
            self.save_data({"schemaVersion": 1, "sounds": []})

    def load_data(self) -> dict[str, Any]:
        self.ensure_initialized()
        raw = self.json_path.read_text(encoding="utf-8").strip()
        if not raw:
            return {"schemaVersion": 1, "sounds": []}
        data = json.loads(raw)
        if not isinstance(data.get("schemaVersion"), int):
            data["schemaVersion"] = 1
        if not isinstance(data.get("sounds"), list):
            data["sounds"] = []
        for entry in data["sounds"]:
            entry["tags"] = normalize_tags(entry.get("tags", []))
        return data

    def save_data(self, data: dict[str, Any]) -> None:
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{self.json_path.name}.", dir=self.json_path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            os.replace(tmp_name, self.json_path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise

    def create_entry(self, source: Path, original_filename: str, tags: list[str]) -> tuple[dict[str, Any], Path]:
        filename = self._safe_display_filename(original_filename)
        extension = Path(filename).suffix.lower()
        if extension not in SUPPORTED_SOUND_EXTENSIONS:
            raise ValueError(f"不支持的音频格式: {original_filename}")
        filesize = source.stat().st_size
        if filesize <= 0:
            raise ValueError(f"音频文件为空: {original_filename}")
        if filesize > MAX_SOUND_FILE_SIZE:
            raise ValueError(f"音频文件超过 50 MiB: {original_filename}")

        entry_id = uuid.uuid4().hex[:12]
        code = f"sound-{entry_id[:8]}"
        digest = self.compute_md5(source)
        target_dir = self.sounds_dir / code
        target_dir.mkdir(parents=True, exist_ok=False)
        storage_name = f"{digest[:12]}{extension}"
        shutil.copyfile(source, target_dir / storage_name)
        return (
            {
                "id": entry_id,
                "name": Path(filename).stem,
                "filename": filename,
                "tags": normalize_tags(tags),
                "file": f"/{code}/{storage_name}",
                "md5sum": digest,
                "filesize": filesize,
                "updateTime": self.utc_now_iso(),
            },
            target_dir,
        )

    def update_entry(self, data: dict[str, Any], entry_id: str, name: str, tags: list[str]) -> bool:
        entry = self.find_entry(data, entry_id)
        if entry is None:
            return False
        entry["name"] = name.strip() or str(entry.get("name", "")).strip() or "未命名音效"
        entry["tags"] = normalize_tags(tags)
        return True

    def reorder_entries(self, data: dict[str, Any], ordered_ids: list[str]) -> bool:
        sounds = data.setdefault("sounds", [])
        existing_ids = [str(item.get("id", "")).strip() for item in sounds]
        normalized = [str(item).strip() for item in ordered_ids]
        if len(normalized) != len(set(normalized)) or set(normalized) != set(existing_ids):
            return False
        if normalized == existing_ids:
            return False
        by_id = {str(item.get("id", "")).strip(): item for item in sounds}
        data["sounds"] = [by_id[entry_id] for entry_id in normalized]
        return True

    def update_tags(self, data: dict[str, Any], entry_ids: list[str], add: list[str], remove: list[str]) -> int:
        selected = {entry_id.strip() for entry_id in entry_ids if entry_id.strip()}
        add_tags = normalize_tags(add)
        remove_tags = set(normalize_tags(remove))
        changed = 0
        for entry in data.setdefault("sounds", []):
            if str(entry.get("id", "")).strip() not in selected:
                continue
            before = normalize_tags(entry.get("tags", []))
            after = [tag for tag in before if tag not in remove_tags]
            after = normalize_tags(after + add_tags)
            if after != before:
                entry["tags"] = after
                changed += 1
        return changed

    def delete_entries(self, data: dict[str, Any], entry_ids: list[str]) -> list[dict[str, Any]]:
        selected = {entry_id.strip() for entry_id in entry_ids if entry_id.strip()}
        deleted = [item for item in data.setdefault("sounds", []) if str(item.get("id", "")).strip() in selected]
        data["sounds"] = [item for item in data["sounds"] if str(item.get("id", "")).strip() not in selected]
        return deleted

    def cleanup_entry_assets(self, data: dict[str, Any], deleted_entries: list[dict[str, Any]]) -> None:
        referenced = {self._entry_code(item) for item in data.get("sounds", [])}
        for entry in deleted_entries:
            code = self._entry_code(entry)
            if code and code not in referenced:
                shutil.rmtree(self.sounds_dir / code, ignore_errors=True)

    @staticmethod
    def find_entry(data: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in data.get("sounds", []) if str(item.get("id", "")).strip() == entry_id),
            None,
        )

    @staticmethod
    def compute_md5(file_path: Path) -> str:
        digest = hashlib.md5()  # noqa: S324
        with file_path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def utc_now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _safe_display_filename(filename: str) -> str:
        value = (filename or "").replace("\\", "/").split("/")[-1].replace("\x00", "").strip()
        if not value or value in {".", ".."}:
            raise ValueError("音频文件名无效")
        return value

    @staticmethod
    def _entry_code(entry: dict[str, Any]) -> str | None:
        parts = [part for part in str(entry.get("file", "")).split("/") if part]
        code = parts[-2] if len(parts) >= 2 else ""
        return code if re.fullmatch(r"[a-zA-Z0-9._-]+", code) and code not in {".", ".."} else None
