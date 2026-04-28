from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyaxmlparser import APK

INCOMPATIBILITY_OPTIONS = ["ALL", "EP32", "EP36", "EP40", "EP41"]
ICON_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass
class ApkMetadata:
    package: str
    version_name: str
    name: str


def parse_apk_metadata(apk_path: Path) -> ApkMetadata:
    # 核心流程：直接解析 APK 的 AndroidManifest，拿到包名和版本信息
    parser = APK(str(apk_path))
    package_name = (parser.package or "").strip()
    if not package_name:
        raise ValueError("无法读取 packageName")

    version_name = (parser.version_name or "").strip() or "0"
    app_name = (parser.application or "").strip() or apk_path.stem
    return ApkMetadata(package=package_name, version_name=version_name, name=app_name)


def extract_icon_from_apk(apk_path: Path, output_dir: Path, output_name: str = "icon") -> Path | None:
    """
    从 APK 中提取图标文件并写入 output_dir，返回生成的本地路径。
    提取顺序：
    1) Manifest 解析出的主图标
    2) 回退扫描 APK 内 ic_launcher 相关图片
    """
    try:
        parser = APK(str(apk_path))
    except Exception:  # noqa: BLE001
        return None
    candidates: list[str] = []

    icon_info = parser.get_app_icon()
    if icon_info and Path(icon_info).suffix.lower() in ICON_EXTENSIONS:
        candidates.append(icon_info)

    for file_name in parser.get_files():
        lower = file_name.lower()
        ext = Path(lower).suffix
        if ext not in ICON_EXTENSIONS:
            continue
        if "ic_launcher" in lower or "app_icon" in lower or "/icon" in lower:
            candidates.append(file_name)

    if not candidates:
        return None

    ranked_candidates = sorted(set(candidates), key=_icon_candidate_score, reverse=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    for icon_file in ranked_candidates:
        try:
            icon_data = parser.get_file(icon_file)
        except Exception:  # noqa: BLE001
            continue
        if not icon_data:
            continue
        ext = Path(icon_file).suffix.lower()
        if ext not in ICON_EXTENSIONS:
            ext = ".png"
        output_path = output_dir / f"{output_name}{ext}"
        output_path.write_bytes(icon_data)
        return output_path

    return None


def normalize_incompatibility(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    valid_values = [value for value in INCOMPATIBILITY_OPTIONS if value in values]
    for value in valid_values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def find_existing_entry(apps: list[dict[str, Any]], package_name: str) -> dict[str, Any] | None:
    for app in apps:
        if app.get("package") == package_name:
            return app
    return None


def find_entry_by_id(apps: list[dict[str, Any]], entry_id: str) -> dict[str, Any] | None:
    for app in apps:
        if str(app.get("id", "")).strip() == entry_id:
            return app
    return None


def infer_existing_code(entry: dict[str, Any] | None) -> str | None:
    if not entry:
        return None
    code = (entry.get("code") or "").strip()
    if code:
        return code

    apk = str(entry.get("apk", "")).strip()
    parts = [segment for segment in apk.split("/") if segment]
    if len(parts) >= 2:
        return parts[-2]
    return None


def normalize_category(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def _icon_candidate_score(path: str) -> int:
    lower = path.lower()
    score = 0
    if "/mipmap" in lower:
        score += 30
    if "/drawable" in lower:
        score += 20
    if "ic_launcher" in lower:
        score += 40
    density_score = {
        "xxxhdpi": 18,
        "xxhdpi": 16,
        "xhdpi": 14,
        "hdpi": 12,
        "mdpi": 10,
        "ldpi": 8,
        "anydpi": 6,
        "nodpi": 4,
    }
    for density, value in density_score.items():
        if density in lower:
            score += value
            break
    return score


class AppStoreManager:
    def __init__(self, json_path: Path, apks_dir: Path) -> None:
        self.json_path = json_path
        self.apks_dir = apks_dir

    def ensure_initialized(self) -> None:
        self.apks_dir.mkdir(parents=True, exist_ok=True)
        if self.json_path.exists():
            return
        self.save_data({"notice": "", "categories": [], "apps": []})

    def load_data(self) -> dict[str, Any]:
        # 核心流程：统一兜底 notice/apps，避免历史 json 缺字段导致页面崩溃
        self.ensure_initialized()
        raw = self.json_path.read_text(encoding="utf-8").strip()
        if not raw:
            return {"notice": "", "categories": [], "apps": []}
        data = json.loads(raw)
        if "notice" not in data:
            data["notice"] = ""
        if "categories" not in data or not isinstance(data["categories"], list):
            data["categories"] = []
        if "apps" not in data or not isinstance(data["apps"], list):
            data["apps"] = []
        data["categories"] = self._dedupe_categories(data.get("categories", []))
        return data

    def save_data(self, data: dict[str, Any]) -> None:
        self.json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def compute_md5(self, file_path: Path) -> str:
        digest = hashlib.md5()  # noqa: S324
        with file_path.open("rb") as fp:
            while True:
                chunk = fp.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def utc_now_iso(self) -> str:
        return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def resolve_code(self, package_name: str, app_name: str, preferred_code: str | None = None) -> str:
        # 核心流程：已存在应用优先复用旧代号，避免客户端缓存路径失效
        existing_codes = set(self.list_codes())
        if preferred_code:
            return preferred_code

        base = self._slugify(package_name.split(".")[-1] or app_name)
        if not base:
            base = "app"

        suffix = hashlib.md5(package_name.encode("utf-8")).hexdigest()[:6]  # noqa: S324
        candidate = f"{base}-{suffix}"
        if candidate not in existing_codes:
            return candidate

        index = 2
        while f"{candidate}-{index}" in existing_codes:
            index += 1
        return f"{candidate}-{index}"

    def upsert_entry(self, data: dict[str, Any], entry: dict[str, Any]) -> None:
        # 核心流程：以 id 为唯一键，允许同 package 的多个应用并存
        apps = data.setdefault("apps", [])
        entry_id = str(entry.get("id", "")).strip()
        for idx, app in enumerate(apps):
            if entry_id and str(app.get("id", "")).strip() == entry_id:
                apps[idx] = entry
                return
        apps.append(entry)
        apps.sort(key=lambda item: str(item.get("name", "")).lower())

    def delete_entry(self, data: dict[str, Any], entry_id: str) -> bool:
        apps = data.setdefault("apps", [])
        for idx, app in enumerate(apps):
            if str(app.get("id", "")).strip() == entry_id:
                del apps[idx]
                return True
        return False

    def ensure_category(self, data: dict[str, Any], category: str) -> None:
        categories = data.setdefault("categories", [])
        normalized = normalize_category(category)
        if not normalized:
            return
        if normalized not in categories:
            categories.append(normalized)

    def replace_categories(self, data: dict[str, Any], categories: list[str]) -> None:
        # 核心流程：按用户输入顺序保存分类数组，只做去重与空值过滤
        data["categories"] = self._dedupe_categories(categories)

    def list_codes(self) -> list[str]:
        data = self.load_data()
        codes: list[str] = []
        for app in data.get("apps", []):
            code = infer_existing_code(app)
            if code:
                codes.append(code)
        return codes

    def backfill_entry_metadata(self, entry: dict[str, Any]) -> bool:
        """
        从 apk 文件回填缺失元数据。仅在字段缺失时执行，避免每次请求都重算。
        返回值表示 entry 是否发生变化。
        """
        changed = False
        apk_rel = str(entry.get("apk", "")).strip()
        if not apk_rel:
            return False
        apk_file = self.apks_dir / apk_rel.lstrip("/")
        if not apk_file.is_file():
            return False

        if not str(entry.get("md5sum", "")).strip():
            entry["md5sum"] = self.compute_md5(apk_file)
            changed = True
        if not entry.get("filesize"):
            entry["filesize"] = apk_file.stat().st_size
            changed = True
        if not str(entry.get("updateTime", "")).strip():
            entry["updateTime"] = self.utc_now_iso()
            changed = True

        code = str(entry.get("code", "")).strip()
        inferred_code = infer_existing_code(entry)
        if not code and inferred_code:
            entry["code"] = inferred_code
            changed = True
            code = inferred_code

        if not str(entry.get("icon", "")).strip() and code:
            app_dir = self.apks_dir / code
            extracted_icon = extract_icon_from_apk(apk_file, app_dir)
            if extracted_icon:
                entry["icon"] = f"/{code}/{extracted_icon.name}"
                changed = True

        need_parse = any(
            not str(entry.get(field, "")).strip()
            for field in ("package", "versionName", "name")
        )
        if need_parse:
            try:
                metadata = parse_apk_metadata(apk_file)
            except Exception:  # noqa: BLE001
                metadata = None
            if metadata:
                if not str(entry.get("package", "")).strip():
                    entry["package"] = metadata.package
                    changed = True
                if not str(entry.get("versionName", "")).strip():
                    entry["versionName"] = metadata.version_name
                    changed = True
                if not str(entry.get("name", "")).strip():
                    entry["name"] = metadata.name
                    changed = True

        return changed

    def ensure_entry_ids(self, data: dict[str, Any]) -> bool:
        apps = data.setdefault("apps", [])
        existing_ids: set[str] = set()
        changed = False
        for app in apps:
            app_id = str(app.get("id", "")).strip()
            if app_id and app_id not in existing_ids:
                existing_ids.add(app_id)
                continue
            new_id = self.generate_entry_id(existing_ids)
            app["id"] = new_id
            existing_ids.add(new_id)
            changed = True
        return changed

    @staticmethod
    def generate_entry_id(existing_ids: set[str] | None = None) -> str:
        existing = existing_ids or set()
        while True:
            candidate = uuid.uuid4().hex[:12]
            if candidate not in existing:
                return candidate

    @staticmethod
    def _slugify(value: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").lower()
        return slug

    @staticmethod
    def _dedupe_categories(categories: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for item in categories:
            value = normalize_category(item)
            if not value or value in seen:
                continue
            result.append(value)
            seen.add(value)
        return result
