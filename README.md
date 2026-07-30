# git-deploy v1-lite

`git-deploy` 是个人日常使用的 Git 感知型构建与文件同步工具：在本地运行构建，比较上次成功部署与当前 `HEAD`，再通过 SFTP 或 FTP 只同步变化的源码和构建产物。

日常部署只有一条命令：

```bash
git-deploy
```

v1-lite 不再提供 v0.3 的 Expected State、Generation、CAS、Transaction、History、Verify、通用 Recover 或 Rollback。`--recover` 只处理一个已审阅的 Hybrid 中断记录：SFTP Staged Hybrid 处理可证明的恢复阶段，FTP In-place Hybrid 只处理已提交 Ownership 后的冻结 State/清理阶段。旧实现冻结在 `legacy/v0.3` 分支和 v0.3.x tags；v1 配置和 state 与旧版不兼容。

## 安装

需要 Python 3.11+ 和 Git。稳定版通过 GitHub Release 分发 wheel：

```bash
uv tool install \
  https://github.com/howjc/git-deploy/releases/download/v1.7.3/git_deploy-1.7.3-py3-none-any.whl
git-deploy --version
```

开发安装：

```bash
uv sync --all-groups
uv tool install --editable .
```

## Shell Tab 补全

安装后会在**首次运行** `git-deploy` 时按 `$SHELL` 自动写入用户级补全（pip / `uv tool`
没有可靠的 post-install hook，因此用首次启动补上）。也可显式安装或重装：

```bash
git-deploy completion install          # 检测 $SHELL，写入脚本 + RC
git-deploy completion install --force  # 强制覆盖
```

默认写入位置：

| Shell | 补全脚本 | RC 片段 |
|-------|----------|---------|
| bash | `~/.local/share/bash-completion/completions/git-deploy` | `~/.bashrc` 中带标记的 `source` |
| zsh | `~/.local/share/zsh/site-functions/_git-deploy` | `~/.zshrc` 中 `fpath` + `source` |

RC 片段用 `# >>> git-deploy shell completion >>>` 标记，可重复执行、不重复追加。
装好后**重新打开终端**（或 `source ~/.bashrc`），再试 `git-deploy <Tab>`。

跳过自动安装：设置环境变量 `GIT_DEPLOY_SKIP_COMPLETION_INSTALL=1`。

### 可选：argcomplete 动态补全

依赖已包含 `argcomplete`。若更想用 Python 侧动态补全，可额外：

```bash
eval "$(register-python-argcomplete git-deploy)"
```

### 调试 / 手工加载

