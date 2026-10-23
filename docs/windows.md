# Windows 本机客户端

适用范围：Windows 10/11 本机运行 git-deploy，向现有 FTP/SFTP 服务器发布。远端路径继续使用 POSIX 格式，远端 `after_deploy` 继续由服务器 Shell 执行。Windows 服务器不在本轮适配范围内。

## 安装与检查

需要 Python 3.11+、Git for Windows 和 uv。SFTP 还需要 PATH 中的 OpenSSH `ssh.exe`；Alias 后端另外需要 `sftp.exe`。可用 PowerShell 或 cmd 启动，无需 WSL、Git Bash 或 GNU make。

当前仓库源码包含兼容改动；已经发布的 v1.8.1 Release wheel 不包含这些改动。请从包含本改动的源码目录安装，或安装由该源码构建的新 wheel：

```powershell
uv tool install . --force
git-deploy --version
git-deploy --help
Get-Command git, ssh, sftp
```

开发安装和门禁：

```powershell
uv sync --all-groups
uv lock --check
uv run pytest -q
uv run ruff check src tests examples
uv run ty check src
uv build --clear
uv run python tests/smoke_wheel.py
```

本地测试中的 Linux 容器服务端、POSIX ControlMaster 和 Shell/SIGPIPE 专属检查只在 Linux 执行；Windows 会执行部署编排、Mock 传输、文件锁、构建超时、状态替换、PowerShell 与 wheel 冒烟测试，以及本机普通 FTP 同步测试。FTP Hybrid 要求大小写敏感的远端；Windows 本机 NTFS FTP fixture 不满足这一服务端条件，其对应服务端集成用例在 Linux 执行。

## 路径与构建

`--config`、`--workspace`、SSH 文件和项目目录支持中文、空格及 Windows 路径。含反斜杠的 TOML 字符串使用单引号，也可统一使用正斜杠：

```toml
[[outputs]]
local = 'frontend\dist'
remote = 'public/dist'

[build]
steps = ['pnpm install --frozen-lockfile', 'pnpm run build']
timeout = 900
```

Windows 上 `build.steps` 由 `cmd.exe` 执行，即使 git-deploy 从 PowerShell 启动也是如此。`npm.cmd`、`pnpm.cmd` 等可直接调用；需要 PowerShell 语法时显式调用 `powershell -NoProfile -File scripts/build.ps1` 或 `pwsh -NoProfile -File scripts/build.ps1`。跨系统项目优先使用 Python/Node 构建脚本，避免把 `export`、`rm`、`touch` 等 Unix 命令直接放进步骤。构建超时或 Ctrl-C 时使用 Windows `taskkill /T /F` 清理构建进程树。

本地文件系统不支持的名称不能作为本地产物名称。源码使用 Git blob 原始字节，上传缓存使用与远端文件名无关的名字，避免 `CON`、冒号、深层路径及大小写不同的远端文件在本地冲突。远端路径始终保留 `/`。输出扫描拒绝符号链接和 Windows junction/reparse point；Windows Target 名称不能使用 `CON`、`NUL` 等保留名，也不能仅以大小写区分。

## SFTP 与 SSH Agent

直接 `host`/`username` Target 使用 Paramiko，支持现有 `password_env`、`key_file` 和 Agent 设置。密码仅通过当前进程环境变量传入，例如 `$env:DEPLOY_SFTP_PASSWORD = ...`，不写入 TOML。

`ssh_host_alias` Target 使用原生 OpenSSH，继续委托 OpenSSH 解析 `%USERPROFILE%\.ssh\config`、Include、ProxyJump 和 ProxyCommand：

```toml
[targets.prod]
protocol = 'sftp'
ssh_host_alias = 'project-prod'
remote_root = '/srv/app'
```

部署前确认 `ssh -o BatchMode=yes project-prod "exit 0"` 能成功，并事先确认服务器 Host Key。Windows Alias 后端要求可非交互使用的密钥或 SSH Agent；需要密码时使用直接 `host` + `password_env` Target。

Windows OpenSSH 不采用 POSIX ControlMaster，因此每次 SSH/SFTP 调用会重新建立连接，Alias 在调用前重新检查、端点仍固定为已审阅值；Workspace 不保证只认证一次。Agent 可能多次要求授权，大量小文件建议使用直接 Paramiko Target。WSL 继续使用 Linux `ssh`/`sftp` 和 ControlMaster，不回退到 Windows `.exe`。Native Hybrid 需要支持 `sftp rename -l` 的 OpenSSH 客户端；较旧的 Windows 系统内置版本应升级 OpenSSH 或改用直接 Paramiko Target，不会降级为可能覆盖未知路径的 rename。

参见 [Windows OpenSSH 设计说明](https://github.com/PowerShell/Win32-OpenSSH/wiki/About-Win32-OpenSSH-and-Design-Details) 和 [WSL/1Password 指南](native-openssh-wsl.md)。

## PowerShell 补全

Windows PowerShell 5.1：

```powershell
git-deploy completion install powershell
```

PowerShell 7：

```powershell
git-deploy completion install pwsh
```

安装程序询问所选 Shell 的 `$PROFILE.CurrentUserAllHosts`，兼容 Documents/OneDrive 重定向和两代 PowerShell 的不同目录，并保留现有 profile 内容。只在显式安装时修改 profile；重复安装不会重复追加。重新打开终端后使用 `git-deploy <Tab>`。补全支持操作、Target、选项和 `--config`/`--workspace` 指定的本地配置，不连接服务器、不执行输入行中的表达式。cmd 可正常运行 CLI，但不提供 Target 动态补全。

只对当前会话加载，不修改 profile：

```powershell
git-deploy completion powershell | Out-String | Invoke-Expression
```

## 状态与锁

Windows 使用进程级字节范围锁，POSIX 使用 `flock`。同一 Git 仓库的 linked worktree 共用锁，争用会立即失败并显示持有者信息；进程退出后由操作系统释放。State、FTP 能力缓存都先写临时文件并刷盘，关闭后原子替换；只有 POSIX 另外同步目录项。保持现有成功后提交 State 的规则，失败不会提前推进部署基线。
