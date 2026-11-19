from __future__ import annotations

import hashlib
import json
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from pyaxmlparser import APK
    from pyaxmlparser.axmlprinter import AXMLPrinter
except ModuleNotFoundError:  # pragma: no cover - 依赖缺失时只影响 APK 解析相关能力
    APK = None
    AXMLPrinter = None

INCOMPATIBILITY_OPTIONS = ["ALL", "EP32", "EP36", "EP40", "EP41"]
ICON_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


@dataclass
class ApkMetadata:
    package: str
    version_name: str
    name: str


@dataclass
class WorkflowBundleMetadata:
    workflow_count: int
    workflow_names: list[str]
    tags: list[str]


def parse_apk_metadata(apk_path: Path) -> ApkMetadata:
    # 核心流程：直接解析 APK 的 AndroidManifest，拿到包名和版本信息
    _require_pyaxmlparser()
    parser = APK(str(apk_path))
    package_name = (parser.package or "").strip()
    if not package_name:
        raise ValueError("无法读取 packageName")

    version_name = (parser.version_name or "").strip() or "0"
    app_name = (parser.application or "").strip() or apk_path.stem
    return ApkMetadata(package=package_name, version_name=version_name, name=app_name)


def parse_workflow_bundle(workflow_path: Path) -> WorkflowBundleMetadata:
    # 核心流程：上传后先解析工作流概要，只把索引需要的摘要字段写进 workflows.json
    try:
        raw = json.loads(workflow_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"工作流 JSON 解析失败: {exc}") from exc

    if isinstance(raw, dict):
        items = [raw]
    elif isinstance(raw, list):
        items = raw
    else:
        raise ValueError("工作流文件必须是 JSON 对象或数组")

    workflow_names: list[str] = []
    tags: list[str] = []
    seen_tags: set[str] = set()

    for item in items:
        if not isinstance(item, dict):
            raise ValueError("工作流列表中的每一项都必须是 JSON 对象")
        workflow_block = item.get("workflow", {})
        if isinstance(workflow_block, dict):
            workflow_name = str(workflow_block.get("name", "")).strip()
            if workflow_name:
                workflow_names.append(workflow_name)
            config = workflow_block.get("config", {})
            if isinstance(config, dict):
                raw_tags = config.get("tags", [])
                if isinstance(raw_tags, list):
                    for tag in raw_tags:
                        value = str(tag).strip()
                        if value and value not in seen_tags:
                            tags.append(value)
                            seen_tags.add(value)

    return WorkflowBundleMetadata(
        workflow_count=len(items),
        workflow_names=workflow_names,
        tags=tags,
    )


def normalize_message_board(raw_items: Any) -> list[dict[str, Any]]:
    # 核心流程：只保存前端需要的 title/contents/text/canCopy，过滤空留言和未知字段
    if not isinstance(raw_items, list):
        return []

    result: list[dict[str, Any]] = []
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        title = str(raw_item.get("title", "")).strip()
        raw_contents = raw_item.get("contents", [])
        contents: list[dict[str, Any]] = []
        if isinstance(raw_contents, list):
            for raw_content in raw_contents:
                if not isinstance(raw_content, dict):
                    continue
                text = str(raw_content.get("text", "")).strip()
                if not text:
                    continue
                contents.append(
                    {
                        "text": text,
                        "canCopy": bool(raw_content.get("canCopy", False)),
                    }
                )
        if not title and not contents:
            continue
        result.append({"title": title, "contents": contents})
    return result