```bash
git-deploy completion bash      # 打印 bash 脚本
git-deploy completion zsh       # 打印 zsh 脚本
git-deploy completion targets   # 列出当前可补全 target（只读本地 TOML）
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

以下保护规则始终生效，不能被配置移除：`.env`、`.env.*`、`.git-deploy/**`、`uploads/**`、`runtime/**`、`storage/cert/**`、`**/*.key`、`**/*.pem`。默认也排除 `.git/**`、`.deploy/**`、`.git-deploy/**`、`node_modules/**` 和 `storage/logs/**`。

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

## Hybrid Output

Hybrid 用于把一个本地聚合视图安全部署到包含 `index.php`、`.env`、后端目录和未知内容的 SFTP 或 FTP 混合根目录。先在 `.gitignore` 增加 `.deploy/`，再让可信 Build 脚本生成最终视图：

```text
.deploy/frontend-root/
├── index.html
├── favicon.ico
├── assets/
├── images/
└── fonts/
```

该项目的 `deploy.toml` 使用唯一 Hybrid Mapping；backend 根据部署时选中的 Target 协议自动确定：

```toml
project_id = "github.com/example/project"

[build]
steps = ["python examples/aggregate_frontend_builds.py"]

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/www/wwwroot/project"
```

聚合根的直接文件是 Root File；直接目录是完整 Mirror Directory，并保留嵌套空目录，确保目录内没有远端孤儿。删除只来自 `.git-deploy/hybrid/frontend-root.json` 声明的历史所有权，本地 State 丢失也不影响删除；未声明的 `index.php`、`.env`、后端、运行时和未知路径永远不处理。SFTP Root File 按 Hash 增量；FTP Hybrid 的 Root File 与 Mirror 内文件均按 Local State Hash、远端是否仍存在与 Pending/`--full` 决定是否上传，不把远端 Size/Modify 当作内容证明。SFTP Mirror Directory 仍整目录 Stage/Swap。

SFTP 使用现有 SFTP Staged Hybrid：目录 Stage/Backup/Swap，中断后先审阅并显式 `--recover`。FTP 使用 FTP In-place Hybrid：所有当前文件先上传到 `.git-deploy/ftp-hybrid/stage/`、RETR SHA256 校验，再逐文件 Rename Replace（发布后不再整文件回读；依赖 Stage 校验、已 Probe 的 Rename 与 Stage 消费检查）。Stage/Publish 可用 `deploy.ftp_connections`（默认 1 串行，1–16 显式并行）并行多条 FTP 控制会话以降低小文件 RTT；元数据与 Prune 仍串行。全部发布成功后才删除 Mirror 孤儿和历史受管路径；Pending/Ownership 等元数据仍 Stage+Final 双校验。最后提交 Ownership 与 Local State。FTP 通过 `.git-deploy/ftp-hybrid/pending/` 提供 Forward Resume，不提供目录原子 Swap、旧树回滚或 `after_deploy`。

FTP Hybrid 首次使用前必须初始化 Capability Profile。推荐对 Project / Workspace 一次性完成：

```bash
git-deploy bootstrap
# CI / 非交互环境
git-deploy bootstrap --yes
# 强制重新探测
git-deploy bootstrap --force --yes
# 只处理指定 Target；缺失 Root 时拒绝创建
git-deploy bootstrap prod staging --no-create-root --yes
```

`bootstrap` 枚举全部 FTP Hybrid Target，做只读 Preflight，输出统一 Plan，批次确认一次后顺序 Probe（可创建缺失配置 Root），一个失败继续下一个，最后 Summary；不 Build、不上传业务文件、不写 Ownership/Pending/State。未知 Target 过滤名在连接前失败；非 Git 工作区不会创建伪 `.git`。Create Root 成功但 Probe 失败时远端 Root 保留，可重跑继续。仍可用单 Target Doctor：

```bash
git-deploy doctor prod --probe-ftp-hybrid
git-deploy doctor prod --probe-ftp-hybrid --yes
```

Probe 在创建 `.git-deploy/ftp-probe/<随机 ID>` 前先以单层 MLSD 拒绝 `.GIT-DEPLOY` 等未知别名；本地 Capability Profile Schema 3 绑定 Target Fingerprint 与 Server Banner。服务器必须广告 UTF8；客户端会尝试 `OPTS UTF8 ON`，仅当 OPTS 返回明确的 500/501/502/504 时按 Pure-FTPd always-on UTF-8 兼容处理，其它永久错误 Fail Closed。还需可靠支持中文名、NFC/NFD 精确名称、大小写敏感路径、FEAT/MLSD、Binary STOR/RETR、跨目录 Rename、Rename Replace、DELE 和 RMD；一旦启用 UTF-8，同一 Transport 的每次重连都重新核对 Banner、FEAT 并再次协商 UTF-8。Banner 哈希会脱敏会话易变字段（用户数、本地时间）并保留稳定后缀，空身份材料拒绝。旧 Profile Schema 1/2 必须重新 Probe，不能无证据迁移。

无 Ownership Manifest 且当前同名路径已存在时，普通部署会拒绝。先人工审阅，再用 `--full` 只接管当前本地存在的同名路径；未知路径不会被 Adoption。Hybrid 固定要求 `remote = "."`、单 Mapping，并禁止显式 `delete_removed`、本地 Local Root 等于项目根目录、本地/远端符号链接和隐式所有权转移。路径组件必须可稳定枚举：拒绝首尾空格、Tab、控制字符和不可见空白；直接 `.git`、`.deploy`、`.git-deploy` 永远拒绝。FTP Hybrid 会在本地及远端写前统一检查 Source、Incremental、Hybrid Direct、历史受管根和内部 `.git-deploy`。远端仅枚举 Root Direct Entries：精确同名继续既有 Adoption/Ownership 规则，与受管根 NFC + casefold 等价但拼写不同的未知项 Fail Closed，无关未知根保持不读、不改、不删。

FTP In-place Hybrid 还要求单发布器：部署期间不得由 CI、另一台机器、面板或手工 FTP 修改受管路径。FTP Rename Replace 无法保证 Planned-Missing 路径最后一刻不覆盖。受管直接路径的 File→Directory / Directory→File 会被拒绝；先从聚合视图移除旧类型并完成一次删除部署，再添加新类型并用 `--full` 审阅。

`examples/aggregate_frontend_builds.py` 展示显式 Sources/Destination、重复路径与文件/目录冲突检测、符号链接拒绝和本地原子替换。完整迁移步骤见 [Hybrid 迁移指南](docs/migrate-to-hybrid-output.md)，架构边界见 [Hybrid ADR](docs/adr/hybrid-output.md)。

## After-deploy 命令

SFTP Target 可配置最多 16 条单行 `after_deploy` 命令。它们在文件上传/删除全部成功后，按声明顺序在 `remote_root` 下执行，全部成功后才提交 State。Single Plan 与 Workspace Combined Plan 都会显示完整命令，`--dry-run` 只供审阅，不会连接远端。

命令默认 `command_timeout = 120` 秒、无 PTY、无 stdin，也不会自动重试；应使用非交互命令并尽量保持幂等，且不得修改 Hybrid 管理路径。普通输出的命令失败可重跑部署；Hybrid 已提交 Ownership 后的失败会保留待恢复阶段，必须先审阅并显式运行 `--recover`，命令按至少一次语义继续。没有文件变化时不会连接，也不会执行 `after_deploy`；Hybrid 含任一 Mirror Directory 时每次都有远端工作，因此命令也会执行。

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
git-deploy prod --remote-plan
git-deploy prod --recover
git-deploy prod --skip-build --yes
git-deploy prod --full --yes
```

`--dry-run` 默认仍执行构建，但不连接服务器、不写 State；FTP Hybrid 会提示使用 `--remote-plan` 获取精确孤儿列表。`--remote-plan` 与它互斥：同样先 Build/Freeze，但只读 Capability Profile、Ownership、Pending、MLSD 受管树和当前路径类型，完整显示 Adoption/Upload/Delete/RMD Plan，绝不 Probe、上传、删除、执行命令或写远端 Manifest/本地 State。FTP Pending Schema 2 的 `PREPARED`、`FILES_PUBLISHED`、`PRUNED` 仅在 Local Manifest、HEAD、Previous State、Source/Incremental Operation 与配置策略全部匹配后向前收敛；`FILES_PUBLISHED` 不重放普通上传/删除。`OWNERSHIP_COMMITTED`、`STATE_COMPLETE` 使用独立 `--recover`，普通部署会在 Build/Plan 前拒绝；恢复不 Build、不读当前 State、不扫描本地 Hybrid，并始终保存 Marker 中冻结的 State 后再清理 Stage/Marker。`--full` 上传全部当前受管内容；Hybrid 中还显式允许接管当前同名路径，但仍不清空根目录或处理未知内容。

### 传输速率与汇总

上传时会显示当前文件进度、IEC 字节单位和 1.5 秒滑动窗口速率；TTY 最多每 250ms 刷新一次，重定向到日志时只保留文件完成行和最终汇总，不产生逐 Block 日志。`--verbose` 也遵守相同节流。

```text
UPLOAD public/assets/app.js  63%  3.8 MiB / 6.0 MiB  2.42 MiB/s
UPLOAD public/assets/app.js 100%  6.0 MiB  avg 2.36 MiB/s
TRANSFER SUMMARY
  files:          1
  payload:        6.0 MiB
  attempt bytes:  7.2 MiB
  active time:    3.05s
  average upload: 2.36 MiB/s (19.8 Mbps)
  retries:        1
```

`payload` 是按最终路径去重后的成功逻辑文件大小；Streaming 后端的 `attempt bytes` 是 Transport Callback 报告的应用层上传字节，包含已回调的失败部分上传和重传，但不包含协议头、TCP 重传或失败 Block 中尚未回调的字节。FTP 与 Paramiko 在父目录准备完成、即将上传时才启动 `active time`；其 `average upload` 不包含 Parent MKD、Build、Freeze、Plan、FTP RETR、Rename、Delete/RMD、远端命令或重试等待。

Native OpenSSH 的 Batch CLI 不提供稳定的分块进度，因此诚实显示为粗粒度发布测量：

```text
UPLOAD public/assets/app.js transferring (Native batch)
UPLOAD public/assets/app.js 100%  6.0 MiB  avg publish 2.36 MiB/s (coarse)
TRANSFER SUMMARY
  measurement:    coarse Native batch
  reported bytes: >= 6.0 MiB
  average publish: 2.36 MiB/s (coarse)
  note:           failed partial bytes may be unreported
```

Native 的 Active Time 覆盖整个 Batch/Publish 区间，不能解释为实时网络速率；失败 Attempt 在首尾 Callback 之间发送的部分字节可能不可见。所有进度和 Summary 输出均为 Fail-open：关闭的 Pipe、Stream I/O 或控制台编码错误只会禁用后续显示，不会触发上传 Retry、改变 State/Recovery 或把成功部署报告为失败。Streaming 样本小于 1 MiB 或 1 秒时会标记 `sample too small`；Delete-only/No-op 不显示 Summary，失败部署不显示成功 Summary。Workspace 为每个 Repository 独立汇总。

只构建、诊断或远端初始化：

```bash
git-deploy build
git-deploy doctor
git-deploy doctor prod
git-deploy doctor prod --create-root
git-deploy doctor prod --probe-ftp-hybrid
git-deploy bootstrap --yes
```

`doctor` 默认只读检查配置、Git、构建命令、Output、State、连接和远端根目录；Hybrid 还报告 `.deploy` Ignore、Local Root、Project ID、Ownership、Recovery、内部目录、Owned Path Type 与 Adoption；FTP 额外报告 Pending/Ownership 阶段矩阵和不自动删除的 Orphan Stage。只有 `--create-root` 才允许创建缺失 Root。Native OpenSSH Doctor 会显示 backend、系统命令绝对路径、Alias 和解析后的 Endpoint，并提示认证可能触发当前 SSH Agent。

`bootstrap` 负责 FTP Hybrid 远端运行时初始化（Capability Profile / 可选 Root），`init` 只负责本地配置模板。两者不合并。

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
git-deploy prod --remote-plan
git-deploy prod --yes
git-deploy doctor prod
git-deploy build
git-deploy build prod  # 可选：只校验每仓都存在 prod 这个名称
```

部署前会先完成所有仓库的 Target 校验；非 dry-run 的 FTP Hybrid 会先对全部仓库只读检查后提交 Pending，发现后统一提示 `--recover`，不会启动任何 Build/Plan。随后才执行全部 Build、Local Plan 和上传字节冻结；Hybrid 普通部署再只读完成所有 Remote Ownership Plan，显示包含 Local/Remote Hybrid、Adoption、文件、命令和 Bytes 的 Combined Plan，只确认一次。每仓按“普通文件 → Hybrid → Ownership → 命令 → State → Cleanup”完成后才进入下一仓；相同 Native OpenSSH Endpoint 共用当前命令生命周期内的一条 ControlMaster。

Workspace 的独立 `build` 命令是纯本地操作：只加载各仓配置并顺序执行 Build，不解析 SSH Alias、不要求 `ssh`/`sftp`、不检查 Git 或远端 Root 所有权。显式附带 Target 时只检查该名称是否在每仓配置中存在。

所有仓库的物理 Endpoint 和 Remote Root 会在首个 Build 前解析；同一 Endpoint 上相同或父子嵌套的 Root 会直接拒绝，避免两个独立 State 相互覆盖。Combined Plan 会展示每仓冻结后的 Host/User/Port、Remote Root、模式、Commit 边界和冻结字节总量。

如果当前目录同时有 `deploy.toml` 与 `deploy.workspace.toml`，必须用 `--config` 或 `--workspace` 显式选择。Workspace 不提供并行、依赖图、Target Map、共享 State、全局事务或自动回滚；中途失败后直接重跑。普通 Incremental 仓库会自然成为 No-op；含 Mirror Directory 的 Hybrid 按强一致语义仍会再次 Mirror。

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

Incremental Outputs 在构建后扫描并计算 SHA256。只有上次 State 已记录、当前已删除且对应 `delete_removed = true` 的产物才会触发远端删除。Hybrid Root File 的 Hash 也进入本地 State，但删除所有权来自受身份约束的 Remote Ownership Manifest；工具永远不会扫描并清理未知远端内容。Root File 的“无需上传”仍信任上次成功写入的本地 State Hash：若有人在部署器之外修改同名远端文件，日常增量部署不会做远端 Hash 校验，应使用 `--full` 重新发布受管内容。

每个 target 的 state 和进程锁保存在 Git Common Dir，多个 linked worktree 共享：

```text
.git/git-deploy/<target>.json
.git/git-deploy/<target>.lock
```

普通部署只有所有上传、删除和 `after_deploy` 命令成功后才原子更新 State。Hybrid 固定为普通文件 → Root File/Directory Stage+Swap/Delete → Remote Ownership → `after_deploy` → Local State → Backup Cleanup；Ownership 表示远端实际事实，因此命令失败时它已生效而本地 State 仍旧，重跑会继续收敛且命令可能重复。

## 安全边界

- 构建失败时远端连接数、远端写入数和 state 修改数都为零。
- 源码上传固定为 committed `HEAD`，不会读取同路径的脏工作区内容。
- output 在连接前复制并复核 hash，避免计划与上传字节不一致。
- Hybrid Local Root 在连接前完整扫描/冻结；Build、聚合、冲突或符号链接失败时远端连接数为零。
- Hybrid 只触碰当前/历史 Ownership 明确声明的直接子项和受保护的 `.git-deploy` 内部路径；未知远端内容不进入候选集合。
- Hybrid 确认后的任何写入前会复核 Ownership 原始字节 Hash 和当前/历史受管路径类型；确认窗口发生漂移会以 Stale Plan 失败，远端写入为零。Workspace 会先复核全部选中仓库，后一仓 Stale 也不会让前一仓先写入。
- 所有 Root File 和 Mirror Directory 完整上传到 Stage 后、开始线上 Swap 前，会再次复核 Ownership、Recovery 和路径类型；上传重试/重连也必须先通过这道门禁。每条路径在 Backup 前还会核对计划类型，计划时 Missing 的目标以 No-overwrite Rename 发布；最后一刻出现同名路径会 Stale 并保留外部内容。
- 写入新 Ownership 前会再次核对旧 Ownership Hash。SFTP Rename 之间仍有短暂切换窗口，不宣称零停机、远端租约或发布事务。
- SFTP 先上传临时文件再替换；兼容回退先备份旧目标，替换失败会恢复旧文件。
- Git `100755` 文件通过 SFTP 发布为 `0755`；FTP 无法保证可执行位，因此会在连接前拒绝。
- FTP 只承诺二进制上传、目录创建和幂等文件操作，不声称原子替换或 POSIX 权限语义；同一连接会缓存完整父目录列表，批量删除不会为每个文件重复 `NLST`。
- `after_deploy` 只执行用户审阅的 SFTP Target 命令；工具不识别数据库 Migration、不抽象服务管理器，也不提供服务器备份或通用运维编排。

## 故障恢复

普通上传失败后直接重跑：

```bash
git-deploy prod --yes
```

源码回滚使用 Git，然后重新部署：

```bash
git revert <bad-commit>
git-deploy prod --yes
```

State 损坏或目标需要覆盖时，审阅计划后使用 `--full`。Hybrid 仅丢失本地 State 时仍以 Remote Ownership 为删除事实来源，不需要为了恢复所有权而接管未知路径。不要让多个发布器同时管理同一远端路径。

Hybrid Swap 中断时保留 `.git-deploy/recovery/<id>.json`：Ownership Commit 前恢复 Backup，Commit 后按阶段继续待执行命令、保存保守 State 或清理；必要 Backup 缺失、新旧 Ownership Hash 无法证明时 Fail Closed 并保留现场，Doctor 报告需要人工检查。普通部署和 `--remote-plan` 都只报告 Recovery，不会暗中修复。v1.4.0 写入的 schema-1 记录若尚未提交 Ownership，仍可保守恢复；若 Ownership 已提交且命令待执行，因为无法证明原命令契约，会 Fail Closed 并由 Doctor 提示 `Legacy Command Contract Unknown`，不得自动执行当前配置命令。

先只读审阅，再显式恢复；恢复完成后重新生成普通计划：

```bash
git-deploy prod --remote-plan
git-deploy prod --recover       # 仍会要求确认；可配 --yes
git-deploy prod --remote-plan   # 重新读取 Ownership 与路径类型
git-deploy prod --yes
```

`--recover` 与 `--dry-run`、`--remote-plan`、`--full` 互斥，并且一次只处理当前配置中的一个可证明记录。它不是历史或自动回滚系统。

Native OpenSSH 若在临时文件上传完成后、正式 rename 前发生连接中断，当前进程会尝试删除已知的 `.git-deploy-<uuid>.tmp`；连接已死亡时该清理可能失败。工具不会扫描或删除未知历史临时文件，以免越过文件所有权边界。跨独立仓库命令的本机物理目标锁取舍见 [ADR](docs/adr/physical-target-lock.md)。

## 开发、测试与发布

```bash
make release-check
```

该门禁依次执行 lock 校验、全部单元/集成测试、Ruff、ty 和 wheel/sdist 构建。测试包含 Fake Transport 编排、本机 FTP、容器化 OpenSSH/SFTP，以及本机可用时的 pnpm、Composer、PHP+Node 真实构建链。

本地隔离安装构建结果：

```bash
uv venv --clear tmp/release-smoke
uv pip install --python tmp/release-smoke/bin/python dist/git_deploy-1.6.3-py3-none-any.whl
tmp/release-smoke/bin/git-deploy --version
tmp/release-smoke/bin/git-deploy --help
```

WSL、1Password、OpenSSH Config、Windows Hello 人工验收与故障排查见 [Native OpenSSH / WSL 指南](docs/native-openssh-wsl.md)。更多文档（产品边界、迁移、ADR、版本说明）见 [docs/](docs/README.md)。

## 许可证

本项目以 [MIT License](LICENSE) 发布。

运行时依赖 [paramiko](https://github.com/paramiko/paramiko)（LGPL-2.1）由 pip/uv 单独安装，未捆绑进本仓库的源码分发物。使用、修改或再分发时请同时遵守各依赖自身的许可证条款。
