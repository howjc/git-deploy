# git-deploy v1.4.2

v1.4.2 收口 v1.4.1 深度审计发现的 Hybrid 运行期 TOCTOU 窗口，并把 `--recover` 从普通部署准备链路中彻底拆分。

## Hybrid 运行期新鲜度

- Root File 与 Mirror Directory 全部先上传到部署专属 Stage；所有上传重试和重连结束后、开始线上 Swap 前，再次核对 Remote Ownership Hash、Recovery Record 和全部当前/历史受管直接路径类型。
- 每条路径在移入 Backup 前再次核对 Remote Plan 冻结的 Expected Type。计划时 Missing 的路径不会把后来出现的同名内容当作旧版本备份。
- 计划时 Missing 的 Root File 与 Directory 通过 No-overwrite Rename 从 Stage 发布；最后一刻出现同名目标时以 Stale Plan 失败，并由 Recovery 保留外部内容。
- 写入新 Ownership 前再次核对已审阅的旧 Ownership Hash，防止 Swap 期间的外部 Manifest 修改被覆盖。
- 二次门禁失败时在线 Final Path 零修改。执行器尝试清理自己的 Stage/Backup/Recovery；内部清理失败会保留显式 Recovery，并提示通过 `--remote-plan` 与 `--recover` 处理。

## 独立 Recovery-only Prepare

- 新增独立的 `PreparedRecovery`、Recovery Plan 与 Workspace Recovery 流程。
- `--recover` 不运行 Build、不读取既有 State 内容、不扫描 Local Hybrid、不生成 Source/Output Plan、不冻结上传字节，也不做临时磁盘空间检查。
- Recovery 仍解析并冻结 Target/Command Contract、获取 Git Common Dir 下的 Target Lock，并在确认前后核对远端 Ownership 与 Recovery 事实。
- Workspace 只显示并准备实际存在 Recovery 的仓库，计划和确认统计只包含将执行的恢复、命令、State 与 Cleanup 动作。

## Legacy Recovery 与 UX

- schema-1 Pre-commit Restore 保持支持。
- schema-1 Ownership 已提交但命令待执行时，旧记录无法证明中断时的命令契约；`--recover` Fail Closed，Doctor 报告 `Legacy Command Contract Unknown`，且不会执行当前配置命令。
- 普通 Remote Plan 发现 Recovery 时隐藏当前部署的 Source/Output 操作和命令，只呈现 Recovery-only 计划。
- Cleanup 警告明确指向 `--remote-plan` 与 `--recover`；移除内部已无语义的 `allow_recovery` 参数。

## 兼容性与边界

- 配置、Local State 与 Ownership Schema 保持兼容，无需迁移 Ownership Manifest。
- 新写入的 Recovery 仍为 schema 2。升级时仅 schema-1 的已提交且命令待执行记录需要人工核对。
- 本版本增加的是多个写入边界的新鲜度检查，不是远端租约、全局事务或多发布器协调；仍不得让多个发布器并发修改同一 Hybrid 受管路径。
