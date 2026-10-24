# 发布仓库约束

- main 仅保留一个无父提交，禁止普通追加提交、合并旧历史或推送历史备份引用。
- 稳定版和测试版各保留一个公开 Release，以 stable.json/testing.json 为准；上传新 APK 后通过 GitHub API 核验附件状态、文件名、大小及可用的 SHA-256，成功后才删除旧版本；禁止回下载 APK，未更新渠道不做包内容校验。
- 仅保留两个应用版本标签和 speedtest、android-fio-arm64 两个工具标签；全部指向唯一快照。工具 Release 的附件、下载地址和说明不变。
- 不修改 appstore 分支、其内容和历史。不得因清理 main 删除其他分支。
- 唯一自动发布入口是相邻 Netamade/scripts/publish-release.sh，由其调用 scripts/retain_current_releases.py。清理脚本默认只读，--apply 执行；首次历史重写必须有仓库外恢复备份。
- 发布失败必须返回非零状态，清理失败不等于发布完成；重试不得覆盖已公开的同名不同内容 APK。
- 不发布历史 APK 和历史更新日志；只维护当前 stable.md/testing.md。历史导出、Mapping 和恢复备份仅在仓库外或被忽略的本地目录保留。
- 禁止将无关工作区修改带入发布快照，禁止 reset --hard、git clean 或批量删除本地未跟踪文件。暂存区非空时停止发布，不覆盖用户暂存工作。
- 暂不实现内测渠道；新增渠道前明确元数据、下载地址和客户端选择规则，再同步保留策略。
- 验证应包含语法检查、只读计划及操作后的远程 Release/标签/main 提交数检查。不得将删除 Git 引用描述为 GitHub 缓存和第三方副本全部清除。