def extract_icon_from_apk(apk_path: Path, output_dir: Path, output_name: str = "icon") -> Path | None:
    """
    从 APK 中提取图标文件并写入 output_dir，返回生成的本地路径。
    提取顺序：
    1) Manifest 解析出的主图标
    2) 回退扫描 APK 内 ic_launcher 相关图片
    """
    if APK is None:
        return None
    try:
        parser = APK(str(apk_path))
    except Exception:  # noqa: BLE001
        return None
    candidate_scores: dict[str, int] = {}

    # 1) 走 Manifest -> resources 的精确解析，优先级最高
    for file_name in _resolve_manifest_icon_candidates(parser):
        if Path(file_name).suffix.lower() in ICON_EXTENSIONS:
            candidate_scores[file_name] = max(candidate_scores.get(file_name, -1), 100 + _icon_candidate_score(file_name))

    # 2) 兼容 pyaxmlparser 直接给出的 icon 文件路径
    icon_info = parser.get_app_icon()
    if icon_info and Path(icon_info).suffix.lower() in ICON_EXTENSIONS:
        candidate_scores[icon_info] = max(candidate_scores.get(icon_info, -1), 90 + _icon_candidate_score(icon_info))

    # 3) 回退兜底：按文件名关键词扫描
    for file_name in parser.get_files():
        lower = file_name.lower()
        ext = Path(lower).suffix
        if ext not in ICON_EXTENSIONS:
            continue
        if "ic_launcher" in lower or "app_icon" in lower or "/icon" in lower:
            candidate_scores[file_name] = max(candidate_scores.get(file_name, -1), _icon_candidate_score(file_name))

    if not candidate_scores:
        return None

    ranked_candidates = sorted(candidate_scores.keys(), key=lambda item: candidate_scores[item], reverse=True)
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


def _resolve_manifest_icon_candidates(parser: APK) -> list[str]:
    if not hasattr(parser, "get_android_resources"):
        return []
    res_parser = parser.get_android_resources()
    if not res_parser:
        return []

    refs: list[str] = []
    try:
        main_activity_name = parser.get_main_activity()
        if main_activity_name:
            activity_icon = parser.get_attribute_value("activity", "icon", name=main_activity_name)
            if activity_icon:
                refs.append(activity_icon)
    except Exception:  # noqa: BLE001
        pass

    try:
        app_icon = parser.get_attribute_value("application", "icon")
        if app_icon:
            refs.append(app_icon)
    except Exception:  # noqa: BLE001
        pass

    if not refs:
        for type_name in ("mipmap", "drawable"):
            try:
                res_id = res_parser.get_res_id_by_key(parser.package, type_name, "ic_launcher")
            except Exception:  # noqa: BLE001
                res_id = None
            if res_id:
                refs.append(f"@{res_id:x}")

    results: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        for file_name in _resolve_resource_ref_to_files(ref, parser, res_parser, depth=0, visited_refs=set()):
            if file_name not in seen:
                seen.add(file_name)
                results.append(file_name)
    return results


def _resolve_resource_ref_to_files(
    ref: str,
    parser: APK,
    res_parser: Any,
    depth: int,
    visited_refs: set[str],
) -> list[str]:
    if not ref or depth > 6:
        return []
    ref = ref.strip()
    if not ref:
        return []
    if ref in visited_refs:
        return []
    visited_refs.add(ref)

    ext = Path(ref).suffix.lower()
    if ext in ICON_EXTENSIONS:
        return [ref]
    if ext == ".xml":
        return _resolve_icon_files_from_adaptive_xml(ref, parser, res_parser, depth, visited_refs)

    if not ref.startswith("@"):
        return []

    res_id = _resolve_res_id_from_ref(ref, parser.package, res_parser)
    if not res_id:
        return []

    try:
        configs = res_parser.get_resolved_res_configs(res_id)
    except Exception:  # noqa: BLE001
        configs = []
    files: list[str] = []
    for item in configs:
        file_name = item[1] if isinstance(item, tuple) and len(item) > 1 else None
        if not isinstance(file_name, str):
            continue
        file_name = file_name.strip()
        if not file_name:
            continue
        item_ext = Path(file_name).suffix.lower()
        if item_ext in ICON_EXTENSIONS:
            files.append(file_name)
            continue
        if item_ext == ".xml":
            files.extend(_resolve_icon_files_from_adaptive_xml(file_name, parser, res_parser, depth + 1, visited_refs))
    return _dedupe_keep_order(files)


