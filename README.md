# netamade-releases
Server not available

## 导出正式版更新日志

运行以下命令，将 Git 历史及当前 `stable.md` 中的历代正式版更新日志增量导出为 Markdown 和长图：

```bash
python3 scripts/export_stable_updates.py
```

产物位于 `updates/md/` 和 `updates/img/`。文件名中的日期取自对应版本 `stable.json` 的 `buildTime`，缺失时回退到正式版提交日期。图片导出需要本机安装 `pandoc`、ImageMagick 和 Playwright CLI。
