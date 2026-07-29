# git-deploy v1.5.0

v1.5.0 增加 FTP In-place Hybrid，让只有 FTP 权限的项目也能在保护未知混合根内容的前提下清理前端 Mirror 孤儿、历史 Root File 和整个历史目录。

## FTP In-place Hybrid

- 相同 `mode = "hybrid"` 根据 Target 自动选择 SFTP Staged 或 FTP In-place backend；现有 SFTP 行为与 Schema 保持兼容。
- 新增显式 `git-deploy doctor TARGET --probe-ftp-hybrid`：在受保护的 `.git-deploy/ftp-probe` 范围验证 MLSD、Binary STOR/RETR、跨目录 Rename、Rename Replace、DELE 与 RMD，并保存绑定 Target/Banner 的本地 Capability Profile。
- FTP Remote Scanner 只使用 MLSD Typed Entry，限制深度与条目数；未知类型、符号链接、权限/列表失败一律 Fail Closed。
- 当前文件经过 Stage、RETR SHA256 校验、Rename Replace 与 Final RETR 校验；所有文件先发布，之后才 Prune 孤儿文件和空目录。
- Remote Ownership 继续使用 schema 1；未知根文件和目录永不接管或删除，Local State 丢失仍可依据 Ownership 清理历史内容。
- `.git-deploy/ftp-hybrid/pending/<mapping>.json` 提供 PREPARED 到 STATE_COMPLETE 的单向 Forward Resume。Publish、Prune、Ownership、State 或 Cleanup 任一阶段中断后，普通部署会验证冻结事实并自然继续。

## CLI 与计划

- `--dry-run` 仍保持零连接，并提示 FTP 精确扫描需要 `--remote-plan`。
- `--remote-plan` 只读 Capability Profile、Ownership、Pending 和受管 MLSD 树，零远端写入。
- Plan 明确显示 FTP IN-PLACE、Upload-first、Prune-last、Forward Resume，以及无目录 Swap/回滚/after_deploy/并发 No-overwrite 保证。
- FTP Hybrid 的受管直接路径类型变化会被拒绝；首次同名路径仍需 `--full` Adoption。

## 兼容性与限制

- 配置 Schema、Local State 与 Remote Ownership Schema 没有破坏性变化；普通 FTP Incremental、SFTP Hybrid、Native OpenSSH 与 Workspace 顺序执行保持兼容。
- FTP Hybrid 只支持单发布器，不提供目录原子切换、旧树回滚、`after_deploy`、多发布器锁或 Full Root Reconcile。
- 自动集成测试覆盖 pyftpdlib Passive/Active、Mixed Root、State Loss、Adoption、Orphan/历史目录清理和各 Pending 阶段。vsftpd、Pure-FTPd/ProFTPD 与实际目标服务器属于可选人工兼容验证，不需要把真实凭据写入配置、日志或报告。
