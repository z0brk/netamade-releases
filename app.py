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
    WorkflowManager,
    extract_icon_from_apk,
    find_entry_by_id,
    normalize_category,
    normalize_incompatibility,
    parse_apk_metadata,
    parse_workflow_bundle,
)

BASE_DIR = Path(__file__).resolve().parent
REPO_ROOT = BASE_DIR
APPSTORE_JSON_PATH = REPO_ROOT / "appstore.json"
APKS_DIR = REPO_ROOT / "apks"
WORKFLOWS_JSON_PATH = REPO_ROOT / "workflows.json"
WORKFLOWS_DIR = REPO_ROOT / "workflows"


def is_supported_apk_filename(filename: str) -> bool:
    lower = (filename or "").strip().lower()
    return lower.endswith(".apk") or lower.endswith(".apk.1")


def is_supported_workflow_filename(filename: str) -> bool:
    return (filename or "").strip().lower().endswith(".json")


def sanitize_storage_filename(filename: str, fallback: str) -> str:
    normalized = (filename or "").replace("\\", "/").split("/")[-1].strip().replace("\x00", "")
    return normalized or fallback

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("APPSTORE_CREATOR_SECRET", "appstore-creator-dev")
# 限制单次上传大小，避免误上传超大文件占满磁盘
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024 * 1024  # 1 GiB

manager = AppStoreManager(json_path=APPSTORE_JSON_PATH, apks_dir=APKS_DIR)
workflow_manager = WorkflowManager(json_path=WORKFLOWS_JSON_PATH, workflows_dir=WORKFLOWS_DIR)


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


@app.get("/workflow-files/<path:workflow_path>")
def download_workflow_file(workflow_path: str):
    safe_path = Path(workflow_path)
    if safe_path.is_absolute() or ".." in safe_path.parts:
        abort(400)
    target = WORKFLOWS_DIR / safe_path
    if not target.is_file():
        abort(404)
    return send_from_directory(str(WORKFLOWS_DIR), workflow_path, as_attachment=True, download_name=target.name)


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

    workflow_data = workflow_manager.load_data()
    workflow_changed = False
    if workflow_manager.ensure_entry_ids(workflow_data):
        workflow_changed = True
    for workflow_entry in workflow_data.get("workflows", []):
        if workflow_manager.backfill_entry_metadata(workflow_entry):
            workflow_changed = True
        workflow_category = normalize_category(str(workflow_entry.get("category", "")).strip()) or "未分类"
        if workflow_entry.get("category") != workflow_category:
            workflow_entry["category"] = workflow_category
            workflow_changed = True
        before_count = len(workflow_data.get("categories", []))
        workflow_manager.ensure_category(workflow_data, workflow_category)
        if len(workflow_data.get("categories", [])) != before_count:
            workflow_changed = True
    if workflow_changed:
        workflow_manager.save_data(workflow_data)

    auto_edit_id = request.args.get("edit", "").strip()
    auto_edit_app = find_entry_by_id(data.get("apps", []), auto_edit_id) if auto_edit_id else None
    auto_edit_workflow_id = request.args.get("workflow_edit", "").strip()
    auto_edit_workflow = (
        find_entry_by_id(workflow_data.get("workflows", []), auto_edit_workflow_id)
        if auto_edit_workflow_id
        else None
    )
    active_tab = request.args.get("tab", "notice").strip().lower()
    if active_tab not in {"notice", "apps", "workflows", "categories"}:
        active_tab = "notice"
    return render_template(
        "index.html",
        apps=data.get("apps", []),
        workflows=workflow_data.get("workflows", []),
        workflow_categories=workflow_data.get("categories", []),
        notice=data.get("notice", ""),
        categories=data.get("categories", []),
        incompatibility_options=INCOMPATIBILITY_OPTIONS,
        preview_asset_url=preview_asset_url,
        auto_edit_id=auto_edit_id,
        auto_edit_app=auto_edit_app,
        auto_edit_workflow_id=auto_edit_workflow_id,
        auto_edit_workflow=auto_edit_workflow,
        active_tab=active_tab,
    )


@app.post("/upload")
def upload_apk():
    apk_file = request.files.get("apk")
    if apk_file is None or not apk_file.filename:
        flash("请选择 APK 文件", "error")
        return redirect(url_for("index"))

    raw_filename = (apk_file.filename or "").strip()
    if not is_supported_apk_filename(raw_filename):
        flash("仅支持上传 .apk 或 .apk.1 文件", "error")
        return redirect(url_for("index"))
    apk_filename = secure_filename(raw_filename)
    if not apk_filename:
        apk_filename = "upload.apk.1" if raw_filename.lower().endswith(".apk.1") else "upload.apk"

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


