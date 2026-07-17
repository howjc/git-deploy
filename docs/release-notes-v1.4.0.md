# git-deploy v1.4.0

v1.4.0 增加单 Hybrid Output，让多个前端构建结果先在本地聚合，再安全部署到包含后端与环境文件的远端混合根目录。

## Local Aggregation 与 Hybrid

- `OutputConfig` 增加可选 `name` 和默认兼容的 `mode = "incremental"`。
- 一个配置最多一个 SFTP `mode = "hybrid"`，必须具名、使用 `remote = "."` 且不能显式配置 `delete_removed`。
- 聚合根直接文件按 Hash 增量发布；直接目录每次完整 Stage/Swap，空目录也会保留。
- 默认 Source 排除 `.deploy/**`，并强保护 `.git-deploy/**`；未忽略聚合根会警告，Clean Worktree 模式直接拒绝。
- 提供冲突/符号链接 Fail Closed 的 `examples/aggregate_frontend_builds.py`。

## Remote Ownership 与 Adoption

- `.git-deploy/hybrid/<mapping>.json` 持久化 Project ID、Mapping、Remote、历史直接目录/文件和 Commit。
- 本地 State 丢失后，历史 Root File 和 Mirror Directory 删除仍由远端 Ownership 驱动。
- 无所有权但同名路径已存在时，普通部署拒绝；显式 `--full` 只 Adoption 当前本地同名路径。
- 未被 Manifest 声明拥有的 `index.php`、`.env`、后端、运行时及未知内容永不扫描或删除。
- Schema、身份、编码、大小和符号链接检查全部 Fail Closed。

## Planning、Recovery 与 Workspace

- `--dry-run` 保持零连接；新增 `--remote-plan`，只读 Ownership/Recovery 和路径类型，不写远端、命令或 State。
- Mirror Directory 使用 `.git-deploy/stage`、`backup` 和窄 Recovery Record；Ownership Commit 前恢复旧值，Commit 后继续清理。
- 执行顺序固定为普通文件 → Hybrid Stage/Swap/Delete → Ownership → `after_deploy` → Local State → Backup Cleanup。
- Workspace Combined Plan 显示 Local/Remote Hybrid、Adoption、Commands 与 Bytes；物理 Root 冲突仍在首个 Build/Remote Write 前拒绝。
- Doctor 只读报告 Git Ignore、Local Root、Project ID、Ownership、Recovery、内部目录、Owned Path Type 与 Adoption。

## 验证

- Fake Transport 覆盖 State 丢失、Adoption、未知内容、Swap/Command 失败、Recovery 和只读计划。
- 隔离 Docker/OpenSSH 分别验证 Paramiko 与 Native 的真实 Manifest Read、Stage/Swap、空目录、历史删除和未知内容保护。
- 发布门禁覆盖 Python 3.11/3.12、Ruff、ty、lock、wheel/sdist、隔离安装、PR CI 与 Main/Tag Blob。

本版本仍不提供 Root Mirror、完整远端扫描、FTP Hybrid、多 Hybrid 同根协调、自动回滚或发布事务。
