# git-deploy

`git-deploy` 是给个人和小团队使用的 Git 增量发布工具。它从可信的本地
current state 自动计算到当前 `HEAD` 的变化，通过 SFTP、FTP 或 FTPS 发布，
并在修改远端前保存真实备份。日常发布不需要复制 commit range，也不需要输入
冗长确认短语。

它适合仍通过 SSH/FTP 发布普通目录、希望保留清晰 Git 来源和最新版本回滚能力
的项目。它不是 CI 平台、服务器面板或数据库 migration 系统；v0.3.2 也不提供
TUI、历史版本派生回滚或自动 GC。

## 安装

需要 Python 3.11+ 和 Git。推荐直接安装当前安全版本 v0.3.2 wheel（**不要**再安装
已知存在阻断问题的 v0.3.0）：

```bash
uv tool install \
  https://github.com/howjc/git-deploy/releases/download/v0.3.2/git_deploy-0.3.2-py3-none-any.whl
git-deploy --version
```

开发安装：

```bash
uv sync
uv tool install --editable .
```

Docker 仅在使用 Docker build runner 时需要；1Password CLI 仅在显式配置
secret-enabled build 时需要。远端不需要安装 Python 或 git-deploy。

## 最小配置

在项目根目录创建不提交到 Git 的 `deploy.toml`：

```toml
[server]
protocol = "sftp"
host = "app.example.com"
username = "deploy"
strict_host_key_checking = true

[projects.application]
repository = "."
remote_root = "/srv/application"
include = ["**"]
exclude = ["runtime/**", "uploads/**"]
protected = [".env", "**/*.key", "storage/cert/**"]
```

多环境项目可配置 `[remotes.dev]`、`[remotes.prod]` 和
`default_remote = "dev"`；生产 remote 建议设置 `risk = "production"`。

## 首次建立可信 state

先运行本地诊断：

```bash
git-deploy doctor application
```

如果远端当前内容对应某个已知 Git commit：

```bash
git-deploy state bootstrap application --revision COMMIT --yes
```

只有在确认所有受管远端路径都不存在时，才使用空基线：

```bash
git-deploy state bootstrap application --empty --yes
```

bootstrap 是一次显式 mutation。不要为了绕过检查而删除 state 文件或 lock。

## 日常发布

建立 current 后，最短流程就是：

```bash
git-deploy plan application
git-deploy deploy application --dry-run
git-deploy deploy application --yes
```

未传 `--revisions` 时，工具按每个项目自己的可信 current 选择缺失提交直到当时的
`HEAD`。plan 会把 `HEAD` 冻结为完整 commit hash；正式 deploy 复用相同 plan 并在
执行前复核 target identity 和 generation。已经部署到 HEAD 时返回 0，并显示
`No changes`，不会连接远端或新建 transaction/manifest。

需要部署显式提交或范围时仍可使用：

```bash
git-deploy deploy application --revisions COMMIT_A..HEAD --yes
```

history 记录的是 plan 当时解析出的完整 HEAD commit hash，而不是易移动的指针。

## Doctor 和远端核对

`doctor` 默认只读本地配置、Git 和 state，不连接服务器：

```bash
git-deploy doctor application
git-deploy doctor all --remote dev
git-deploy doctor application --remote prod --check-remote
```

`--check-remote` 只做远端读取/目录检查。完整路径核对使用：

```bash
git-deploy state verify application --check-remote
```

## 最新版本回滚

回滚默认选择最新成功 deployment，不再要求 `--latest`：

```bash
git-deploy rollback application --dry-run
git-deploy rollback application --yes
```

v0.3.0 只自动回滚最新 deployment。backup、current lineage 或远端状态无法证明时，
工具会停止并保留 transaction evidence，不会猜测。

## 深入文档

- [v0.3 简化设计](docs/planning/2026-07-14-git-deploy-v0.3-simplified-northstar.md)
- [application contract](docs/application-contract-v0.3.md)
- [从手工 FTP 发布迁移](docs/migrate-from-manual-ftp.md)
- [故障恢复手册](docs/recovery-playbook.md)
- [target lock 审计](docs/audit/v0.3-target-lock-audit.md)

