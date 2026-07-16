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
  https://github.com/howjc/git-deploy/releases/download/v1.1.0/git_deploy-1.1.0-py3-none-any.whl
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

相对路径以 `deploy.toml` 所在目录（即项目根目录）解析。`build.steps` 是可信配置，会直接在当前 Shell 环境中执行，不是沙箱。

以下保护规则始终生效，不能被配置移除：`.env`、`.env.*`、`uploads/**`、`runtime/**`、`storage/cert/**`、`**/*.key`、`**/*.pem`。默认也排除 `.git/**`、`node_modules/**` 和 `storage/logs/**`。

配置 `ssh_host_alias` 时自动使用当前环境的 Native OpenSSH：

- 完整读取 OpenSSH Config、Include、ProxyJump 和 ProxyCommand；
- 继承当前 `SSH_AUTH_SOCK`，兼容 WSL 中的 1Password SSH Agent；
- 使用临时 ControlMaster，多文件只建立一次认证连接；
- 不读取私钥、不管理 Agent Socket、不启用 Agent Forwarding；
- 只调用当前环境的 POSIX `ssh`/`sftp`，不会回退 Windows `ssh.exe`。

Alias Target 只配置 `ssh_host_alias`、可选 `ssh_config_file` 和 `remote_root`；不要混入 `host`、`username`、`port`、`key_file` 或 `password_env`。只配置 `host`/`username` 的 SFTP Target 继续使用 Paramiko，支持 `key_file`、`password_env`、`known_hosts_file` 和 SSH Agent。

FTP 必须通过 `password_env` 读取密码，不接受 TOML 明文密码。v1-lite 不支持 FTPS、远端命令、owner/group 或远端健康检查。

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

只有所有上传和删除成功后才原子更新 state。中途失败会保留旧 state；重新执行同一条 `git-deploy` 即可覆盖已完成文件并继续收敛。

## 安全边界

- 构建失败时远端连接数、远端写入数和 state 修改数都为零。
- 源码上传固定为 committed `HEAD`，不会读取同路径的脏工作区内容。
- output 在连接前复制并复核 hash，避免计划与上传字节不一致。
- SFTP 先上传临时文件再替换；兼容回退先备份旧目标，替换失败会恢复旧文件。
- Git `100755` 文件通过 SFTP 发布为 `0755`；FTP 无法保证可执行位，因此会在连接前拒绝。
- FTP 只承诺二进制上传、目录创建和幂等文件操作，不声称原子替换或 POSIX 权限语义。
- 本工具只同步文件；数据库、消息队列、缓存刷新、进程重启和服务器备份不在职责内。

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

## 开发、测试与发布

```bash
make release-check
```

该门禁依次执行 lock 校验、全部单元/集成测试、Ruff、ty 和 wheel/sdist 构建。测试包含 Fake Transport 编排、本机 FTP、容器化 OpenSSH/SFTP，以及本机可用时的 pnpm、Composer、PHP+Node 真实构建链。

本地隔离安装构建结果：

```bash
uv venv --clear tmp/release-smoke
uv pip install --python tmp/release-smoke/bin/python dist/git_deploy-1.1.0-py3-none-any.whl
tmp/release-smoke/bin/git-deploy --version
tmp/release-smoke/bin/git-deploy --help
```

WSL、1Password、OpenSSH Config、Windows Hello 人工验收与故障排查见 [Native OpenSSH / WSL 指南](docs/native-openssh-wsl.md)。详细范围和实施记录见 [OpenSSH/Workspace 总方案](docs/git-deploy-v1-lite-audit-workspace-openssh-master-plan.md)。