def _resolve_res_id_from_ref(ref: str, package_name: str, res_parser: Any) -> int | None:
    # @7f080123 / @android:7f080123 / @mipmap/ic_launcher / @android:mipmap/ic_launcher
    value = ref[1:]
    if ":" in value:
        _, value = value.split(":", 1)
    if "/" in value:
        type_name, name = value.split("/", 1)
        try:
            return res_parser.get_res_id_by_key(package_name, type_name, name)
        except Exception:  # noqa: BLE001
            return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _resolve_icon_files_from_adaptive_xml(
    xml_file: str,
    parser: APK,
    res_parser: Any,
    depth: int,
    visited_refs: set[str],
) -> list[str]:
    try:
        raw = parser.get_file(xml_file)
    except Exception:  # noqa: BLE001
        return []
    if not raw:
        return []

    xml_root = _parse_android_xml(raw)
    if xml_root is None:
        return []

    refs: list[str] = []
    for element in xml_root.iter():
        tag_name = _xml_local_name(element.tag).lower()
        if tag_name not in {"adaptive-icon", "foreground", "background", "monochrome"}:
            continue
        for attr_key, attr_value in element.attrib.items():
            if _xml_local_name(attr_key).lower() == "drawable" and isinstance(attr_value, str) and attr_value.strip():
                refs.append(attr_value.strip())

    files: list[str] = []
    for ref in refs:
        files.extend(_resolve_resource_ref_to_files(ref, parser, res_parser, depth + 1, visited_refs))
    return _dedupe_keep_order(files)


def _parse_android_xml(raw: bytes):
    stripped = raw.lstrip()
    if stripped.startswith(b"<"):
        try:
            return ET.fromstring(raw)
        except Exception:  # noqa: BLE001
            pass
    if AXMLPrinter is None:
        return None
    try:
        printer = AXMLPrinter(raw)
        if printer.is_valid():
            return printer.get_xml_obj()
    except Exception:  # noqa: BLE001
        pass
    return None


def _xml_local_name(name: Any) -> str:
    text = str(name or "")
    if "}" in text:
        return text.split("}", 1)[1]
    if ":" in text:
        return text.split(":", 1)[1]
    return text


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _require_pyaxmlparser() -> None:
    if APK is None:
        raise ModuleNotFoundError("缺少 pyaxmlparser，请先安装 requirements.txt 中的依赖")


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


