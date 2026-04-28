# AppStore Creator

用于维护仓库根目录的 `appstore.json`，并管理 `apks/` 文件。

## 功能

- 上传 APK（支持 `.apk` / `.apk.1`）并自动解析 `package`、`versionName`、应用名
- 自动生成应用代号 `code`
- 每个应用可指定 `category` 分类
- 根级维护 `categories` 分类列表（按顺序保存）
- 上传时始终自动从 APK 提取 icon 并写入条目
- 维护 `incompatibility` 字段（`ALL/EP32/EP36/EP40/EP41`）
- 支持 `notice` 公告编辑
- 应用列表支持查看、编辑、删除（点击卡片弹窗编辑）
- 上传成功后自动弹出该应用编辑对话框
- 支持多个 `package` 相同的应用同时存在
- 上传区仅保留“上传 APK”按钮，icon 为唯一图标来源

## 启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python app.py
# 临时指定其他端口
python app.py --port 5000
```

打开 `http://127.0.0.1:<端口>`（默认 `35889`）。
