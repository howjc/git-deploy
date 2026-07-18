# ADR：单 Hybrid Output 与远端所有权

状态：Accepted；v1.4.2 运行期新鲜度与独立恢复修订

日期：2026-07-17

## 问题

前端构建产物通常不进入 Git，并会直接铺到同时包含 `index.php`、`.env`、后端目录和运行时数据的远端项目根目录。本地 State 丢失或换机器后，单靠本地 output manifest 无法安全判断哪些旧前端文件可以删除；清空根目录、普通 Root Mirror 或递归扫描删除都会越过所有权边界。

## 决策

v1.4.0 只增加一个受控模型：项目 Build 先把明确来源聚合到一个 Local Aggregation Root，再由一个 SFTP `mode = "hybrid"` Output 映射到 `remote = "."`。

- 聚合根的直接文件是 Root File：按 Hash 增量上传，删除只来自 Remote Ownership Manifest。
- 聚合根的直接目录是 Mirror Directory：每次部署完整 Stage/Swap，目录内容等于本地视图。
- Remote Ownership Manifest 只记录该 Hybrid 明确拥有的直接文件和目录；未记录的远端内容永不扫描、接管或删除。
- 无 Manifest 时，已存在的同名路径必须通过显式 `--full` Adoption；只接管当前本地存在的同名路径。
- Local State 记录最后完整成功的部署和 Root File Hash；Remote Ownership 记录可删除的远端直接子项。两者职责不可互换。
- 同一配置最多一个 Hybrid；Workspace 继续拒绝同一物理 Endpoint 上相同或嵌套的 Remote Root。
- Hybrid 仅支持 SFTP。FTP 缺少本版本所需的可靠递归 Stage/Swap 与路径类型契约。

## 为何不做 Root Mirror / Full Root Reconcile

远端根目录包含不属于部署器的后端、环境和运行时内容。对根目录做 Mirror 或递归 Reconcile 需要推断未知路径的所有权，无法满足“未知内容永不删除”的安全不变量。因此 v1.4.0 只 Mirror 聚合根中明确出现的直接目录，并只根据远端 Manifest 删除历史拥有项。

## Recovery 与原子性边界

Root File 与 Mirror Directory 都先完整上传到 `.git-deploy/stage/<id>`，现有路径移动到 `.git-deploy/backup/<id>` 后再发布。计划时 Missing 的路径使用不覆盖目标的 Rename，目标最后一刻出现时发布失败，不会把外部路径移入 Backup。`.git-deploy/recovery/<id>.json` 记录 Deployment ID、Mapping、Target Fingerprint、Stage/Backup、阶段和新旧 Ownership Hash。

中断记录的发现与执行严格分离。普通部署、`--remote-plan` 和 Doctor 都只读报告；只有用户确认后的 `--recover` 才执行恢复，并在完成后退出，要求下一次普通部署重新读取事实和确认计划。Recovery-only Prepare 不运行 Build、不读取既有 State 内容、不扫描或冻结当前 Local Hybrid，也不生成当前 Source/Output 操作；它只冻结 Target/Command Contract，获取本地 Target Lock，并读取完成恢复所需的远端事实。Workspace 只保留实际存在 Recovery 的项目。

中断发生在 Ownership Commit 前时恢复 Backup；Commit 后按持久阶段继续 `after_deploy`、Local State 和清理。命令采用至少一次语义；已记录完成的命令不会因 State/Cleanup 失败而重复。必要 Backup 缺失或无法证明新旧 Ownership Hash 时 Fail Closed 并保留 Recovery、Stage 和 Backup 现场。schema-1 记录的 Pre-commit Restore 继续支持；若其 Ownership 已提交且命令待执行，因为旧格式没有命令契约指纹，必须 Fail Closed 并人工处理，不能执行当前配置命令。它只恢复当前 Hybrid Swap，不是历史、回滚或发布事务系统。

SFTP 没有标准目录交换操作，Rename 之间存在短暂切换窗口；本方案不宣称严格零停机或跨多个目录的全局事务。

## Dry-run 与 Remote-plan 契约

- `--dry-run`：执行 Build、扫描并冻结本地视图；远端连接、远端读写、命令和 State 写入均为零。
- `--remote-plan`：在冻结后建立只读连接，读取 Ownership/Recovery 和路径类型，显示 Adoption/Delete/Stage 计划；上传、删除、Recovery 修复、Manifest/State 写入和 `after_deploy` 均为零。
- 普通部署：远端 Preflight 后显示完整计划并确认，才创建 Root、Stage、Backup 与 Recovery。

确认后的首个写操作前，执行端重新读取 Ownership 的原始字节 Hash，并重新 `lstat` 当前与历史全部受管直接路径。任一事实变化抛出 Stale Plan，且不得创建 Root、Stage、Backup、Recovery 或执行普通上传。Workspace 在任一仓写入前先复核全部选中仓库。

所有 Stage 上传（包括重试和重连）结束后、开始 `SWAPPING` 前，执行端再次核对 Ownership Hash、Recovery Record 和全部计划路径类型；Stale 时只清理执行器拥有的内部 Stage/Backup/Recovery，清理失败则保留 Recovery 供显式 `--recover` 续作，在线路径不修改。每条路径在 Backup 前还必须等于计划类型；写入新 Ownership 前再次核对旧 Ownership Hash。这是运行期逐边界的新鲜度保证，不是远端租约，仍不支持多发布器并发写同一受管路径。

## 明确不做

Root Mirror、完整远端扫描、多 Hybrid 同根协调、Hybrid 所有权自动转移、FTP Hybrid、自动回滚、Workspace 全局 Hook、发布事务和通用远端运维框架均不属于 v1.4.0。