> `deploy.py` 是旧版全量上传入口；新 CLI 不读取旧版
> `deploy.example.toml`。

## 配置指南

### 配置文件发现顺序

1. 顶层参数 `--config PATH`。
2. 当前目录的 `./deploy.toml`。
3. 环境变量 `GIT_DEPLOY_CONFIG`。
4. `~/.config/git-deploy/deploy.toml`。

工具不会向父目录递归搜索。配置中的相对路径以配置文件所在目录为基准。`--config` 必须写在子命令之前：

```bash
git-deploy --config /path/to/deploy.toml plan application --revisions HEAD
```

`deploy.toml` 可能包含服务器名称、路径和凭据变量名，默认应保持未跟踪。

### 单远程配置

```toml
[server]
protocol = "sftp"
host = "192.0.2.10"
port = 22
username = "deploy"
ssh_host_alias = "application-prod"
ssh_config_file = "~/.ssh/config"
strict_host_key_checking = true
timeout = 15
owner = "www-data"
group = "www-data"
file_mode = "0644"
executable_mode = "0755"
directory_mode = "0755"

[projects.application]
repository = "."
remote_root = "/srv/application"
include = ["src/**", "public/**", "config/**"]
exclude = ["tests/**", "docs/**", "runtime/**", "tmp/**"]
protected = [".env", "runtime/**", "storage/cert/**"]
post_commands = ["cd /srv/application && ./bin/clear-cache"]
health_urls = ["https://example.invalid/health"]
```

FTP/FTPS 推荐用 `password_env`，不要在 TOML 中写明文密码：

```toml
[server]
protocol = "ftps"
host = "ftp.example.invalid"
username = "deploy"
password_env = "GIT_DEPLOY_FTP_PASSWORD"
```

### 多远程与默认环境

```toml
default_remote = "dev"

[remotes.dev]
protocol = "sftp"
host = "dev.example.invalid"
username = "deploy-dev"
strict_host_key_checking = true
owner = "www-data"
group = "www-data"
file_mode = "0644"
executable_mode = "0755"
directory_mode = "0755"

[remotes.prod]
protocol = "sftp"
host = "prod.example.invalid"
username = "deploy-prod"
strict_host_key_checking = true
owner = "www-data"
group = "www-data"
file_mode = "0644"
executable_mode = "0755"
directory_mode = "0755"

[projects.application]
repository = "."
include = ["src/**", "public/**", "config/**"]
exclude = ["tests/**", "docs/**", "runtime/**", "tmp/**"]
protected = [".env", "runtime/**", "storage/cert/**"]

[projects.application.remotes.dev]
remote_root = "/srv/dev/application"
post_commands = ["cd /srv/dev/application && ./bin/clear-cache"]
health_urls = ["https://dev.example.invalid/health"]

[projects.application.remotes.prod]
remote_root = "/srv/application"
post_commands = ["cd /srv/application && ./bin/clear-cache"]
health_urls = ["https://example.invalid/health"]
```

- 未配置 `default_remote` 且存在多个 remote 时，所有命令必须显式传 `--remote`。
- 建议默认指向 `dev`，不要默认指向生产。
- `remote_root`、`post_commands`、`health_urls` 未覆盖时继承项目级配置。
- remote 层的 `build` 和 `artifacts` 是整体替换，不进行深合并。
- 相同 canonical protocol/host/port/project/root 的 alias 共享 target、state 和 lock；不同目标隔离。
- 显式 `target_id` 不能跨不同 physical payload 复用。

### SFTP 所属者与权限

SFTP remote 可以声明部署文件和新建目录的 POSIX ownership/mode：

```toml
[remotes.prod]
protocol = "sftp"
host = "prod.example.invalid"
username = "root"
owner = "www-data"
group = "www-data"
file_mode = "0644"
executable_mode = "0755"
directory_mode = "0755"
```

