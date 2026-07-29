# git-deploy v1.4.1

v1.4.1 是 Hybrid Output 的安全收口版本，修复 v1.4.0 深度审计发现的确认窗口、恢复阶段和本地边界问题。

## Stale Plan 零写入门禁

- Remote Plan 冻结 Ownership 原始字节 Hash，以及当前与历史全部受管直接路径的类型。
- 用户确认后、任何上传或 Hybrid 内部目录创建前重新读取这些事实；Missing/File/Directory 或 Ownership 任一漂移都会抛出 Stale Plan。
- Workspace 在第一仓写入前复核全部选中仓库，避免后一仓已 Stale 时前一仓先产生部分写入。

## 显式、分阶段 Recovery

- 普通部署、`--remote-plan` 和 Doctor 只读报告待恢复记录，不再自动修改远端。
- 新增互斥的 `--recover`：显示 Restore、Resume Commands 或 Cleanup 动作，确认后只执行恢复并退出；后续普通部署必须重新读取事实与确认。
- Recovery 持久化区分 Ownership、Commands、State 和 Cleanup 阶段。Delete-only、Ownership-only 与命令失败不再丢失待执行命令；State/Cleanup 失败不会重复已完成命令。
- Recovery 绑定中断部署时的 `after_deploy` 命令与超时指纹；配置漂移时拒绝执行，避免恢复阶段静默改跑另一组命令。
- 旧路径应存在但 Backup 缺失时 Fail Closed，保留 Recovery、Stage 和 Backup 现场，并由 Doctor 报告人工检查。

## 本地与路径边界

- Hybrid Local Root 等于项目根目录（含 resolve 后的符号链接别名）会在构建前拒绝。
- `.git/**`、`.deploy/**` 加入强制 Protect；Hybrid 直接 `.git`、`.deploy`、`.git-deploy` 双重拒绝。
- Local Manifest、Remote Ownership、Paramiko 和 Native OpenSSH 统一拒绝首尾空格、Tab、控制字符及不可见空白组件。
- Mirror Manifest 记录全部嵌套目录，Stage 会保留空目录；参考聚合脚本使用相同的保护名称规则。

## 兼容性与运维提示

- 配置和 Ownership Schema 保持兼容；版本升级后无需迁移 Ownership Manifest。新写入的 Recovery Record 使用带逐路径进度的 schema 2。
- Hybrid Root File 仍用本地成功 State Hash 判断增量，不读取远端内容 Hash；外部修改受管 Root File 后应使用 `--full`。
- v1.4.0 schema-1 Recovery 可由 v1.4.1 保守读取；因旧记录缺少逐路径进度，事实无法证明时会要求人工检查。恢复时输出 Hash 不可得，因此保存空 Output Manifest，下一次普通部署会保守重传当前 Outputs。
