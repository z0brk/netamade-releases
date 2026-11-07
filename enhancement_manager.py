from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


class EnhancementManager:
    """只分发作者已签名的 AEP2，不持有平台私钥，不代打包 APK。"""

    ENTRY_FIELDS = ("name", "package", "author", "authorKeyFingerprint", "enhancementId")
    SIGNED_FIELDS = (
        "name", "package", "author", "enhancementId", "releaseId", "updateTime",
        "description", "compatibility", "apkSha256", "apkSize", "activationMode",
        "recipientKeyId", "hostMinVersionCode",
    )

    def __init__(self, json_path: Path, assets_dir: Path):
        self.json_path = json_path
        self.assets_dir = assets_dir

    def load_data(self) -> dict[str, Any]:
        if not self.json_path.exists():
            return {"schemaVersion": 2, "aeps": [], "enhancements": [], "history": []}
        data = json.loads(self.json_path.read_text(encoding="utf-8"))
        self._require_schema(data)
        self._require_index_shape(data)
        return data

    @staticmethod
    def _require_schema(data: dict[str, Any]) -> None:
        if not isinstance(data, dict) or type(data.get("schemaVersion")) is not int or data["schemaVersion"] != 2:
            raise ValueError("增强中心仅支持 schemaVersion=2；旧清单须显式迁移归档")
        if (not isinstance(data.get("aeps"), list) or not isinstance(data.get("enhancements"), list)
                or not isinstance(data.get("history", []), list)):
            raise ValueError("增强中心 aeps/enhancements/history 必须为数组")

    @classmethod
    def _require_index_shape(cls, data: dict[str, Any]) -> None:
        for entry in data["aeps"]:
            if (not isinstance(entry, dict) or not isinstance(entry.get("id"), str)
                    or not isinstance(entry.get("versions"), list) or not entry["versions"]
                    or any(field not in entry for field in cls.ENTRY_FIELDS)
                    or not isinstance(entry["author"], dict) or entry.get("simulation")):
                raise ValueError("增强中心条目结构无效，不接受旧 APK 或模拟条目")
            for version in entry["versions"]:
                if (not isinstance(version, dict) or type(version.get("formatVersion")) is not int
                        or version["formatVersion"] != 2
                        or any(field in version for field in ("apk", "sha256", "filesize"))
                        or any(field not in version for field in (*cls.SIGNED_FIELDS, "id", "authorKeyFingerprint",
                                                                  "aep", "aepSha256", "aepSize"))):
                    raise ValueError("中心仅允许完整 formatVersion=2 索引，不允许 APK/v1 或含混摘要字段")

    def save_data(self, data: dict[str, Any]) -> None:
        # 写入前重新核验实际资源，不能通过手工改 JSON 绕过签名元数据约束。
        self.validate_data(data)
        self.json_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.json_path.parent,
                                         prefix="enhancements-", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        try:
            os.replace(temporary, self.json_path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def normalize_update_time(value: str) -> str:
        try:
            moment = datetime.fromisoformat(value.strip())
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=ZoneInfo("Asia/Shanghai"))
            return moment.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError as error:
            raise ValueError("更新时间格式无效") from error

    @staticmethod
    def inspect_aep(source: Path) -> tuple[dict[str, Any], str]:
        if source.suffix.lower() != ".aep":
            raise ValueError("应用增强只接收 AEP2，不接收 APK 或 AEP v1")
        # 仅使用仓库内固定的公开协议实现，绝不执行上传者附带的验证脚本。
        from aep_protocol import Crypto, parse_v2
        with tempfile.TemporaryDirectory(prefix="appstore-aep-verify-") as directory:
            package = parse_v2(source, Crypto(directory))
        return package["metadata"], package["author_fingerprint"]

    def _version_from_aep(self, source: Path) -> tuple[str, dict[str, Any]]:
        header, fingerprint = self.inspect_aep(source)
        update_time = header["updateTime"]
        if (not re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", update_time)
                or self.normalize_update_time(update_time) != update_time):
            raise ValueError("AEP2 updateTime 必须为北京时间 YYYY-MM-DD HH:mm:ss，不含时区")
        entry_id = fingerprint + ":" + header["enhancementId"]
        # 公钥指纹和稳定 ID 只作为逻辑身份；目录使用摘要，避免作者控制文件路径。
        directory = hashlib.sha256(entry_id.encode("utf-8")).hexdigest()
        digest = self.sha256(source)
        version = {field: copy.deepcopy(header[field]) for field in self.SIGNED_FIELDS}
        version.update({
            "id": header["releaseId"], "formatVersion": 2,
            "authorKeyFingerprint": fingerprint,
            "aep": f"/enhancements/{directory}/{digest}.aep",
            "aepSha256": digest, "aepSize": source.stat().st_size,
        })
        return entry_id, version

    def _asset_path(self, path: str) -> Path:
        prefix = "/enhancements/"
        if not isinstance(path, str) or not path.startswith(prefix):
            raise ValueError("AEP 资源路径必须位于 /enhancements/")
        relative = Path(path[len(prefix):])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts:
            raise ValueError("AEP 资源路径无效")
        target = self.assets_dir / relative
        if not target.resolve().is_relative_to(self.assets_dir.resolve()):
            raise ValueError("AEP 资源不得指向目录外")
        return target

    def _store_asset(self, source: Path, version: dict[str, Any]) -> None:
        destination = self._asset_path(version["aep"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if self.sha256(destination) != version["aepSha256"]:
                raise ValueError("已存在同名 AEP 资源但摘要不同，拒绝覆盖")
            return
        with tempfile.NamedTemporaryFile(dir=destination.parent, suffix=".tmp", delete=False) as stream:
            staging = Path(stream.name)
        try:
            shutil.copyfile(source, staging)
            if staging.stat().st_size != version["aepSize"] or self.sha256(staging) != version["aepSha256"]:
                raise ValueError("AEP 源文件在上传期间发生变化")
            os.replace(staging, destination)
        finally:
            staging.unlink(missing_ok=True)

    @classmethod
    def _refresh_entry(cls, entry: dict[str, Any]) -> None:
        # 每个版本保留其自身签名信息；聚合卡片展示最后上传版本的作者资料。
        entry.update({field: copy.deepcopy(entry["versions"][-1][field]) for field in cls.ENTRY_FIELDS})

    def add_upload(self, data: dict[str, Any], source: Path, entry_id: str | None = None) -> dict[str, Any]:
        self._require_schema(data)
        signed_id, version = self._version_from_aep(source)
        if entry_id is not None and signed_id != entry_id:
            raise ValueError("AEP 作者公钥或增强 ID 与所选条目不一致")
        existing = self.find(data, signed_id)
        target = next((v for v in (existing or {}).get("versions", []) if v["id"] == version["id"]), None)
        if target:
            if target != version:
                raise ValueError("同一 releaseId 已有不同发行包；请由作者使用新的 releaseId 重新打包")
            return target
        self._store_asset(source, version)
        if existing is None:
            existing = {"id": signed_id, "versions": []}
            data["aeps"].append(existing)
        existing["versions"].append(version)
        self._refresh_entry(existing)
        return version

    def replace_version_aep(self, data: dict[str, Any], entry_id: str, version_id: str, source: Path) -> None:
        entry = self.find(data, entry_id)
        target = next((v for v in (entry or {}).get("versions", []) if v["id"] == version_id), None)
        if not entry or not target:
            raise ValueError("未找到增强版本")
        signed_id, version = self._version_from_aep(source)
        if signed_id != entry_id:
            raise ValueError("AEP 作者公钥或增强 ID 与所选条目不一致")
        if any(v["id"] == version["id"] for v in entry["versions"] if v is not target):
            raise ValueError("新 releaseId 已存在，请使用新增版本或选择正确版本")
        if target == version:
            return
        if version["id"] == version_id:
            raise ValueError("修改发行包必须使用新的 releaseId")
        self._store_asset(source, version)
        self._archive_version(data, entry, target)
        entry["versions"].remove(target)
        entry["versions"].append(version)
        self._refresh_entry(entry)

    @staticmethod
    def _archive_version(data: dict[str, Any], entry: dict[str, Any], version: dict[str, Any]) -> None:
        record = {
            "entryId": entry["id"], "name": version.get("name", entry["name"]),
            "package": version.get("package", entry["package"]), "releaseId": version["id"],
            "format": "aep2", "path": version["aep"], "sha256": version["aepSha256"],
            "size": version["aepSize"], "updateTime": version["updateTime"],
        }
        if record not in data.setdefault("history", []):
            data["history"].append(record)

    @staticmethod
    def find(data: dict[str, Any], entry_id: str) -> dict[str, Any] | None:
        return next((item for item in data.get("aeps", []) if item.get("id") == entry_id), None)

    def delete_version(self, data: dict[str, Any], entry_id: str, version_id: str) -> bool:
        entry = self.find(data, entry_id)
        target = next((v for v in (entry or {}).get("versions", []) if v["id"] == version_id), None)
        if not entry or not target:
            return False
        self._archive_version(data, entry, target)
        entry["versions"].remove(target)
        if entry["versions"]:
            self._refresh_entry(entry)
        else:
            data["aeps"].remove(entry)
        # 管理器移除索引时保留资源；不再分发的文件由用户另行手动清理。
        return True

    def delete(self, data: dict[str, Any], entry_id: str) -> bool:
        entry = self.find(data, entry_id)
        if not entry:
            return False
        for version in entry["versions"]:
            self._archive_version(data, entry, version)
        data["aeps"].remove(entry)
        return True

    def validate_data(self, data: dict[str, Any]) -> None:
        self._require_schema(data)
        self._require_index_shape(data)
        seen: set[str] = set()
        for entry in data["aeps"]:
            if not isinstance(entry, dict) or not entry.get("versions"):
                raise ValueError("中心条目必须包含 AEP2 版本")
            entry_id = entry.get("id")
            if not isinstance(entry_id, str) or entry_id in seen:
                raise ValueError("中心条目 ID 缺失或重复")
            seen.add(entry_id)
            releases: set[str] = set()
            for version in entry["versions"]:
                source = self._asset_path(version.get("aep", ""))
                signed_id, expected = self._version_from_aep(source)
                if signed_id != entry_id or expected != version:
                    raise ValueError("中心版本索引与已签 AEP2 不一致")
                if version["id"] in releases:
                    raise ValueError("同一中心条目的 releaseId 重复")
                releases.add(version["id"])
            latest = entry["versions"][-1]
            if any(entry.get(field) != latest[field] for field in self.ENTRY_FIELDS):
                raise ValueError("中心条目显示信息与最后上传版本不一致")

    @staticmethod
    def sha256(source: Path) -> str:
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()


def main() -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="AEP2 增强中心本地管理；不会推送或上传远端")
    parser.add_argument("--manifest", type=Path, default=root / "enhancements.json")
    parser.add_argument("--assets-dir", type=Path, default=root / "enhancements")
    commands = parser.add_subparsers(dest="command", required=True)
    upload = commands.add_parser("upload", help="从已签 AEP2 生成索引并复制到本地资源目录")
    upload.add_argument("aep", type=Path)
    upload.add_argument("--entry-id", help="新增版本时核对所选作者/增强身份")
    replace = commands.add_parser("replace", help="用新的已签发行包替换目录版本，保留旧文件")
    replace.add_argument("entry_id")
    replace.add_argument("release_id")
    replace.add_argument("aep", type=Path)
    inspect = commands.add_parser("inspect", help="只读核验单个 AEP2 并显示索引，不复制或保存")
    inspect.add_argument("aep", type=Path)
    commands.add_parser("validate", help="核对 v2 清单、结构、作者签名及资源摘要，不解密 APK")
    args = parser.parse_args()
    manager = EnhancementManager(args.manifest, args.assets_dir)
    try:
        if args.command == "inspect":
            _, version = manager._version_from_aep(args.aep)
            print(json.dumps(version, ensure_ascii=False, indent=2))
            return 0
        data = manager.load_data()
        if args.command == "upload":
            version = manager.add_upload(data, args.aep, args.entry_id)
            manager.save_data(data)
            print(json.dumps(version, ensure_ascii=False, indent=2))
        elif args.command == "replace":
            manager.replace_version_aep(data, args.entry_id, args.release_id, args.aep)
            manager.save_data(data)
        else:
            manager.validate_data(data)
            print(json.dumps({"schemaVersion": 2, "entries": len(data["aeps"]),
                              "verifiedAep2Versions": sum(len(entry["versions"]) for entry in data["aeps"]),
                              "payloadVerified": False}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as error:
        parser.exit(1, f"增强中心操作失败：{error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