- 默认普通文件为 `0644`、Git 可执行文件为 `0755`、新建目录为 `0755`。
- `owner`、`group` 接受安全的用户/组名称或数字 UID/GID；两者均可单独配置。
- mode 推荐写成字符串（如 `"0644"`），也可用 TOML 八进制整数（如 `0o644`）；十进制 `644` 会被拒绝，避免误设权限。
- 文件先在临时路径完成 chmod 和 chown/chgrp，再执行原子替换；设置失败会终止部署，不会把错误 ownership 的临时文件发布为目标文件。
- ownership 设置需要登录账号具备对应权限；以 `root` 登录并希望由 Web 用户运行时，必须显式配置 `owner`/`group`。省略时保留 SFTP 登录用户 ownership。
- 只调整本次新建的目录，不递归修改既有目录或远端根目录；既有 `remote_root` 应在首次部署前正确预置。
- FTP/FTPS 无法可靠保证 POSIX ownership/mode，因此配置这些字段会直接报错。

### 项目字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `repository` | 路径 | Git 仓库；相对配置目录解析 |
| `remote_root` | POSIX 绝对路径 | 项目远端根目录 |
| `include` | glob 数组 | 纳入管理的路径 |
| `exclude` | glob 数组 | 从受管集合排除的路径 |
| `protected` | glob 数组 | 禁止部署或删除的敏感路径 |
| `post_commands` | 字符串数组 | 文件变更后的远端命令 |
| `health_urls` | URL 数组 | 部署后的健康检查 |
| `local_state_dir` | 路径 | 可选的本地 state 基目录 |
| `target_id` | 字符串 | 可选的稳定 physical target ID |
| `build` | table | Host/Docker 构建配置 |
| `artifacts` | table 数组 | artifact source→destination 映射 |
| `remotes.NAME` | table | 指定环境的项目覆盖 |

### Host 构建产物

```toml
[projects.application.build]
runner = "host"
commands = [
  ["npm", "ci", "--offline"],
  ["npm", "run", "build"],
]
timeout = 600
cwd = "."
env_allowlist = ["NODE_ENV"]

[[projects.application.artifacts]]
source = "dist"
destination = "public/dist"
kind = "tree"
```

`kind` 支持 `file` / `tree`。Host runner 不经过 shell，但拥有当前用户可访问的 filesystem/network 权限；隔离 worktree 不是操作系统沙箱。

### Docker 与 1Password 构建

```toml
[projects.application.remotes.prod.build]
runner = "docker"
commands = [
  ["composer", "install", "--no-dev", "--classmap-authoritative", "--no-interaction"],
]
timeout = 900
env_allowlist = ["COMPOSER_AUTH"]

[projects.application.remotes.prod.build.docker]
image = "composer@sha256:0000000000000000000000000000000000000000000000000000000000000000"
platform = "linux/amd64"
network = "none"
pull_policy = "never"

[projects.application.remotes.prod.build.onepassword.env]
COMPOSER_AUTH = "op://build/composer/auth"

[[projects.application.remotes.prod.artifacts]]
source = "vendor"
destination = "vendor"
kind = "tree"
```

请替换示例 digest 和 `op://` URI。规则如下：

- `runner` 只支持 `host` / `docker`；Docker 不可用时不会回退 Host。
- `network` 支持 `none` / `bridge`；`pull_policy` 支持 `never` / `missing`。
- 建议配置不可变 image digest，解析后的 image ID 会进入 build fingerprint。
- 1Password 变量名必须同时出现在 `env_allowlist`，且不能以 `OP_` 开头。
- 工具使用固定 `op run --`，不使用 `op read`、`op inject` 或 `--no-masking`。
- 启用 1Password 后绕过 artifact cache，secret rotation 会重新构建。
- Docker daemon 管理员可读取存活容器环境变量；Docker secret build 必须信任本地 daemon。
- git-deploy 不扫描 artifact 内容，不能阻止构建脚本把秘密写入产物，不提供 DLP 保证。

### 1Password SSH Agent

```sshconfig
Host application-prod
    HostName 192.0.2.10
    User deploy
    IdentityFile ~/.ssh/1password/application-prod.pub
    IdentitiesOnly yes
```

```toml
[remotes.prod]
protocol = "sftp"
host = "192.0.2.10"
ssh_host_alias = "application-prod"
ssh_config_file = "~/.ssh/config"
strict_host_key_checking = true
```

