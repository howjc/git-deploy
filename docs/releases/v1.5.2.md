# git-deploy v1.5.2

v1.5.2 收口 FTP In-place Hybrid 的 Pending 计划契约、跨机器冻结 State 恢复、根命名空间与 Unicode 路径语义。

## Pending 与恢复

- Pending 升级为 Schema 2，绑定稳定的 Source/Incremental Operation、内容 SHA/Size、策略、`--full`、Previous Commit 与 Previous State Hash。
- PREPARED/FILES_PUBLISHED/PRUNED 恢复会在任何远端写入前拒绝 State、配置、HEAD 或计划漂移；FILES_PUBLISHED 不再重放普通 Operation Queue。
- OWNERSHIP_COMMITTED 和 STATE_COMPLETE 的显式 `--recover` 都先原子写入 Pending 中冻结的 State，再推进或清理 Marker；空 clone、旧 State 与重复恢复均收敛。
- 普通单项目和 Workspace 部署在 Build/Plan 前检查后提交 Pending，并明确提示使用 `--recover`。

## 路径安全

- Source、Incremental、Hybrid Direct、历史受管根与内部 `.git-deploy` 使用统一 NFC + casefold 根命名空间门禁，并在 FTP 连接前拒绝冲突。
- Capability Profile 升级为 Schema 3；服务器必须广告 UTF8、接受 `OPTS UTF8 ON`，并通过中文名、NFC/NFD 精确 MLSD/RETR/Rename/Delete Probe。Schema 1/2 必须重新 Probe。

## 验证边界

- 自动门禁覆盖 Python 3.11/3.12、Ruff、ty、构建与隔离安装，以及本地 pyftpdlib Passive/Active 和现有 SFTP/Native OpenSSH 回归。
- 实际目标 FTP Probe 与完整 Deployment 保持独立的可选人工增强；不会读取、输出或记录真实凭据，也不阻塞已验证的自动主线。
