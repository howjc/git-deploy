# git-deploy v1.1.0

v1.1.0 增加单仓 Target Lock、Git Common Dir State、明确可执行位，以及完整委托系统 SSH 的 Native OpenSSH SFTP Backend。

## Native OpenSSH

- 配置 `ssh_host_alias` 自动选择系统 `ssh`/`sftp`；直连 host 继续使用 Paramiko。
- 完整保留 OpenSSH Config、Include、Match、ProxyJump、ProxyCommand、Host Key 和当前 SSH Agent 行为。
- 不读取私钥、不管理 `SSH_AUTH_SOCK`、不调用 Windows `ssh.exe`、不启用 Agent Forwarding。
- 使用私有 `0700` Control Socket 目录和短生命周期 ControlMaster；多文件只认证一次。
- common-dir 路径过长时安全回退到当前 UID 专属短临时根，避免 Unix socket 长度失败。
- SFTP Batch 支持父目录、临时上传、chmod、Rename、Backup Swap、Delete、错误映射和清理。

## 单仓可靠性

- State 与 Lock 改用 `git rev-parse --git-common-dir`，linked worktree 共享部署进度和互斥锁。
- `<git-common-dir>/git-deploy/<target>.lock` 在 Build 前非阻塞获取，并记录 PID、Host、时间和 Target。
- Git `100755` 通过 SFTP 发布为 `0755`；FTP 无法保证时在连接前拒绝。
- Doctor 默认只读检查 Root；`--create-root` 才允许创建。
- `git-deploy init` 生成不含凭据、Target 保持注释的配置模板，并给出 pnpm/npm/yarn/Composer 建议。

## 验证

- WSL2 中系统 `/usr/bin/ssh`/`sftp`；
- 真实 OpenSSH Alias、非默认 Port、严格 Host Key、IdentityFile、ProxyJump、ControlMaster；
- Native 首次部署与第二次 No-op；
- Fake Process 调用顺序、无 BatchMode、Backend 选择、缺失/Windows executable、路径转义、Backup Swap、Pool 复用、清理；
- linked worktree Common State/Lock、Doctor 只读、Executable Mode、Init；
- Python 3.11/3.12、Ruff、ty、wheel/sdist 和隔离安装。

真实 1Password/Windows Hello 授权属于用户 SSH 环境的人工增强验收；自动门禁使用隔离临时密钥，不读取真实 Vault 或私钥配置。
