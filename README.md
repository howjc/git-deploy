# git-deploy v1-lite

`git-deploy` 是个人日常使用的 Git 感知型构建与文件同步工具：在本地运行构建，比较上次成功部署与当前 `HEAD`，再通过 SFTP 或 FTP 只同步变化的源码和构建产物。

日常部署只有一条命令：

```bash
git-deploy
```

v1-lite 不再提供 v0.3 的 Expected State、Generation、CAS、Transaction、History、Verify、Recover 或 Rollback。旧实现冻结在 `legacy/v0.3` 分支和 v0.3.x tags；v1 配置和 state 与旧版不兼容。

## 安装

需要 Python 3.11+ 和 Git。稳定版通过 GitHub Release 分发 wheel：

```bash
uv tool install \
  https://github.com/howjc/git-deploy/releases/download/v1.3.0/git_deploy-1.3.0-py3-none-any.whl
git-deploy --version
```

开发安装：

```bash
uv sync --all-groups
uv tool install --editable .
```

## 配置

在项目根目录创建不提交到 Git 的 `deploy.toml`：

```toml
default_target = "dev"

[source]
include = ["**"]
exclude = ["tests/**", "docs/**"]
protect = ["storage/private/**"]
require_clean_worktree = false

[build]
steps = [
  "pnpm install --frozen-lockfile",
  "pnpm run build",
  "composer install --no-dev --prefer-dist --optimize-autoloader --no-interaction"
]
timeout = 900

[[outputs]]
local = "dist"
remote = "public/dist"
delete_removed = true

[[outputs]]
local = "vendor"
remote = "vendor"
delete_removed = true

[targets.dev]
protocol = "sftp"
ssh_host_alias = "project-dev"
remote_root = "/srv/project-dev"
after_deploy = [
  "php artisan optimize:clear",
  "sudo -n /usr/bin/systemctl restart application.service",
  "sudo -n /usr/bin/systemctl is-active --quiet application.service"
]
command_timeout = 120

[targets.prod]
protocol = "ftp"
host = "ftp.example.com"
username = "deploy"
password_env = "DEPLOY_FTP_PASSWORD"
remote_root = "/public_html"
passive = true

[deploy]
retries = 3
retry_delay = 2
```

相对路径以 `deploy.toml` 所在目录（即项目根目录）解析。`build.steps` 和 `after_deploy` 都是可信配置，会分别在本地与远端 Shell 中执行，不是沙箱；不要把密码、Token 或私钥写进命令。

以下保护规则始终生效，不能被配置移除：`.env`、`.env.*`、`uploads/**`、`runtime/**`、`storage/cert/**`、`**/*.key`、`**/*.pem`。默认也排除 `.git/**`、`node_modules/**` 和 `storage/logs/**`。

配置 `ssh_host_alias` 时自动使用当前环境的 Native OpenSSH：

- 完整读取 OpenSSH Config、Include、ProxyJump 和 ProxyCommand；
- 继承当前 `SSH_AUTH_SOCK`，兼容 WSL 中的 1Password SSH Agent；
- 使用临时 ControlMaster，多文件只建立一次认证连接；
- 连接前复核 Alias，并将已审阅的 HostName、User、Port 固定到实际 OpenSSH 命令；
- `timeout` 只生成 OpenSSH `ConnectTimeout`，不会中断 1Password 授权或完整文件传输；
- 文件同步和 `after_deploy` 复用同一条 ControlMaster，不会再次触发生物认证；
- 不读取私钥、不管理 Agent Socket、不启用 Agent Forwarding；
- 只调用当前环境的 POSIX `ssh`/`sftp`，不会回退 Windows `ssh.exe`。

Alias Target 只配置 `ssh_host_alias`、可选 `ssh_config_file` 和 `remote_root`；不要混入 `host`、`username`、`port`、`key_file` 或 `password_env`。只配置 `host`/`username` 的 SFTP Target 继续使用 Paramiko，支持 `key_file`、`password_env`、`known_hosts_file` 和 SSH Agent。

FTP 必须通过 `password_env` 读取密码，不接受 TOML 明文密码，也不支持 `after_deploy`。v1-lite 不支持 FTPS、owner/group 或远端健康检查。