私钥保留在 Agent 中；TOML 中的规范 host 用于 target identity，alias 用于连接配置。

## Revision 选择规则

| 写法 | 含义 |
|---|---|
| `COMMIT` | 选择该提交相对 first parent 的变化；根提交相对空 tree |
| `FROM..TO` | 选择 FROM 之后到 TO 为止的 first-parent 提交 |
| `COMMIT_1 COMMIT_3` | 组合多个非连续提交 |
| `COMMIT_1 RANGE COMMIT_9` | 混合提交与连续范围 |
| `COMMIT..HEAD` | 从某提交之后一直部署到当前最新提交 |

- selector 会去重并按 Git 历史顺序应用，而不是命令行顺序。
- 所有 selector 必须属于同一 first-parent 历史；merge commit 按 first parent 解释。
- 最老 selector 的父提交是 legacy 基线；已有 current state 时以可信 current tree 为基线。
- 省略提交导致 patch 无法干净应用时，会在连接远端前失败。
- 旧参数 `--from`、`--to`、`--range` 已移除。

## 完整使用指南

### 1. 预览和只读检查

```bash
git-deploy plan application \
  --revisions COMMIT_A..COMMIT_B \
  --remote dev

git-deploy plan application \
  --revisions COMMIT_A..COMMIT_B \
  --remote dev \
  --check-remote
```

普通 plan 只读 Git 和本地 state；`--check-remote` 才建立只读远端连接。

### 2. 建立可信 state

远端已与某个已知提交一致：

```bash
git-deploy state bootstrap application \
  --revision CURRENT_COMMIT \
  --remote prod \
  --dry-run

git-deploy state bootstrap application \
  --revision CURRENT_COMMIT \
  --remote prod \
  --yes
```

所有受管 source/artifact destination 都确定为空：

```bash
git-deploy state bootstrap application --empty --remote dev --dry-run
git-deploy state bootstrap application --empty --remote dev --yes
```

bootstrap 执行只读验证远端并写本地 generation 1，不修改远端。`--empty` 仍会验证受管路径不存在。artifact 部署必须先有可信 state。

### 3. Dry-run 与正式部署

```bash
git-deploy deploy application \
  --revisions CURRENT_COMMIT..TARGET_COMMIT \
  --remote prod \
  --dry-run

git-deploy deploy application \
  --revisions CURRENT_COMMIT..TARGET_COMMIT \
  --remote prod \
  --yes
```

普通 dry-run 不创建 worktree、不构建、不调用 Docker/op、不写 state、不连接远端。`--dry-run --check-remote` 增加只读远端验证。不传 `--yes` 时会交互确认。

### 4. 单独构建 artifact

```bash
git-deploy build application \
  --revisions TARGET_COMMIT \
  --remote prod
```

`build` 写本地隔离 worktree/cache，不连接部署远端。

### 5. 多项目部署

```bash
git-deploy deploy all \
  --revisions HEAD~1..HEAD \
  --remote dev \
  --dry-run
```

`all` 对每个仓库独立解析同一 selector。不同项目需要不同 revision 时应分别运行。`rollback all` / `verify all` 使用 `--latest`。

部署计划中的 `HEAD`、`HEAD^`、`HEAD~N` 等表达式会在写入 deployment history 时冻结为当时解析出的完整 commit hash；之后仓库继续提交不会改变历史记录的含义。

### 6. 历史、验证与回滚

```bash
git-deploy history application --remote prod --limit 20

git-deploy verify application \
  --deployment DEPLOYMENT_ID \
  --remote prod

git-deploy rollback application \
  --latest \
  --remote prod \
  --dry-run --check-remote

git-deploy rollback application \
  --latest \
  --remote prod \
  --yes
```

`--deployment` 接受完整 ID 或唯一前缀。当前 stateful 路径只允许回滚最新成功 deployment；非最新回滚会在连接远端前拒绝。回滚恢复文件 bytes/mode 和 state，不回滚数据库或其他外部副作用。

### 7. State 检查与恢复

```bash
git-deploy state inspect application --remote prod
git-deploy state verify application --remote prod
git-deploy state verify application --remote prod --check-remote

git-deploy state recover application --remote prod
git-deploy state recover application --remote prod --execute --yes
```

