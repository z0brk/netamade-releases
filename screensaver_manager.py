from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SUPPORTED_MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".mov", ".mkv", ".webm"}
SUITABILITIES = {"SUITABLE", "CANDIDATE", "UNSUITABLE"}
TARGETS = {"L_MAIN_FULL", "L_MAIN_HALF", "L_SECONDARY_FULL", "S_GT_MAIN", "S_GT_SECONDARY"}


class ScreensaverManager:
    def __init__(self, json_path: Path, screensavers_dir: Path) -> None:
        self.json_path = json_path
        self.screensavers_dir = screensavers_dir

    def ensure_initialized(self) -> None:
        self.screensavers_dir.mkdir(parents=True, exist_ok=True)
        if not self.json_path.exists():
            self.save_data({"schemaVersion": 1, "screensavers": []})

    def load_data(self) -> dict[str, Any]:
        self.ensure_initialized()
        data = json.loads(self.json_path.read_text(encoding="utf-8") or "{}")
        data.setdefault("schemaVersion", 1)
        data.setdefault("screensavers", [])
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

    def prepare_entry(
        self,
        source: Path,
        original_filename: str,
        suitability: str,
        targets: list[str],
        staging_root: Path,
    ) -> tuple[dict[str, Any], Path]:
        filename = self._safe_filename(original_filename)
        extension = Path(filename).suffix.lower()
        if extension not in SUPPORTED_MEDIA_EXTENSIONS:
            raise ValueError(f"不支持的屏保格式: {filename}")
        normalized_suitability = suitability.strip().upper()
        if normalized_suitability not in SUITABILITIES:
            raise ValueError(f"无效适配状态: {suitability}")
        normalized_targets = list(dict.fromkeys(target.strip().upper() for target in targets if target.strip()))
        if any(target not in TARGETS for target in normalized_targets):
            raise ValueError("包含无效适配目标")
        if normalized_suitability != "UNSUITABLE" and not normalized_targets:
            raise ValueError("适合和候选屏保必须选择至少一个目标")

        media = self.probe_media(source)
        entry_id = uuid.uuid4().hex[:12]
        code = f"screensaver-{entry_id[:8]}"
        entry_dir = staging_root / code
        entry_dir.mkdir(parents=True, exist_ok=False)
        digest = self.compute_md5(source)
        storage_name = f"{digest[:12]}{extension}"
        shutil.copyfile(source, entry_dir / storage_name)
        self.generate_preview(source, entry_dir / "preview.webp")
        return (
            {
                "id": entry_id,
                "name": self.clean_name(filename),
                "type": media["type"],
                "width": media["width"],
                "height": media["height"],
                "suitability": normalized_suitability,
                "targets": normalized_targets,
                "file": f"/{code}/{storage_name}",
                "preview": f"/{code}/preview.webp",
                "md5sum": digest,
                "filesize": source.stat().st_size,
                "updateTime": self.utc_now_iso(),
            },
            entry_dir,
        )

    def commit_batch(self, data: dict[str, Any], entries: list[dict[str, Any]], staged_dirs: list[Path]) -> None:
        moved: list[Path] = []
        try:
            self.screensavers_dir.mkdir(parents=True, exist_ok=True)
            for staged_dir in staged_dirs:
                target = self.screensavers_dir / staged_dir.name
                if target.exists():
                    raise FileExistsError(target)
                os.replace(staged_dir, target)
                moved.append(target)
            data.setdefault("screensavers", []).extend(entries)
            self.save_data(data)
        except Exception:
            for target in moved:
                shutil.rmtree(target, ignore_errors=True)
            raise

    def update_entry(self, data: dict[str, Any], entry_id: str, name: str, suitability: str, targets: list[str]) -> bool:
        entry = self.find_entry(data, entry_id)
        normalized = suitability.strip().upper()
        normalized_targets = list(dict.fromkeys(target.strip().upper() for target in targets if target.strip()))
        if entry is None:
            return False
        if normalized not in SUITABILITIES or any(target not in TARGETS for target in normalized_targets):
            raise ValueError("无效适配信息")
        if normalized != "UNSUITABLE" and not normalized_targets:
            raise ValueError("适合和候选屏保必须选择至少一个目标")
        entry["name"] = name.strip() or entry.get("name") or "未命名屏保"
        entry["suitability"] = normalized
        entry["targets"] = normalized_targets
        return True

    def reorder_entries(self, data: dict[str, Any], ordered_ids: list[str]) -> bool:
        entries = data.setdefault("screensavers", [])
        existing = [str(item.get("id", "")) for item in entries]
        normalized = [str(item).strip() for item in ordered_ids]
        if len(normalized) != len(set(normalized)) or set(normalized) != set(existing):
            return False
        if normalized == existing:
            return False
        by_id = {str(item.get("id", "")): item for item in entries}
        data["screensavers"] = [by_id[item] for item in normalized]
        return True

    def delete_entries(self, data: dict[str, Any], entry_ids: list[str]) -> list[dict[str, Any]]:
        selected = {item.strip() for item in entry_ids if item.strip()}
        entries = data.setdefault("screensavers", [])
        deleted = [item for item in entries if str(item.get("id", "")) in selected]
        data["screensavers"] = [item for item in entries if str(item.get("id", "")) not in selected]
        return deleted

    def cleanup_entry_assets(self, entries: list[dict[str, Any]]) -> None:
        for entry in entries:
            code = self._entry_code(entry)
            if code:
                shutil.rmtree(self.screensavers_dir / code, ignore_errors=True)

    @staticmethod
    def find_entry(data: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
        return next((item for item in data.get("screensavers", []) if str(item.get("id", "")) == entry_id), None)

    @staticmethod
    def probe_media(source: Path) -> dict[str, Any]:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_type,width,height,nb_frames", "-of", "json", str(source)],
            check=True,
            capture_output=True,
            text=True,
        )
        streams = json.loads(result.stdout).get("streams", [])
        if not streams:
            raise ValueError(f"无法读取媒体画面: {source.name}")
        stream = streams[0]
        width, height = int(stream.get("width", 0)), int(stream.get("height", 0))
        if width <= 0 or height <= 0:
            raise ValueError(f"媒体尺寸无效: {source.name}")
        extension = source.suffix.lower()
        media_type = "VIDEO" if extension in {".mp4", ".mov", ".mkv", ".webm"} else "IMAGE"
        return {"type": media_type, "width": width, "height": height}

    @staticmethod
    def generate_preview(source: Path, target: Path) -> None:
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-frames:v", "1", "-vf", "scale='min(480,iw)':'min(480,ih)':force_original_aspect_ratio=decrease", "-c:v", "libwebp", "-quality", "75", str(target)],
            check=True,
            capture_output=True,
        )
        if not target.is_file() or target.stat().st_size <= 0:
            raise ValueError(f"缩略图生成失败: {source.name}")

    @staticmethod
    def clean_name(filename: str) -> str:
        return re.sub(r"(?:_?\[[^\]]+\])+$", "", Path(filename).stem).strip(" _-") or Path(filename).stem

    @staticmethod
    def compute_md5(source: Path) -> str:
        digest = hashlib.md5()  # noqa: S324
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def utc_now_iso() -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _safe_filename(filename: str) -> str:
        value = (filename or "").replace("\\", "/").split("/")[-1].replace("\x00", "").strip()
        if not value or value in {".", ".."}:
            raise ValueError("屏保文件名无效")
        return value

    @staticmethod
    def _entry_code(entry: dict[str, Any]) -> str | None:
        parts = [part for part in str(entry.get("file", "")).split("/") if part]
        code = parts[-2] if len(parts) >= 2 else ""
        return code if re.fullmatch(r"screensaver-[a-zA-Z0-9]+", code) else None