## After-deploy 命令

SFTP Target 可配置最多 16 条单行 `after_deploy` 命令。它们在文件上传/删除全部成功后，按声明顺序在 `remote_root` 下执行，全部成功后才提交 State。Single Plan 与 Workspace Combined Plan 都会显示完整命令，`--dry-run` 只供审阅，不会连接远端。

命令默认 `command_timeout = 120` 秒、无 PTY、无 stdin，也不会自动重试；应使用非交互命令并尽量保持幂等。任一命令非零退出或超时会停止后续命令并保留旧 State，重新运行部署会再次同步文件和执行命令。没有文件变化时不会连接，也不会执行 `after_deploy`。

需要 sudo 时使用 `sudo -n`，并在服务器上配置精确的 `NOPASSWD` allowlist，例如：

```sudoers
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart application.service
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl is-active application.service
```

不要配置 `NOPASSWD: ALL`。本功能不提供 before/on-failure hook、自动回滚、命令重试、交互 Shell、Secret 插值、Workspace 全局命令或通用远程任务系统。

## 使用

首次先预览：

```bash
git-deploy --dry-run
git-deploy --yes
```

多环境、跳过构建和完整覆盖：

```bash
git-deploy prod --dry-run
git-deploy prod --skip-build --yes
git-deploy prod --full --yes
```

`--dry-run` 默认仍执行构建，但不连接服务器、不写 state。`--full` 上传全部受管 Git 源码与全部 outputs，不清空远端，也不删除任何未知远端文件。

只构建或诊断：

```bash
git-deploy build
git-deploy doctor
git-deploy doctor prod
git-deploy doctor prod --create-root
```

`doctor` 默认只检查配置、Git、构建命令、output、state、连接和远端根目录，不创建远端内容；只有 `--create-root` 才允许创建缺失 Root。Native OpenSSH Doctor 会显示 backend、系统命令绝对路径、Alias 和解析后的 Endpoint，并提示认证可能触发当前 SSH Agent。

新仓库可先生成无凭据模板：

```bash
git-deploy init
```

`init` 会根据 pnpm/npm/yarn/Composer lockfile 提供保守的 Build/Output 建议，但 Target 全部保持注释，必须由用户编辑，不连接服务器也不写密码。

## Thin Workspace

多个独立 Git 仓库可由一个极薄的 `deploy.workspace.toml` 按顺序编排。每仓仍保留自己的 `deploy.toml`、Build、Git 历史、State 和 Target Lock；Workspace 只保存名称、相对路径、顺序和统一的 Target 名：

```toml
default_target = "dev"

[[repositories]]
name = "api"
path = "api"

[[repositories]]
name = "web"
path = "web"
```

在 Workspace 根目录运行与单仓相同的命令：

```bash
git-deploy prod --dry-run
git-deploy prod --yes
git-deploy doctor prod
git-deploy build
git-deploy build prod  # 可选：只校验每仓都存在 prod 这个名称
```

部署前会先完成所有仓库的 Target 校验、Build、Plan 和上传字节冻结；任一 Prepare 失败时不会建立远端连接。全部成功后显示包含文件操作与各仓 `after_deploy` 的 Combined Plan，只确认一次，并按“仓库文件 → 仓库命令 → 仓库 State”的顺序部署。相同 Native OpenSSH Endpoint 会共用当前命令生命周期内的一条 ControlMaster。

Workspace 的独立 `build` 命令是纯本地操作：只加载各仓配置并顺序执行 Build，不解析 SSH Alias、不要求 `ssh`/`sftp`、不检查 Git 或远端 Root 所有权。显式附带 Target 时只检查该名称是否在每仓配置中存在。

所有仓库的物理 Endpoint 和 Remote Root 会在首个 Build 前解析；同一 Endpoint 上相同或父子嵌套的 Root 会直接拒绝，避免两个独立 State 相互覆盖。Combined Plan 会展示每仓冻结后的 Host/User/Port、Remote Root、模式、Commit 边界和冻结字节总量。