def infer_workflow_code(entry: dict[str, Any] | None) -> str | None:
    if not entry:
        return None
    code = str(entry.get("code", "")).strip()
    if code:
        return code

    workflow_file = str(entry.get("file", "")).strip()
    parts = [segment for segment in workflow_file.split("/") if segment]
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
        self.save_data({"notice": "", "messageBoard": [], "categories": [], "apps": []})

    def load_data(self) -> dict[str, Any]:
        # 核心流程：统一兜底 notice/apps，避免历史 json 缺字段导致页面崩溃
        self.ensure_initialized()
        raw = self.json_path.read_text(encoding="utf-8").strip()
        if not raw:
            return {"notice": "", "messageBoard": [], "categories": [], "apps": []}
        data = json.loads(raw)
        if "notice" not in data:
            data["notice"] = ""
        if "messageBoard" not in data or not isinstance(data["messageBoard"], list):
            data["messageBoard"] = []
        else:
            data["messageBoard"] = normalize_message_board(data["messageBoard"])
        if "categories" not in data or not isinstance(data["categories"], list):
            data["categories"] = []
        if "apps" not in data or not isinstance(data["apps"], list):
            data["apps"] = []
        data["categories"] = self._dedupe_categories(data.get("categories", []))
        return data

    def save_data(self, data: dict[str, Any]) -> None:
        self.json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    @staticmethod
    def compute_md5(file_path: Path) -> str:
        digest = hashlib.md5()  # noqa: S324
        with file_path.open("rb") as fp:
            while True:
                chunk = fp.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def utc_now_iso() -> str:
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

    def reorder_entries(self, data: dict[str, Any], ordered_ids: list[str]) -> bool:
        # 核心流程：前端提交完整 id 顺序，后端只按已有条目重排，避免丢失应用
        apps = data.setdefault("apps", [])
        if not ordered_ids:
            return False

        by_id = {str(app.get("id", "")).strip(): app for app in apps if str(app.get("id", "")).strip()}
        ordered_unique: list[str] = []
        seen: set[str] = set()
        for raw_id in ordered_ids:
            entry_id = str(raw_id).strip()
            if not entry_id or entry_id in seen or entry_id not in by_id:
                continue
            ordered_unique.append(entry_id)
            seen.add(entry_id)

        if set(ordered_unique) != set(by_id):
            return False

        reordered = [by_id[entry_id] for entry_id in ordered_unique]
        if [str(app.get("id", "")).strip() for app in apps] == ordered_unique:
            return False
        data["apps"] = reordered
        return True

    def delete_entry(self, data: dict[str, Any], entry_id: str) -> bool:
        apps = data.setdefault("apps", [])
        for idx, app in enumerate(apps):
            if str(app.get("id", "")).strip() == entry_id:
                del apps[idx]
                return True
        return False

    def cleanup_entry_assets(self, data: dict[str, Any], deleted_entry: dict[str, Any]) -> bool:
        """
        删除应用后清理其 apks 目录；如果仍被其他应用引用则不删。
        """
        code = infer_existing_code(deleted_entry)
        if not code:
            return False
        for app in data.get("apps", []):
            if infer_existing_code(app) == code:
                return False
        target_dir = self.apks_dir / code
        if target_dir.is_dir():
            shutil.rmtree(target_dir, ignore_errors=True)
            return True
        return False

    def cleanup_orphan_asset_dirs(self, data: dict[str, Any]) -> list[str]:
        """
        清理 apks 目录下不在 appstore.json 应用列表中引用的目录。
        返回被删除的目录名列表。
        """
        referenced_codes = {infer_existing_code(app) for app in data.get("apps", [])}
        referenced_codes.discard(None)
        removed: list[str] = []
        for item in self.apks_dir.iterdir():
            if not item.is_dir():
                continue
            if item.name in referenced_codes:
                continue
            shutil.rmtree(item, ignore_errors=True)
            removed.append(item.name)
        removed.sort()
        return removed

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


