# ADR：FTP In-place Hybrid

状态：Accepted；目标版本 v1.5.0

日期：2026-07-18

## 问题

部分项目只能通过 FTP 发布，但前端构建产物仍需铺到包含后端代码、`.env`、上传目录和其他未知内容的混合根目录。普通 Incremental Output 的删除事实依赖本地 State；State 丢失或换机器后无法安全清理历史 Root File、Mirror Directory 孤儿文件和已移除的整个目录。对根目录做全量 Mirror 又会越过所有权边界。

## 决策

相同的 `mode = "hybrid"` 根据 Target 协议选择明确 backend：

- `SFTP Staged Hybrid` 保持现有目录 Stage/Backup/Swap、显式 Recovery 和可恢复回滚语义；
- `FTP In-place Hybrid` 使用文件级 Stage/Verify/Publish，遵守 Upload-first、Prune-last、Ownership-last 和 State-after-ownership；
- FTP 只提供 Forward Resume，不提供目录 Swap、旧目录树回滚或 `after_deploy`；
- 两种 backend 继续复用 `.git-deploy/hybrid/<mapping>.json` Ownership Schema；未被 Ownership 或经验证 Pending 声明拥有的未知根目录内容永不接管或删除。

FTP Hybrid 强制要求 FEAT/MLSD、二进制 STOR/RETR、跨目录 Rename、Rename Replace、DELE 和 RMD。能力必须由用户显式运行 `git-deploy doctor TARGET --probe-ftp-hybrid` 探测，并缓存绑定 Target Fingerprint 与 Server Banner 的本地 Capability Profile。普通部署和只读 `--remote-plan` 不得静默写 Probe。

## 为什么不用 Incremental

Incremental 的本地 State 只描述上次在当前 Git Common Dir 成功部署的文件。它不能作为换机器、State 损坏或历史目录删除的远端所有权证据，也不能可靠枚举 Mirror Directory 内的孤儿。因此 FTP Hybrid 必须使用 Remote Ownership 和 MLSD Typed Scan。

## 为什么不做 FTP Directory Swap

FTP 没有跨服务器一致的目录原子替换、目录备份恢复和符号链接类型契约。模拟 SFTP 目录 Swap 会产生更大的不可恢复窗口。首版只对每个文件做经过 Probe 证明的 Rename Replace；所有当前文件发布完成后才清理孤儿。

## Forward Resume

`.git-deploy/ftp-hybrid/pending/<mapping>.json` 记录冻结的 Local Manifest、旧/新 Ownership Hash、下一份 Local State 与阶段：`PREPARED`、`FILES_PUBLISHED`、`PRUNED`、`OWNERSHIP_COMMITTED`、`STATE_COMPLETE`。重跑必须验证 Project、Mapping、Remote、Target Fingerprint、Ownership Hash、Local Manifest Hash 和 HEAD，验证通过后只向前收敛；Local Build 或 Ownership 不匹配时 Fail Closed。

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

FTP Directory Swap、FTP Rollback、FTP `after_deploy`、多发布器协调、多 Hybrid 同根、全根目录 Reconcile、LIST/NLST 类型猜测和不安全降级均不属于 v1.5.0。
