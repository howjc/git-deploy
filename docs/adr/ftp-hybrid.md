# ADR：FTP In-place Hybrid

状态：Accepted；v1.5.3 Session/Remote Alias 契约收口

日期：2026-07-19

## 问题

部分项目只能通过 FTP 发布，但前端构建产物仍需铺到包含后端代码、`.env`、上传目录和其他未知内容的混合根目录。普通 Incremental Output 的删除事实依赖本地 State；State 丢失或换机器后无法安全清理历史 Root File、Mirror Directory 孤儿文件和已移除的整个目录。对根目录做全量 Mirror 又会越过所有权边界。

## 决策

相同的 `mode = "hybrid"` 根据 Target 协议选择明确 backend：

- `SFTP Staged Hybrid` 保持现有目录 Stage/Backup/Swap、显式 Recovery 和可恢复回滚语义；
- `FTP In-place Hybrid` 使用文件级 Stage/Verify/Publish，遵守 Upload-first、Prune-last、Ownership-last 和 State-after-ownership；
- FTP 只提供 Forward Resume，不提供目录 Swap、旧目录树回滚或 `after_deploy`；
- 两种 backend 继续复用 `.git-deploy/hybrid/<mapping>.json` Ownership Schema；未被 Ownership 或经验证 Pending 声明拥有的未知根目录内容永不接管或删除。

FTP Hybrid 强制要求大小写敏感且保留 Unicode normalization 的 UTF-8 路径、FEAT/MLSD、二进制 STOR/RETR、跨目录 Rename、Rename Replace、DELE 和 RMD。能力必须由用户显式运行 `git-deploy doctor TARGET --probe-ftp-hybrid` 探测：服务器必须广告 `UTF8`；客户端会尝试 `OPTS UTF8 ON`，若服务器以永久 5xx 拒绝（Pure-FTPd 等 always-on UTF-8 实现常见），在 FEAT 已广告 UTF8 时仍启用客户端 UTF-8 encoding，并通过中文名及 NFC/NFD 精确 MLSD/RETR/Rename/Delete 完成真实路径契约证明。结果缓存为绑定 Target Fingerprint 与 Server Banner 的 Capability Profile Schema 3；旧 Schema 1/2 升级后必须重新 Probe。普通部署和只读 `--remote-plan` 不得静默写 Probe。

一次 `enable_utf8()` 成功后，UTF-8 要求绑定 Transport 生命周期而非单条 FTP Session。网络重试的新 Session 必须在 Login 后重新核对 Server Banner、FEAT UTF8、`OPTS UTF8 ON`（或 always-on 5xx 兼容路径）与客户端 encoding；任一步失败都在 STOR/RETR/DELE/RMD/Rename 等业务命令前关闭连接。`close()` 与 retry invalidation 不得清除此要求。

## 为什么不用 Incremental

Incremental 的本地 State 只描述上次在当前 Git Common Dir 成功部署的文件。它不能作为换机器、State 损坏或历史目录删除的远端所有权证据，也不能可靠枚举 Mirror Directory 内的孤儿。因此 FTP Hybrid 必须使用 Remote Ownership 和 MLSD Typed Scan。

## Mirror 文件增量（Local State）

FTP In-place 对 Mirror Directory 内每个文件单独 Stage/Publish，因此 Local State 必须持久化 `<directory>/<relative>` 的 SHA256/Size（与 Root File 同一内容契约）。

### Mirror 模式（`deploy.ftp_incremental_mirror`，默认 `true`）

| 模式 | 配置 | 行为 |
|------|------|------|
| **LOCAL-STATE INCREMENTAL** | `ftp_incremental_mirror = true`（默认） | Local State hash 未变且远端路径仍存在时可跳过上传；**不校验远端文件内容**是否等于 State |
| **STRONG** | `ftp_incremental_mirror = false` 或 CLI `--full` | 当前全部 Hybrid Root/Mirror 文件进入上传队列（强制收敛） |

远端 Size/Modify 仍不可作为内容证明。面板改写、备份回滚、外部覆盖等漂移需 `--full` 或关闭增量。孤儿清理继续依赖 MLSD Typed Scan，与上传增量无关。升级后若旧 State 没有嵌套路径，下一次部署会按「previous 缺失」重传 Mirror 文件一次并写入完整 State。Remote Plan 会明示 `FTP MIRROR MODE` 与 `REMOTE CONTENT HASH` 契约行。

## 业务文件校验边界（Stage RETR；可选 Final RETR）

每个业务上传文件的内容证明默认发生在 **Stage STOR 之后的整文件 RETR SHA256**（含 Publish 重试时的 restage）。Publish 执行已 Probe 的 Rename Replace，检查 Stage 源路径已被消费、最终路径类型为 File；默认 **不再对最终路径做整文件 RETR**（`CONTENT PROOF: STAGE-VERIFIED RENAME-TRUSTED`）。设置 `deploy.ftp_verify_final = true` 时在 Rename 后再做一次 Final RETR SHA256。Pending Marker 与 Ownership 等小元数据仍使用 `publish_verified_bytes`（Stage RETR + Final RETR）。