class WorkflowManager:
    def __init__(self, json_path: Path, workflows_dir: Path) -> None:
        self.json_path = json_path
        self.workflows_dir = workflows_dir

    def ensure_initialized(self) -> None:
        self.workflows_dir.mkdir(parents=True, exist_ok=True)
        if self.json_path.exists():
            return
        self.save_data({"categories": [], "workflows": []})

    def load_data(self) -> dict[str, Any]:
        self.ensure_initialized()
        raw = self.json_path.read_text(encoding="utf-8").strip()
        if not raw:
            return {"categories": [], "workflows": []}
        data = json.loads(raw)
        if "categories" not in data or not isinstance(data["categories"], list):
            data["categories"] = []
        if "workflows" not in data or not isinstance(data["workflows"], list):
            data["workflows"] = []
        data["categories"] = AppStoreManager._dedupe_categories(data.get("categories", []))
        return data

    def save_data(self, data: dict[str, Any]) -> None:
        self.json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_codes(self) -> list[str]:
        data = self.load_data()
        codes: list[str] = []
        for entry in data.get("workflows", []):
            code = infer_workflow_code(entry)
            if code:
                codes.append(code)
        return codes

    def ensure_category(self, data: dict[str, Any], category: str) -> None:
        categories = data.setdefault("categories", [])
        normalized = normalize_category(category)
        if not normalized:
            return
        if normalized not in categories:
            categories.append(normalized)

    def replace_categories(self, data: dict[str, Any], categories: list[str]) -> None:
        data["categories"] = AppStoreManager._dedupe_categories(categories)

    def resolve_code(self, workflow_name: str, file_md5: str, preferred_code: str | None = None) -> str:
        existing_codes = set(self.list_codes())
        if preferred_code:
            return preferred_code

        base = AppStoreManager._slugify(workflow_name) or "workflow"
        suffix = (file_md5 or uuid.uuid4().hex)[:6]
        candidate = f"{base}-{suffix}"
        if candidate not in existing_codes:
            return candidate

        index = 2
        while f"{candidate}-{index}" in existing_codes:
            index += 1
        return f"{candidate}-{index}"

    def upsert_entry(self, data: dict[str, Any], entry: dict[str, Any]) -> None:
        workflows = data.setdefault("workflows", [])
        entry_id = str(entry.get("id", "")).strip()
        for idx, item in enumerate(workflows):
            if entry_id and str(item.get("id", "")).strip() == entry_id:
                workflows[idx] = entry
                return
        workflows.append(entry)
        workflows.sort(key=lambda item: str(item.get("name", "")).lower())

    def delete_entry(self, data: dict[str, Any], entry_id: str) -> bool:
        workflows = data.setdefault("workflows", [])
        for idx, item in enumerate(workflows):
            if str(item.get("id", "")).strip() == entry_id:
                del workflows[idx]
                return True
        return False

    def cleanup_entry_assets(self, data: dict[str, Any], deleted_entry: dict[str, Any]) -> bool:
        code = infer_workflow_code(deleted_entry)
        if not code:
            return False
        for item in data.get("workflows", []):
            if infer_workflow_code(item) == code:
                return False
        target_dir = self.workflows_dir / code
        if target_dir.is_dir():
            shutil.rmtree(target_dir, ignore_errors=True)
            return True
        return False

    def backfill_entry_metadata(self, entry: dict[str, Any]) -> bool:
        changed = False
        workflow_rel = str(entry.get("file", "")).strip()
        if not workflow_rel:
            return False
        workflow_file = self.workflows_dir / workflow_rel.lstrip("/")
        if not workflow_file.is_file():
            return False

        if not str(entry.get("md5sum", "")).strip():
            entry["md5sum"] = AppStoreManager.compute_md5(workflow_file)
            changed = True
        if not entry.get("filesize"):
            entry["filesize"] = workflow_file.stat().st_size
            changed = True
        if not str(entry.get("updateTime", "")).strip():
            entry["updateTime"] = AppStoreManager.utc_now_iso()
            changed = True

        code = str(entry.get("code", "")).strip()
        inferred_code = infer_workflow_code(entry)
        if not code and inferred_code:
            entry["code"] = inferred_code
            changed = True

        if not str(entry.get("filename", "")).strip():
            entry["filename"] = workflow_file.name
            changed = True
        if not str(entry.get("name", "")).strip():
            entry["name"] = workflow_file.stem
            changed = True

        need_parse = (
            not entry.get("workflowCount")
            or not isinstance(entry.get("workflowNames"), list)
            or not isinstance(entry.get("tags"), list)
        )
        if need_parse:
            metadata = parse_workflow_bundle(workflow_file)
            entry["workflowCount"] = metadata.workflow_count
            entry["workflowNames"] = metadata.workflow_names
            entry["tags"] = metadata.tags
            changed = True

        if "author" not in entry:
            entry["author"] = ""
            changed = True
        if "description" not in entry:
            entry["description"] = ""
            changed = True
        if not str(entry.get("category", "")).strip():
            entry["category"] = "未分类"
            changed = True

        return changed

    def ensure_entry_ids(self, data: dict[str, Any]) -> bool:
        workflows = data.setdefault("workflows", [])
        existing_ids: set[str] = set()
        changed = False
        for item in workflows:
            entry_id = str(item.get("id", "")).strip()
            if entry_id and entry_id not in existing_ids:
                existing_ids.add(entry_id)
                continue
            new_id = AppStoreManager.generate_entry_id(existing_ids)
            item["id"] = new_id
            existing_ids.add(new_id)
            changed = True
        return changed
