from __future__ import annotations

import argparse
import os
import shutil
import tempfile
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template, request, send_from_directory, url_for
from werkzeug.utils import secure_filename

from appstore_manager import (
    INCOMPATIBILITY_OPTIONS,
    AppStoreManager,
    extract_icon_from_apk,
    find_entry_by_id,
    normalize_category,
    normalize_incompatibility,
    parse_apk_metadata,
)

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR.parent
APPSTORE_JSON_PATH = REPO_ROOT / "appstore.json"
APKS_DIR = REPO_ROOT / "apks"


def is_supported_apk_filename(filename: str) -> bool:
    lower = filename.lower()
    return lower.endswith(".apk") or lower.endswith(".apk.1")

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APPSTORE_CREATOR_SECRET", "appstore-creator-dev")
# 限制单次上传大小，避免误上传超大文件占满磁盘
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GiB

manager = AppStoreManager(json_path=APPSTORE_JSON_PATH, apks_dir=APKS_DIR)


def preview_asset_url(path: str) -> str:
    value = (path or "").strip()
    if not value:
        return ""
    if value.startswith("http://") or value.startswith("https://"):
        return value
    return url_for("preview_asset", asset_path=value.lstrip("/"))


@app.get("/preview/<path:asset_path>")
def preview_asset(asset_path: str):
    # 只允许读取 apks 目录内的文件，避免路径穿越
    safe_path = Path(asset_path)
    if safe_path.is_absolute() or ".." in safe_path.parts:
        abort(400)
    target = APKS_DIR / safe_path
    if not target.is_file():
        abort(404)
    return send_from_directory(str(APKS_DIR), asset_path)


@app.get("/")
def index() -> str:
    data = manager.load_data()
    changed = False
    if manager.ensure_entry_ids(data):
        changed = True
    for app_entry in data.get("apps", []):
        if "images" in app_entry:
            app_entry.pop("images", None)
            changed = True
        if manager.backfill_entry_metadata(app_entry):
            changed = True
    if changed:
        manager.save_data(data)
    auto_edit_id = request.args.get("edit", "").strip()
    auto_edit_app = find_entry_by_id(data.get("apps", []), auto_edit_id) if auto_edit_id else None
    return render_template(
        "index.html",
        apps=data.get("apps", []),
        notice=data.get("notice", ""),
        categories=data.get("categories", []),
        incompatibility_options=INCOMPATIBILITY_OPTIONS,
        preview_asset_url=preview_asset_url,
        auto_edit_id=auto_edit_id,
        auto_edit_app=auto_edit_app,
    )


@app.post("/upload")
def upload_apk():
    apk_file = request.files.get("apk")
    if apk_file is None or not apk_file.filename:
        flash("请选择 APK 文件", "error")
        return redirect(url_for("index"))

    apk_filename = secure_filename(apk_file.filename)
    if not is_supported_apk_filename(apk_filename):
        flash("仅支持上传 .apk 或 .apk.1 文件", "error")
        return redirect(url_for("index"))

    tmp_dir = Path(tempfile.mkdtemp(prefix="appstore-apk-"))
    tmp_apk_path = tmp_dir / apk_filename

    try:
        apk_file.save(tmp_apk_path)
        metadata = parse_apk_metadata(tmp_apk_path)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        flash(f"APK 解析失败: {exc}", "error")
        return redirect(url_for("index"))

    data = manager.load_data()
    manager.ensure_entry_ids(data)
    generated_code = manager.resolve_code(
        package_name=metadata.package,
        app_name=request.form.get("name", "").strip() or metadata.name,
        preferred_code=None,
    )

    app_dir = APKS_DIR / generated_code
    app_dir.mkdir(parents=True, exist_ok=True)

    # 核心流程：先落 APK，再计算元数据，保证 json 中记录与磁盘文件一致
    apk_storage_name = secure_filename(apk_filename) or f"{generated_code}.apk"
    apk_final_path = app_dir / apk_storage_name
    shutil.copyfile(tmp_apk_path, apk_final_path)

    # 核心流程：icon 始终优先从 APK 自动提取，避免依赖手动上传图片
    icon_path = ""
    extracted_icon = extract_icon_from_apk(tmp_apk_path, app_dir)
    if extracted_icon:
        icon_path = f"/{generated_code}/{extracted_icon.name}"

    incompatibility = normalize_incompatibility(request.form.getlist("incompatibility"))
    description = request.form.get("description", "").strip()
    app_name = request.form.get("name", "").strip() or metadata.name
    category = normalize_category(request.form.get("category", ""))
    if not category:
        category = "未分类"
    manager.ensure_category(data, category)

    entry_id = manager.generate_entry_id({str(item.get("id", "")).strip() for item in data.get("apps", [])})
    entry = {
        "id": entry_id,
        "code": generated_code,
        "name": app_name,
        "category": category,
        "icon": icon_path,
        "package": metadata.package,
        "md5sum": manager.compute_md5(apk_final_path),
        "filesize": apk_final_path.stat().st_size,
        "updateTime": manager.utc_now_iso(),
        "versionName": metadata.version_name,
        "description": description,
        "incompatibility": incompatibility,
        "apk": f"/{generated_code}/{apk_storage_name}",
    }

    manager.upsert_entry(data, entry)
    manager.save_data(data)

    shutil.rmtree(tmp_dir, ignore_errors=True)
    flash(
        f"已解析并更新 {app_name} | package={metadata.package} | version={metadata.version_name} | md5={entry['md5sum']}",
        "success",
    )
    return redirect(url_for("index", edit=entry_id))