如果当前目录同时有 `deploy.toml` 与 `deploy.workspace.toml`，必须用 `--config` 或 `--workspace` 显式选择。Workspace 不提供并行、依赖图、Target Map、共享 State、全局事务或自动回滚；中途失败后直接重跑，已成功仓库会自然成为 No-op。

### 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 成功 |
| 1 | Doctor 检查失败或未分类预期错误 |
| 2 | 配置或命令用法错误 |
| 3 | 构建失败或超时 |
| 4 | Git、State 或计划安全错误 |
| 5 | 远端连接或文件操作失败 |
| 130 | Ctrl-C 中断 |

## 差异与 State

源码差异来自：

```bash
git diff --no-renames --name-status -z LAST_COMMIT..HEAD
```

未提交工作区内容默认只警告，不会上传；源码上传使用精确的 `HEAD` blob。可设置 `source.require_clean_worktree = true` 直接阻止脏工作区部署。

outputs 在构建后扫描并计算 SHA256。只有上次 state 已记录、当前已删除且对应 `delete_removed = true` 的产物才会触发远端删除；工具永远不会扫描并清理未知远端内容。

每个 target 的 state 和进程锁保存在 Git Common Dir，多个 linked worktree 共享：

```text
.git/git-deploy/<target>.json
.git/git-deploy/<target>.lock
```

只有所有上传、删除和 `after_deploy` 命令成功后才原子更新 state。中途失败会保留旧 state；重新执行同一条 `git-deploy` 即可覆盖已完成文件并继续收敛。

## 安全边界

- 构建失败时远端连接数、远端写入数和 state 修改数都为零。
- 源码上传固定为 committed `HEAD`，不会读取同路径的脏工作区内容。
- output 在连接前复制并复核 hash，避免计划与上传字节不一致。
- SFTP 先上传临时文件再替换；兼容回退先备份旧目标，替换失败会恢复旧文件。
- Git `100755` 文件通过 SFTP 发布为 `0755`；FTP 无法保证可执行位，因此会在连接前拒绝。
- FTP 只承诺二进制上传、目录创建和幂等文件操作，不声称原子替换或 POSIX 权限语义；同一连接会缓存完整父目录列表，批量删除不会为每个文件重复 `NLST`。
- `after_deploy` 只执行用户审阅的 SFTP Target 命令；工具不识别数据库 Migration、不抽象服务管理器，也不提供服务器备份或通用运维编排。

## 故障恢复

上传失败后直接重跑：

```bash
git-deploy prod --yes
```

源码回滚使用 Git，然后重新部署：

```bash
git revert <bad-commit>
git-deploy prod --yes
```

state 丢失或目标需要覆盖时使用 `--full`。不要让多个发布器同时管理同一远端路径。

Native OpenSSH 若在临时文件上传完成后、正式 rename 前发生连接中断，当前进程会尝试删除已知的 `.git-deploy-<uuid>.tmp`；连接已死亡时该清理可能失败。工具不会扫描或删除未知历史临时文件，以免越过文件所有权边界。跨独立仓库命令的本机物理目标锁取舍见 [ADR](docs/adr-physical-target-lock.md)。

## 开发、测试与发布

```bash
make release-check
```

该门禁依次执行 lock 校验、全部单元/集成测试、Ruff、ty 和 wheel/sdist 构建。测试包含 Fake Transport 编排、本机 FTP、容器化 OpenSSH/SFTP，以及本机可用时的 pnpm、Composer、PHP+Node 真实构建链。

本地隔离安装构建结果：

```bash
uv venv --clear tmp/release-smoke
uv pip install --python tmp/release-smoke/bin/python dist/git_deploy-1.3.0-py3-none-any.whl
tmp/release-smoke/bin/git-deploy --version
tmp/release-smoke/bin/git-deploy --help
```

WSL、1Password、OpenSSH Config、Windows Hello 人工验收与故障排查见 [Native OpenSSH / WSL 指南](docs/native-openssh-wsl.md)。详细范围和实施记录见 [OpenSSH/Workspace 总方案](docs/git-deploy-v1-lite-audit-workspace-openssh-master-plan.md)。