`inspect` 和默认 `state verify` 纯本地只读。recover 默认显示决策；`--execute --yes` 才执行可证明安全的恢复。第三种远端内容进入人工恢复状态，不会被覆盖。

### 8. 历史与 policy 迁移

```bash
# legacy history：plan → staging → publish
git-deploy state migrate application --remote dev
git-deploy state migrate application --remote dev --stage
git-deploy state migrate application --remote dev --yes

# managed policy：plan → read-only verify + local CAS
git-deploy state policy-migrate application --remote prod
git-deploy state policy-migrate application --remote prod --execute --yes
```

历史迁移保留 legacy 证据。policy migration 的远端写调用为 0。

## 命令说明表

### 顶层命令

| 命令 | 用途 | 默认副作用 | 关键参数 |
|---|---|---|---|
| `plan TARGETS...` | 生成 revision 计划 | 本地只读；check 时远端只读 | `--revisions`、`--remote`、`--check-remote`、`--force` |
| `deploy TARGETS...` | 部署 source/artifact | 正式执行写远端和 state | `--revisions`、`--remote`、`--dry-run`、`--check-remote`、`--force`、`--yes` |
| `build TARGET` | 本地构建 artifact | 写 worktree/cache，不连接远端 | `--revisions`、`--remote` |
| `history TARGET` | 查看部署历史 | 本地只读 | `--limit`、`--remote` |
| `verify TARGET` | 比较远端与 deployment record | 远端只读 | `--deployment` / `--latest`、`--remote` |
| `rollback TARGET` | 恢复部署前快照 | 正式执行写远端和 state | `--deployment` / `--latest`、`--dry-run`、`--check-remote`、`--force`、`--yes` |
| `state ...` | 管理 expected state | 取决于子命令 | 见下表 |

### State 子命令

| 命令 | 用途 | 远端行为 | 本地写入 |
|---|---|---|---|
| `state inspect TARGET` | 显示 target/generation/policy/transaction | 无 | 无 |
| `state verify TARGET` | 校验 current、CAS、Git tree、policy | 默认无；check 时只读 | 无 |
| `state bootstrap TARGET` | 创建 generation 1 | `--yes` 时只读验证 | `--yes` 写 state/Git store |
| `state recover TARGET` | 显示或执行 transaction 恢复 | execute 可能读写远端 | execute 更新 journal/state |
| `state migrate TARGET` | 迁移 legacy/named-remote 历史 | 无 | stage/yes 写本地历史 |
| `state policy-migrate TARGET` | 迁移 managed policy | execute 时远端只读 | `--execute --yes` CAS 推进 |
| `state gc` | v0.3.0 保留全部对象 | 不支持并返回错误 | 无 |

### 常用参数

| 参数 | 说明 |
|---|---|
| `--config PATH` | 指定 TOML；放在子命令之前 |
| `--version` | 显示版本 |
| `--remote NAME` | 选择 named remote |
| `--revisions SELECTOR...` | 一个或多个 commit/range selector |
| `--dry-run` | 预览，不执行 mutation |
| `--check-remote` | 增加只读远端核对；deploy/rollback 需与 dry-run 配合 |
| `--yes` | 跳过 mutation 的交互确认 |
| `--force` | 允许已确认的 hash drift；不能绕过其他门禁 |
| `--deployment ID` | 完整 deployment ID 或唯一前缀 |
| `--latest` | 最新成功 deployment |
| `--limit N` | history 每项目记录数，默认 20 |
| `--execute` | 执行 recover/policy migration 计划 |
| `--stage` | 创建 history migration staging |
| `--empty` | bootstrap 已验证的空基线 |

### 退出码

| 退出码 | 含义 |
|---:|---|
| 0 | 成功 |
| 1 | 一般预期部署错误 |
| 2 | 安全策略阻断或 migration conflict |
| 3 | 远端漂移或验证不一致 |
| 4 | 配置、路径或 Git revision 输入错误 |
| 130 | Ctrl-C 中断 |

```bash
git-deploy deploy --help
git-deploy state bootstrap --help
```

## 打包和发布

### 1. 发布门禁