@app.post("/workflows/upload")
def upload_workflow():
    workflow_file = request.files.get("workflow")
    if workflow_file is None or not workflow_file.filename:
        flash("请选择工作流 JSON 文件", "error")
        return redirect(url_for("index", tab="workflows"))

    raw_filename = (workflow_file.filename or "").strip()
    if not is_supported_workflow_filename(raw_filename):
        flash("仅支持上传 .json 工作流文件", "error")
        return redirect(url_for("index", tab="workflows"))

    workflow_filename = sanitize_storage_filename(raw_filename, "workflow.json")
    tmp_dir = Path(tempfile.mkdtemp(prefix="appstore-workflow-"))
    tmp_workflow_path = tmp_dir / workflow_filename

    try:
        workflow_file.save(tmp_workflow_path)
        workflow_metadata = parse_workflow_bundle(tmp_workflow_path)
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(tmp_dir, ignore_errors=True)
        flash(f"工作流解析失败: {exc}", "error")
        return redirect(url_for("index", tab="workflows"))

    data = workflow_manager.load_data()
    workflow_manager.ensure_entry_ids(data)
    file_md5 = manager.compute_md5(tmp_workflow_path)
    display_name = request.form.get("name", "").strip() or Path(workflow_filename).stem
    generated_code = workflow_manager.resolve_code(display_name, file_md5)
    category = normalize_category(request.form.get("category", "")) or "未分类"
    workflow_manager.ensure_category(data, category)

    workflow_dir = WORKFLOWS_DIR / generated_code
    workflow_dir.mkdir(parents=True, exist_ok=True)
    workflow_final_path = workflow_dir / workflow_filename
    shutil.copyfile(tmp_workflow_path, workflow_final_path)

    entry_id = manager.generate_entry_id({str(item.get("id", "")).strip() for item in data.get("workflows", [])})
    entry = {
        "id": entry_id,
        "code": generated_code,
        "name": display_name,
        "category": category,
        "author": request.form.get("author", "").strip(),
        "description": request.form.get("description", "").strip(),
        "filename": workflow_filename,
        "file": f"/{generated_code}/{workflow_filename}",
        "md5sum": file_md5,
        "filesize": workflow_final_path.stat().st_size,
        "updateTime": manager.utc_now_iso(),
        "workflowCount": workflow_metadata.workflow_count,
        "workflowNames": workflow_metadata.workflow_names,
        "tags": workflow_metadata.tags,
    }

    workflow_manager.upsert_entry(data, entry)
    workflow_manager.save_data(data)

    shutil.rmtree(tmp_dir, ignore_errors=True)
    flash(
        f"已上传工作流包 {display_name} | 含 {workflow_metadata.workflow_count} 个工作流 | md5={entry['md5sum']}",
        "success",
    )
    return redirect(url_for("index", tab="workflows", workflow_edit=entry_id))


@app.post("/categories")
def save_categories():
    categories = [item.strip() for item in request.form.getlist("categories") if item.strip()]
    data = manager.load_data()
    manager.replace_categories(data, categories)
    manager.save_data(data)
    flash("分类列表已更新", "success")
    return redirect(url_for("index"))


@app.post("/workflows/categories")
def save_workflow_categories():
    categories = [item.strip() for item in request.form.getlist("categories") if item.strip()]
    data = workflow_manager.load_data()
    workflow_manager.replace_categories(data, categories)
    workflow_manager.save_data(data)
    flash("工作流分类列表已更新", "success")
    return redirect(url_for("index", tab="categories"))


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
    return redirect(url_for("index", tab="apps"))


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
    return redirect(url_for("index", tab="apps"))


@app.post("/workflows/update")
def update_workflow():
    entry_id = request.form.get("workflow_id", "").strip()
    if not entry_id:
        flash("缺少 workflow_id", "error")
        return redirect(url_for("index", tab="workflows"))

    data = workflow_manager.load_data()
    workflow_manager.ensure_entry_ids(data)
    entry = find_entry_by_id(data.get("workflows", []), entry_id)
    if not entry:
        flash(f"未找到工作流: {entry_id}", "error")
        return redirect(url_for("index", tab="workflows"))

    updated_entry = dict(entry)
    updated_entry.update(
        {
            "id": entry_id,
            "name": request.form.get("name", "").strip() or str(entry.get("name", "")).strip() or "未命名工作流",
            "category": normalize_category(request.form.get("category", "")) or "未分类",
            "author": request.form.get("author", "").strip(),
            "description": request.form.get("description", "").strip(),
        }
    )
    workflow_manager.ensure_category(data, str(updated_entry.get("category", "")).strip())

    workflow_manager.upsert_entry(data, updated_entry)
    workflow_manager.save_data(data)
    flash(f"已更新工作流: {updated_entry['name']}", "success")
    return redirect(url_for("index", tab="workflows"))


@app.post("/workflows/delete")
def delete_workflow():
    entry_id = request.form.get("workflow_id", "").strip()
    if not entry_id:
        flash("缺少 workflow_id", "error")
        return redirect(url_for("index", tab="workflows"))

    data = workflow_manager.load_data()
    workflow_manager.ensure_entry_ids(data)
    deleted_entry = find_entry_by_id(data.get("workflows", []), entry_id)
    if not deleted_entry:
        flash(f"未找到工作流: {entry_id}", "error")
        return redirect(url_for("index", tab="workflows"))
    deleted = workflow_manager.delete_entry(data, entry_id)
    if not deleted:
        flash(f"未找到工作流: {entry_id}", "error")
        return redirect(url_for("index", tab="workflows"))
    workflow_manager.cleanup_entry_assets(data, deleted_entry)

    workflow_manager.save_data(data)
    flash(f"已删除工作流: {entry_id}", "success")
    return redirect(url_for("index", tab="workflows"))


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
    workflow_manager.ensure_initialized()
    app.run(host=args.host, port=args.port, debug=args.debug)
