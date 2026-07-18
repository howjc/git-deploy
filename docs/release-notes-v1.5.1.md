# git-deploy v1.5.1

v1.5.1 是 FTP In-place Hybrid 的安全收口版本，修复 v1.5.0 深度审计发现的路径语义、内部清理、后提交恢复和 Freshness 问题。

## 路径语义

- Capability Profile 升级为 Schema 2，新增 `case_sensitive_paths`；v1.5.0 Schema 1 必须重新运行 `git-deploy doctor TARGET --probe-ftp-hybrid`。
- Probe 同时创建两个大小写变体，验证 MLSD 精确名称、独立 RETR/DELE 与 case-only Rename；大小写不敏感服务器 Fail Closed。
- Local Hybrid Scanner 与 Remote MLSD Scanner 都拒绝同一目录内 NFC + casefold 冲突，避免 Upload/Delete 指向同一底层路径。

## 恢复与清理

- 当前部署只强制清理自己的 Stage Root 与 Pending Marker；共享 Stage/Probe Parent 仅 best-effort 删除，旧 sibling 不再阻断成功。
- 初次 PREPARED Marker 写入失败会 best-effort 清理刚创建的 Stage，并保留带 Deployment ID 的原始错误说明。
- Doctor 只读报告 Orphan Stage 的 ID、年龄与条目数，不静默删除。
- FTP `OWNERSHIP_COMMITTED` / `STATE_COMPLETE` 接入显式 `--recover`：不运行 Build、不读取当前 State、不扫描当前 Hybrid，使用 Pending 中冻结的 State 完成收口。

## Freshness 与状态机

- `FTPTransport.refresh_remote_metadata()` 在每次 Freshness Gate 前清除 typed MLSD、NLST 与 missing caches。
- Pending Ownership Hash 矩阵收紧：PREPARED/FILES_PUBLISHED 只接受 previous，PRUNED 接受 previous/next，OWNERSHIP_COMMITTED/STATE_COMPLETE 只接受 next。
- `--probe-ftp-hybrid` 本身明确表示重新探测并覆盖 Profile；移除没有独立语义的 `--reprobe`。

## 验证边界

- 自动门禁覆盖 Python 3.11/3.12、Ruff、ty、构建与隔离安装，以及 pyftpdlib Passive/Active、大小写门禁、Orphan Stage、Pending 写失败、缓存 Freshness 和后提交恢复。
- vsftpd、ProFTPD/Pure-FTPd、Windows/IIS 与实际目标 FTP 仍是可选人工兼容验证；不读取或记录真实凭据。
