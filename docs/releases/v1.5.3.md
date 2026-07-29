# git-deploy v1.5.3

v1.5.3 收口 FTP 重连后的 UTF-8 Session 契约、远端未知 Root Alias 写前门禁，并移除非 FTP Hybrid 路径的额外 Source 哈希成本。

## FTP Session 安全

- `enable_utf8()` 成功后把 UTF-8 要求绑定到 Transport 生命周期；`close()` 和 retry invalidation 不清除该要求。
- 每个重连 Session 在 Login 后重新核对 Server Banner、FEAT UTF8、`OPTS UTF8 ON` 和客户端 encoding；失败时在任何业务命令前关闭连接。
- 自动测试覆盖 Unicode Source/Incremental Upload、Delete、Hybrid Stage/Publish/RMD，以及 Passive/Active、OPTS Failure 和 Banner Drift。

## Remote Root Alias Gate

- Capability Probe 在首次 MKD 前把计划的 `.git-deploy` 与远端 Root Direct Entries 做 NFC + casefold 比对。
- FTP Remote Plan 与 Freshness Gate 同样覆盖当前/历史 Source、Incremental、Hybrid 与 Internal 根；异形未知名称 Fail Closed，精确同名继续 Adoption/Ownership，无关未知根不递归也不修改。
- `--remote-plan` 也在 Build 前拦截 post-commit Pending，避免无意义的本地构建。

## 计划性能

- Stable Source Content Contract 只在 `Target = FTP` 且存在 Hybrid Mapping 时生成。
- Git tree entry 保留 Blob OID；单个 `git cat-file --batch` 按块计算 SHA256 和 Size，不再逐文件启动 Git 或把大 Blob 一次读入 Python 内存。
- 10k 文件本地基准为 0.822 秒（约 12,169 files/s；结果仅用于本机回归观察，不作为跨机器性能承诺）。

## 验证边界

- 自动门禁覆盖 Python 3.11/3.12、Ruff、ty、构建、隔离 wheel 安装、本地 pyftpdlib Passive/Active，以及 SFTP/Native OpenSSH 回归。
- 实际目标 FTP Probe 与 Canary Deployment 保持独立的可选人工增强；本轮不读取或记录真实凭据，也不以它们阻塞自动主线。
