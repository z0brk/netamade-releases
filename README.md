# netamade-releases

本仓库 main 是当前版本快照，仅保留一次无父提交。每次发布会替换 main 及保留版本标签，不能用此仓库追溯历史版本。

## 保留规则

- 应用 Release：最新稳定版和最新测试版各一个，由 `stable.json`、`testing.json` 确定。
- 工具 Release：保留 `speedtest` 和 `android-fio-arm64`，客户端仍依赖其下载地址。
- 四个保留标签均指向 main 的唯一提交，APK 版本、附件名称及下载地址不变。
- 删除旧应用 Release（含附件、发布说明）及其他标签。GitHub 缓存及第三方副本不属于仓库脚本能保证清除的范围。
- appstore 分支及其历史独立维护，发布脚本不修改。
- 当前只支持 stable/testing；未来内测渠道需先明确客户端更新契约再实现。

## 自动发布

在相邻 Netamade 源码仓库执行 `make publish-release` 或 `make publish-testing`。

发布流程：检查元数据 → 上传草稿 APK → 通过 GitHub API 核对附件状态、文件名、大小及可用的 SHA-256 → 公开新 Release → 原子替换 main 和保留标签 → 删除旧 Release 和标签 → 核验结果。
APK 不回下载校验，未更新渠道不做包内容校验。GitHub API 未提供摘要时只验证附件状态、文件名和大小，不声称远程内容摘要已通过；同名公开包重试必须有匹配的 SHA-256，否则要求使用新版本标识。

失败返回非零状态，可在源码仓库直接重跑 `./scripts/publish-release.sh release` 或 `./scripts/publish-release.sh testing`，复用原构建。已公开的同名 APK 不覆盖，内容变化必须使用新版本标识。元数据切换前会短暂存在新旧两个 Release；任何清理错误都必须处理，不能报告发布成功。

手动检查保留计划（只读）：

```bash
python3 scripts/retain_current_releases.py
```

重新收敛当前状态（会修改远程和删除旧版本）：

```bash
python3 scripts/retain_current_releases.py --apply
```

修改发布规则或保留脚本后，执行 `python3 scripts/retain_current_releases.py --apply --update-policy`，仅将 README、AGENTS 和保留脚本纳入新的唯一快照，不发布 APK。

首次压缩已有 main 历史时必须额外指定仓库外的 `--backup-dir /绝对路径/新目录`。备份包含完整 Git 镜像、Release 元数据（含说明及附件信息）和工作区差异，不下载全部旧 APK。备份不上传到本仓库。

脚本通过独占锁避免同一发布仓库并发操作，通过带精确租约的原子推送避免覆盖远程并发修改。其他克隆不能继续普通 pull 合并旧 main；应保留本地工作后重新克隆或明确同步新快照，禁止把旧历史合并回来。

## 导出当前正式版更新日志

```bash
python3 scripts/export_stable_updates.py
```

仅导出当前 `stable.md`，不从 Git 恢复历史日志。产物在已忽略的 `updates/md/` 和 `updates/img/`，不公开发布；既有本地文件不自动删除。图片导出需要 `pandoc`、ImageMagick 和 Playwright CLI。

本地 APK、Mapping、`正式包/` 和未提交工作不属于远程历史清理范围，脚本不批量删除。Mapping 应保留在非公开位置供历史版本崩溃分析使用。