```bash
uv lock --check
uv run pytest -q
uvx ruff check src tests
uvx ty check src
uv build --clear
```

当前 v0.3.2 基线为 365+ 个自动测试，包含 Host、真实 OpenSSH/SFTP 容器和 fake FTP/FTPS contract 门禁，以及 application exact-plan / exact-deployment 竞态回归。

### 2. 更新版本

同步修改 `pyproject.toml` 的 `project.version` 和 `src/git_deploy/__init__.py` 的 `__version__`，再由工具更新 lock：

```bash
uv lock
```

禁止手工编辑 `uv.lock`。**禁止**用新内容覆盖重发同一版本号（例如不要再发布不同内容的 `0.3.1`）。

### 3. 构建、校验与隔离安装

```bash
uv build --clear

(
  cd dist
  sha256sum \
    git_deploy-0.3.2-py3-none-any.whl \
    git_deploy-0.3.2.tar.gz \
    > SHA256SUMS
)

uv venv --clear tmp/release-smoke
uv pip install \
  --python tmp/release-smoke/bin/python \
  dist/git_deploy-0.3.2-py3-none-any.whl

tmp/release-smoke/bin/git-deploy --version
tmp/release-smoke/bin/git-deploy --help
```

继续用隔离配置验证 named remote、单/组合 revision 和 Host/Docker/1Password dry-run；fake Docker/op 调用必须为 0。

### 4. 提交、Tag 与 GitHub Release

```bash
git add README.md pyproject.toml uv.lock src tests docs git-deploy.example.toml
git commit -m "release v0.3.2"
git push

git tag -a v0.3.2 -m "git-deploy v0.3.2"
git push origin v0.3.2

gh release create v0.3.2 \
  dist/git_deploy-0.3.2-py3-none-any.whl \
  dist/git_deploy-0.3.2.tar.gz \
  dist/SHA256SUMS \
  --verify-tag \
  --title "git-deploy v0.3.2" \
  --notes-file docs/release-notes-v0.3.2.md
```

仓库当前通过 GitHub Release 分发 wheel/sdist，尚未配置 PyPI 自动发布。未来接入 PyPI 时应使用 trusted publishing 或受保护的 registry credential，不能把 token 写入仓库、命令历史或文档。

## 安全模型

- 部署 bytes 来自真实或本地合成 Git tree，不读取工作区文件。
- 非连续 selector 使用隔离 Git index/object directory，不修改正常 index、分支或仓库 objects。
- 未提交工作区变化被忽略并在输出中提示。
- 修改/删除前必须匹配可信 hash，upload 后再次验证。
- 已达到目标 hash 的上传和已不存在的删除按幂等 no-op 处理。
- `.env`、私钥、证书、runtime 及 `protected` 路径被阻断。
- source/artifact owner 冲突在连接远端前拒绝。
- artifact collector 拒绝绝对路径、`..`、symlink、submodule、FIFO、socket 和 device。
- 上传使用临时文件再 rename，删除最后执行；事务阶段写入 durable journal。
- source/artifact 任一步失败时恢复 before bytes 和 before state。
- `--force` 只处理明确漂移，不能绕过 identity、policy、integrity 或 transaction 门禁。
- Host runner 具有当前用户权限；Docker daemon 管理员可读取存活容器环境变量。
- 1Password reference、认证 token 和解析值不会写入 fingerprint、manifest、state 或日志。
- 回滚不处理数据库、消息、支付或其他外部系统副作用。
- v0.3 继续保留 state/CAS/Git 历史对象；自动 GC 与非最新回滚已冻结，不进入当前版本。

## 深入文档

- [v0.2 状态、目标与迁移运维](docs/v0.2-state-operations.md)
- [v0.2 构建产物与秘密安全](docs/v0.2-build-artifacts.md)
- [v0.2 北极星设计](docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md)
- [v0.2 原子 TODO](docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md)
- [v0.3 简化稳定版北极星](docs/planning/2026-07-14-git-deploy-v0.3-simplified-northstar.md)
- [已冻结的 v0.3 TUI 设计记录](docs/planning/2026-07-12-git-deploy-v0.3-tui-northstar.md)