## 并行 FTP 会话

`deploy.ftp_connections`（**默认 1**，范围 1–16）控制 Hybrid **Stage** 与 **Publish** 阶段各自使用的并行控制会话数。默认串行以兼容共享主机与单连接限额；并行为显式 opt-in。实现先建立全部 sibling 会话再启动 Worker；sibling 建连失败时安全降级到已成功的会话数（至少 primary），并输出 WARNING 与 effective connections。每个 worker 持有独立 `FTPTransport`（独立控制连接与 PASV 数据连接），继承主会话的 UTF-8/Banner 契约；主连接不关闭。仍是单发布器进程内并行，不允许多机器并发发布。普通 Source/Incremental 队列、目录 MKD、Prune、Ownership/Pending 元数据仍走主连接串行路径。

## 为什么不做 FTP Directory Swap

FTP 没有跨服务器一致的目录原子替换、目录备份恢复和符号链接类型契约。模拟 SFTP 目录 Swap 会产生更大的不可恢复窗口。首版只对每个文件做经过 Probe 证明的 Rename Replace；所有当前文件发布完成后才清理孤儿。

## Forward Resume

`.git-deploy/ftp-hybrid/pending/<mapping>.json` Schema 2 记录冻结的 Local Manifest、非 Hybrid Operation/Policy Hash、Previous State Hash、旧/新 Ownership Hash、下一份 Local State 与阶段：`PREPARED`、`FILES_PUBLISHED`、`PRUNED`、`OWNERSHIP_COMMITTED`、`STATE_COMPLETE`。前三个阶段必须验证当前 Local Manifest、HEAD、稳定计划、Previous State 与严格 Ownership Hash 矩阵；`FILES_PUBLISHED` 不会重放普通 Source/Incremental Operations。Schema 1 的提交前 Marker 因缺少这些事实而 Fail Closed，提交后 Marker 仍可显式恢复。后两个阶段只接受 Next Ownership，并通过显式 `--recover` 始终重写 Marker 中冻结的 State 后再清理，不运行 Build、不读取当前 State、不扫描当前 Hybrid 输出。普通部署发现后提交 Marker 时在 Build/Plan 前提示改用 `--recover`。

FTP Hybrid 的 Source、Incremental、Hybrid Direct、历史受管根以及内部 `.git-deploy` 共用一个 NFC + casefold 根命名空间。除了本地联合检查，Capability Probe 和 Remote Plan 还必须先单层读取远端 Root Direct Entries：精确同名继续 Adoption/Ownership 语义，NFC + casefold 等价但拼写不同的未知项在任何写入前拒绝，无关未知项不递归。历史 Source commit 不可用时也不得猜测。

稳定 Source 内容契约只服务 FTP Hybrid Pending Resume。普通 SFTP、Native OpenSSH、FTP Incremental 与 SFTP Hybrid 不额外哈希 Source；FTP Hybrid 从 `ls-tree` 保留 Blob OID，并通过单个 `git cat-file --batch` 流式计算 SHA256/Size，避免逐文件子进程和整 Blob 入内存。

每次 Freshness Gate 必须先清空 FTP MLSD、NLST 和 Missing Cache。清理只强制删除当前 Deployment Stage 和 Pending Marker；共享 Stage Parent 仅 best-effort 删除，旧 Orphan Stage 不得阻断当前部署完成。Doctor 只读报告 Orphan，不静默删除。

## 类型变化与接管

FTP In-place Hybrid 拒绝受管直接路径的 File→Directory 和 Directory→File，因为转换必然先删除旧类型且没有可恢复目录备份。用户应先从聚合视图移除旧类型、部署确认删除，再添加新类型并用 `--full` 审阅。

首次遇到未拥有的同名当前路径时，普通部署拒绝；`--full` 只接管当前本地同名路径，不接管未知根内容。经过验证的 Pending 路径是上次部署的 Forward Resume，不要求再次 Adoption。

## 单发布器边界

FTP Rename Replace 无法保证 Planned-Missing 的最后一刻不覆盖。FTP Hybrid 仅支持个人、单发布器使用；部署期间不得由另一台机器、CI、面板或手工 FTP 修改受管路径。Plan 必须明确显示这一边界。

## 术语

- **SFTP Staged Hybrid**：通过目录 Stage/Backup/Swap 发布的现有 backend。
- **FTP In-place Hybrid**：对在线树执行文件级 Stage/Publish 的 FTP backend。
- **Forward Resume**：从远端 Pending Marker 验证事实后继续向前收敛，不恢复旧版本。
- **Upload-first**：所有当前文件先完成上传、校验和发布。
- **Prune-last**：当前文件全部发布后才删除孤儿文件与目录。
- **Capability Profile**：显式 Probe 产生、绑定目标与服务器 Banner 的本地能力证明。

## 明确不做

FTP Directory Swap、FTP Rollback、FTP `after_deploy`、多发布器协调、多 Hybrid 同根、全根目录 Reconcile、LIST/NLST 类型猜测和不安全降级均不属于当前范围。