@app.post("/categories")
def save_categories():
    raw_categories = request.form.get("categories", "")
    categories = [line for line in (item.strip() for item in raw_categories.splitlines()) if line]
    data = manager.load_data()
    manager.replace_categories(data, categories)
    manager.save_data(data)
    flash("分类列表已更新", "success")
    return redirect(url_for("index"))


@app.post("/notice")
def save_notice():
    data = manager.load_data()
    data["notice"] = request.form.get("notice", "").strip()
    manager.save_data(data)
    flash("公告已更新", "success")
    return redirect(url_for("index"))


@app.post("/apps/update")
def update_app():
    entry_id = request.form.get("app_id", "").strip()
    if not entry_id:
        flash("缺少 app_id", "error")
        return redirect(url_for("index"))

    data = manager.load_data()
    manager.ensure_entry_ids(data)
    entry = find_entry_by_id(data.get("apps", []), entry_id)
    if not entry:
        flash(f"未找到应用: {entry_id}", "error")
        return redirect(url_for("index"))

    app_name = request.form.get("name", "").strip() or str(entry.get("name", ""))
    description = request.form.get("description", "").strip()
    category = normalize_category(request.form.get("category", "")) or "未分类"
    incompatibility = normalize_incompatibility(request.form.getlist("incompatibility"))
    package_name = request.form.get("package", "").strip() or str(entry.get("package", "")).strip()
    code = request.form.get("code", "").strip() or str(entry.get("code", "")).strip()
    icon = request.form.get("icon", "").strip()
    apk = request.form.get("apk", "").strip()
    version_name = request.form.get("versionName", "").strip()
    md5sum = request.form.get("md5sum", "").strip()
    filesize_raw = request.form.get("filesize", "").strip()
    update_time = request.form.get("updateTime", "").strip()
    if not icon:
        icon = str(entry.get("icon", "")).strip()
    if not apk:
        apk = str(entry.get("apk", "")).strip()
    if not version_name:
        version_name = str(entry.get("versionName", "")).strip()
    if not md5sum:
        md5sum = str(entry.get("md5sum", "")).strip()
    if not update_time:
        update_time = str(entry.get("updateTime", "")).strip() or manager.utc_now_iso()
    if not filesize_raw:
        filesize = int(entry.get("filesize", 0) or 0)
    else:
        try:
            filesize = int(filesize_raw)
        except ValueError:
            filesize = int(entry.get("filesize", 0) or 0)

    manager.ensure_category(data, category)

    updated_entry = dict(entry)
    updated_entry.pop("images", None)
    updated_entry.update(
        {
            "id": entry_id,
            "name": app_name,
            "package": package_name,
            "code": code,
            "description": description,
            "category": category,
            "incompatibility": incompatibility,
            "icon": icon,
            "apk": apk,
            "versionName": version_name,
            "md5sum": md5sum,
            "filesize": filesize,
            "updateTime": update_time,
        }
    )

    manager.upsert_entry(data, updated_entry)
    manager.save_data(data)
    flash(f"已更新应用: {app_name}", "success")
    return redirect(url_for("index"))


@app.post("/apps/delete")
def delete_app():
    entry_id = request.form.get("app_id", "").strip()
    if not entry_id:
        flash("缺少 app_id", "error")
        return redirect(url_for("index"))

    data = manager.load_data()
    manager.ensure_entry_ids(data)
    deleted_entry = find_entry_by_id(data.get("apps", []), entry_id)
    if not deleted_entry:
        flash(f"未找到应用: {entry_id}", "error")
        return redirect(url_for("index"))
    deleted = manager.delete_entry(data, entry_id)
    if not deleted:
        flash(f"未找到应用: {entry_id}", "error")
        return redirect(url_for("index"))
    manager.cleanup_entry_assets(data, deleted_entry)

    manager.save_data(data)
    flash(f"已删除应用: {entry_id}", "success")
    return redirect(url_for("index"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AppStore Creator")
    parser.add_argument(
        "--host",
        default=os.environ.get("APPSTORE_CREATOR_HOST", "0.0.0.0"),
        help="监听地址，默认 0.0.0.0",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("APPSTORE_CREATOR_PORT", "35889")),
        help="监听端口，默认 35889",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=True,
        help="是否开启 Flask debug（默认开启）",
    )
    parser.add_argument(
        "--no-debug",
        dest="debug",
        action="store_false",
        help="关闭 Flask debug",
    )
    args = parser.parse_args()

    manager.ensure_initialized()
    app.run(host=args.host, port=args.port, debug=args.debug)
