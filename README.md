# netamade-releases

本仓库 main 是当前版本快照，仅保留一次无父提交。每次发布会替换 main 及保留版本标签，不能用此仓库追溯历史版本。

## 保留规则

- 应用 Release：最新稳定版、尝鲜版和内测版各一个，由 `stable.json`、`testing.json`、`beta.json` 确定。
- 工具 Release：保留 `speedtest` 和 `android-fio-arm64`，客户端仍依赖其下载地址。
- 已启用渠道和工具的保留标签均指向 main 的唯一提交；首个内测版发布后共五个 Release、五个标签。APK 版本、附件名称及下载地址不变。
- 删除旧应用 Release（含附件、发布说明）及其他标签。GitHub 缓存及第三方副本不属于仓库脚本能保证清除的范围。
- appstore 分支及其历史独立维护，发布脚本不修改。
- 首次内测发布前允许没有 `beta.json`，仍保留四个 Release/标签；已提交内测元数据或远程存在内测版本后，缺少 `beta.json` 必须停止清理。

## 渠道契约

| 渠道 | 构建 / 发布目标 | 元数据 / 更新日志 | 版本标识 |
| --- | --- | --- | --- |
| 稳定版 | `make release` / `make publish-release` | `stable.json` / `stable.md` | `5.2.2-REL` |
| 尝鲜版 | `make testing` / `make publish-testing` | `testing.json` / `testing.md` | `5.2.3-TEST-1023_2230` |
| 内测版 | `make beta` / `make publish-beta` | `beta.json` / `beta.md` | `5.2.3-BETA-1023_2230` |

新发布的 JSON 显式包含 `channel: stable/testing/beta`；旧 stable/testing JSON 缺少该字段时仍按 `testing` 布尔值识别。`testing` 兼容字段在稳定版为 false，尝鲜版和内测版为 true。渠道必须与文件名、版本标识、Release 标签及 APK 附件名一致。

标签格式为 `v<versionName>-<channel>`，附件名为 `netamade-<channel>-<versionName>-<versionCode>.apk`，下载地址为 `https://github.com/z0brk/netamade-releases/releases/download/<标签>/<附件名>`。内测 Release 标记为 GitHub prerelease，不设为 Latest。

客户端三渠道分别读取对应 JSON 和 Markdown；同 versionCode 跨渠道允许切换，同渠道按构建时间比较。专业用户资格仅限制客户端内测入口，公开仓库附件仍可直接下载。

首次启用时先更新发布与留存规则，再发布首个 beta，最后发布带独立内测渠道支持的尝鲜版或稳定版。旧客户端选择内测时仍会使用 testing，需先更新客户端。`beta.json` 只由真实 beta 构建发布生成，禁止复制 testing 元数据冒充内测版。

## 自动发布

在相邻 Netamade 源码仓库执行对应的 `make publish-release`、`make publish-testing` 或 `make publish-beta`。本地 APK 和 Mapping 分别保存在 `release/`、`testing/`、`beta/`。

发布流程：检查元数据 → 上传草稿 APK → 通过 GitHub API 核对附件状态、文件名、大小及可用的 SHA-256 → 公开新 Release → 原子替换 main 和保留标签 → 删除旧 Release 和标签 → 核验结果。
APK 不回下载校验，未更新渠道不做包内容校验。GitHub API 未提供摘要时只验证附件状态、文件名和大小，不声称远程内容摘要已通过；同名公开包重试必须有匹配的 SHA-256，否则要求使用新版本标识。

失败返回非零状态，可在源码仓库直接重跑 `python3 scripts/publish_release.py release|testing|beta`（选择其中一个参数），复用原构建。已公开的同名 APK 不覆盖，内容变化必须使用新版本标识；同一分钟重复构建导致标识冲突时，可在下一分钟重建或显式指定对应的 `TESTING_VERSION_NAME` / `BETA_VERSION_NAME`。元数据切换前会短暂存在新旧两个 Release；任何清理错误都必须处理，不能报告发布成功。

手动检查保留计划（只读）：

```bash
python3 scripts/retain_current_releases.py
```

重新收敛当前状态（会修改远程和删除旧版本）：

```bash
python3 scripts/retain_current_releases.py --apply
```

修改发布规则或保留脚本后，执行 `python3 scripts/retain_current_releases.py --apply --update-policy`，仅将 README、AGENTS 和保留脚本纳入新的唯一快照，不发布 APK。

`--update-policy` 会读取这些文件的完整工作区内容；同文件混有其他未提交修改时，须先区分并准备本次要发布的版本，不得顺带发布无关改动。

首次压缩已有 main 历史时必须额外指定仓库外的 `--backup-dir /绝对路径/新目录`。备份包含完整 Git 镜像、Release 元数据（含说明及附件信息）和工作区差异，不下载全部旧 APK。备份不上传到本仓库。

脚本通过独占锁避免同一发布仓库并发操作，通过带精确租约的原子推送避免覆盖远程并发修改。其他克隆不能继续普通 pull 合并旧 main；应保留本地工作后重新克隆或明确同步新快照，禁止把旧历史合并回来。

## 导出当前正式版更新日志

```bash
python3 scripts/export_stable_updates.py
```

仅导出当前 `stable.md`，不从 Git 恢复历史日志。产物在已忽略的 `updates/md/` 和 `updates/img/`，不公开发布；既有本地文件不自动删除。图片导出需要 `pandoc`、ImageMagick 和 Playwright CLI。

本地 APK、Mapping、`正式包/` 和未提交工作不属于远程历史清理范围。相邻 Netamade 的 `publish_release.py` 在远程发布成功后，调用 `scripts/prune-local-releases.py` 清理本地产物：`release/`、`testing/`、`beta/` 各按 APK 修改时间保留最新 3 组 APK/Mapping；`正式包/` 保留最新 3 个 APK 及对应更新日志图片，安装工具和教程不删除。保留下来的 Mapping 仍仅存于本地，不公开发布。
