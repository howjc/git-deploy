# git-deploy v1-lite 破坏性重构总方案

> 仓库：`howjc/git-deploy`
> 当前主线版本：`v0.3.2`
> 当前主线提交：`d4cd698924608308a27172f362840f9dd813f63e`
> 方案日期：`2026-07-16`
> 目标：将项目从“本地发布管理平台”重构为“个人日常使用的 Git 感知型构建与文件同步工具”

> 实施基线校正：方案成稿后仓库已发布 `v0.3.3`（`aaf3f72f40e4369822574dd97a3b14226b1c9f5c`）。为保留其可靠性修复，实际 `legacy/v0.3` 与 `rewrite/v1-lite` 均从 `v0.3.3` 建立；下文保留的 `v0.3.2` 描述仅代表方案调研时的 main 基线。

---

## 1. 背景与问题定义

`git-deploy` 最初要解决的是一个非常直接的问题：

1. 在本地完成项目构建；
2. 找出本次发生变化的源码和构建产物；
3. 通过 FTP 或 SFTP 上传到远程服务器；
4. 日常重复执行同一条命令即可完成部署。

但随着项目持续演进，当前 `v0.3.2` 已经逐渐形成了一套较完整的状态化发布系统，包含：

- Expected State；
- Generation；
- Transition；
- CAS；
- Deployment Manifest；
- Transaction Journal；
- Bootstrap；
- Recover；
- Policy Migration；
- Latest Rollback；
- Application Service；
- Plan Token；
- Doctor；
- History；
- Verify；
- Artifact Baseline；
- Host/Docker Build Runner；
- 1Password Build 集成。

这些设计能够支撑更严肃的发布场景，但已经明显偏离个人日常开发需求。

当前主要问题不是单个功能不好用，而是产品模型本身变重：

```text
原始目标
本地构建 + 增量文件同步
        ↓
当前实现
可信状态机 + 事务发布 + 审计 + 回滚系统
```

由此产生了明显的认知负荷：

- 首次使用需要理解 bootstrap；
- 日常部署前需要理解 current、generation 和 plan；
- 失败后需要理解 transaction 和 recover；
- 回滚需要理解 manifest 和 lineage；
- 配置中出现大量与日常上传无关的概念；
- 用户需要先理解工具，才能完成本来很简单的部署工作。

---

# 2. 核心决策

## 2.1 进行破坏性重构

不在当前架构上继续叠加 Lite Mode，也不保留新旧两套运行模式。

原因：

1. Lite Mode 会形成两套 planner、state、executor 和错误处理；
2. 使用者仍需要判断自己应该使用哪种模式；
3. 维护者仍需理解旧状态机；
4. 旧架构会持续约束新版本设计；
5. 简化能力会被兼容逻辑重新复杂化。

因此，新版本采用全新的配置、状态和 CLI。

---

## 2.2 继续使用当前仓库

不新建仓库。

产品名称、目标用户和核心用途没有变化，仍然是：

> 在本地完成构建，并通过 FTP/SFTP 部署项目文件。

新建仓库会带来额外问题：

- 历史、Issue、Release 和文档被拆散；
- 两个仓库的产品身份难以区分；
- 可复用传输代码迁移成本增加；
- 旧仓库是否继续维护会成为新的决策负担；
- 使用者不清楚哪个才是主线。

旧代码通过 Git Tag 和冻结分支保存即可，不应在新主线中保留 `legacy/` 兼容目录。

---

## 2.3 新版本的北极星

> **一次配置，日常只执行一条命令，自动完成构建、差异计算和上传。**

默认操作：

```bash
git-deploy
```

多环境操作：

```bash
git-deploy dev
git-deploy prod
```

预览：

```bash
git-deploy --dry-run
```

完整覆盖同步：

```bash
git-deploy --full
```

用户不应该再需要理解以下概念：

```text
generation
transition
lineage
expected state
artifact baseline
CAS
manifest
transaction journal
policy fingerprint
plan token
recovery decision
```

---

# 3. 新产品定位

## 3.1 产品定义

`git-deploy v1-lite` 是一款：

> Git 感知型本地构建与 FTP/SFTP 文件同步工具。

它不是：

- CI/CD 平台；
- 发布审批系统；
- 服务器管理面板；
- 分布式发布系统；
- 审计合规系统；
- 自动回滚平台；
- 构建沙箱；
- 容器编排工具。

---

## 3.2 目标用户

主要面向：

- 个人开发者；
- 通过 WSL、本地 Linux 或 macOS 开发的用户；
- 仍使用 FTP/SFTP 发布项目的场景；
- PHP、Node.js 或前后端混合项目；
- 需要将 `dist/`、`build/`、`vendor/` 等构建产物同步到服务器的项目；
- 希望“一次配置，重复一条命令部署”的用户。

---

## 3.3 产品成功标准

不是看：

- 模块数量；
- 测试数量；
- 支持多少企业发布场景；
- 是否具备完整状态机。

而是看：

```text
完成代码修改
    ↓
执行 git-deploy
    ↓
自动完成 pnpm/npm/Composer/项目编译
    ↓
只上传本次变化
    ↓
部署结束
    ↓
继续开发
```

使用者不需要思考工具本身，才算成功。

---

# 4. v1-lite 范围边界

## 4.1 必须保留

### 本地构建

- npm；
- pnpm；
- yarn；
- Composer；
- 任意自定义命令；
- 多步骤串行执行；
- 构建失败立即终止；
- 构建完成后再计算产物变化。

### 源码增量

- Git Add；
- Git Modify；
- Git Delete；
- 从上次成功部署 Commit 计算到当前 HEAD；
- 支持首次完整上传；
- 支持工作区未提交变更提示。

### 构建产物增量

- `dist/`；
- `build/`；
- `vendor/`；
- 自定义输出目录；
- 本地 SHA256 Manifest；
- 新增、修改和删除；
- 只删除曾经由本工具记录的产物。

### 文件传输

- SFTP；
- FTP；
- 自动创建目录；
- 文件上传；
- 文件删除；
- 逐文件重试；
- 上传进度；
- SFTP 临时文件替换；
- 全部成功后更新本地状态。

### 基础安全

- `.env` 默认保护；
- 私钥、证书默认保护；
- 上传目录默认保护；
- Runtime、日志等目录可排除；
- 未知远端文件永不删除；
- FTP 密码使用环境变量；
- SFTP 支持 SSH Config 和 SSH Agent。

---

## 4.2 明确移除

| 移除能力 | 原因 |
|---|---|
| Expected State | 与个人日常上传不匹配 |
| Generation | 用户不可感知且增加恢复复杂度 |
| Transition ID | Git Commit 已足够表达源码历史 |
| Content Addressed Store | 不需要完整发布证据库 |
| Transaction Journal | 失败后重跑即可收敛 |
| Deployment Manifest | Git + 轻量本地 State 已足够 |
| History 命令 | 使用 Git Log 和 State 即可 |
| Verify 命令 | 不再维护远端 Expected State |
| 自动 Rollback | Git Revert 后重新部署 |
| State Bootstrap | 首次直接完整上传 |
| State Recover | 重新执行部署即可 |
| State Migration | 新旧状态不兼容 |
| Policy Migration | 新版本没有 Managed Policy |
| Application Service | 只有一个同步 CLI，无需多层适配 |
| Plan Token / HMAC | 不存在跨进程计划授权 |
| Worker / Cancellation | CLI 同步执行即可 |
| Artifact Baseline | 产物只和本地 Manifest 比较 |
| Artifact Cache | npm/pnpm/Composer 自带缓存 |
| Docker Build Runner | 构建直接在当前 Host/WSL 运行 |
| 1Password Build Provider | 环境变量由当前终端负责 |
| FTPS | 当前真实需求只保留 FTP/SFTP |
| TUI | 不属于核心工作流 |
| 自动 GC | 新状态不会持续积累大型对象 |
| Remote Hook | 避免 SFTP 与 FTP 能力不一致 |
| Health URL | 不属于文件同步核心职责 |
| Owner/Group 管理 | 通过正确部署账号和目录权限解决 |

---

# 5. 最终用户工作流

## 5.1 首次配置

在项目根目录创建：

```text
deploy.toml
```

然后执行：

```bash
git-deploy --dry-run
```

检查：

- Build Steps；
- Git 源码清单；
- Output 产物清单；
- 远端目标；
- Upload/Delete 数量。

确认后执行：

```bash
git-deploy
```

首次部署自动视为完整同步，不需要 bootstrap。

---

## 5.2 日常部署

```bash
git-deploy
```

默认使用 `default_target`。

等价流程：

```text
加载配置
    ↓
运行 Build Steps
    ↓
读取上次成功 Commit
    ↓
计算 Git 源码差异
    ↓
扫描 Outputs
    ↓
与上次 Output Manifest 比较
    ↓
生成 UPLOAD / DELETE
    ↓
上传和删除
    ↓
写入新的 State
```

---

## 5.3 部署不同环境

```bash
git-deploy dev
git-deploy prod
```

不同 Target 分别保存自己的：

- Last Commit；
- Output Manifest；
- Target Fingerprint；
- Last Success Time。

---

## 5.4 Dry-run

```bash
git-deploy prod --dry-run
```

要求：

- 默认执行构建；
- 计算完整文件变化；
- 不连接服务器；
- 不修改 State；
- 显示 Upload/Delete 计划；
- 返回清晰退出码。

可选：

```bash
git-deploy prod --dry-run --skip-build
```

只基于当前已有产物预览。

---

## 5.5 跳过构建

```bash
git-deploy prod --skip-build
```

适用于：

- 只修改 PHP 源码；
- 已经手动完成构建；
- 临时快速同步；
- 调试构建产物。

---

## 5.6 完整同步

```bash
git-deploy prod --full
```

行为：

- 上传所有受管 Git 源码；
- 上传所有 Outputs；
- 不依赖旧 Last Commit；
- 不删除未知远端文件；
- 完成后重建本地 State。

适用于：

- State 丢失；
- 新电脑；
- 远端需要重新覆盖；
- 第一次部署；
- 怀疑增量状态不可靠。

---

# 6. CLI 设计

## 6.1 极简命令模型

公开 CLI 最多只保留三个入口：

```text
git-deploy
git-deploy build
git-deploy doctor
```

默认没有子命令时就是部署。

---

## 6.2 部署命令

```bash
git-deploy
git-deploy prod
git-deploy prod --dry-run
git-deploy prod --skip-build
git-deploy prod --full
git-deploy prod --yes
```

### 参数

| 参数 | 作用 |
|---|---|
| `TARGET` | 可选，默认使用 `default_target` |
| `--dry-run` | 构建并预览，不连接服务器 |
| `--skip-build` | 跳过 Build Steps |
| `--full` | 完整上传并重建 State |
| `--yes` | 非交互确认 |
| `--config PATH` | 指定配置文件 |
| `--verbose` | 显示详细日志 |

---

## 6.3 Build 命令

```bash
git-deploy build
```

只运行：

```toml
[build]
steps = [...]
```

不连接服务器，不读取远端，不修改部署 State。

---

## 6.4 Doctor 命令

```bash
git-deploy doctor
git-deploy doctor prod
```

只检查：

- 配置是否合法；
- Git 仓库是否存在；
- Build 命令是否存在；
- Output 路径是否合法；
- Target 配置是否完整；
- FTP/SFTP 是否可连接；
- Remote Root 是否存在或可创建；
- 本地 State 是否可读。

不检查：

- CAS；
- Generation；
- Manifest；
- Transaction；
- Lineage；
- Rollback；
- Policy。

---

# 7. 配置格式

## 7.1 推荐配置

```toml
default_target = "dev"

[source]
include = ["**"]

exclude = [
  ".git/**",
  ".env",
  ".env.*",
  "node_modules/**",
  "runtime/**",
  "uploads/**",
  "storage/logs/**",
  "tests/**"
]

protect = [
  ".env",
  ".env.*",
  "uploads/**",
  "storage/cert/**",
  "**/*.key",
  "**/*.pem"
]

[build]
steps = [
  "pnpm install --frozen-lockfile",
  "pnpm run build",
  "composer install --no-dev --prefer-dist --optimize-autoloader --no-interaction"
]

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
host = "dev.example.com"
username = "deploy"
remote_root = "/www/wwwroot/project-dev"
ssh_host_alias = "project-dev"

[targets.prod]
protocol = "ftp"
host = "ftp.example.com"
username = "deploy"
password_env = "DEPLOY_FTP_PASSWORD"
remote_root = "/public_html"
passive = true
```

---

## 7.2 配置原则

### 一个仓库对应一个项目

不再在一个配置文件中定义多个 Projects。

当前目录就是 Project Root。

### Target 只表示远端环境

例如：

```text
dev
test
staging
prod
legacy
```

### Build Steps 使用字符串命令

原因：

- 用户更容易复制现有命令；
- 支持管道、参数和环境变量；
- 不再构建复杂 Runner 抽象；
- 直接使用当前 Shell 环境。

需要在文档中明确：

> `build.steps` 是可信配置，会在本机 Shell 中执行。

---

# 8. 构建设计

## 8.1 通用执行模型

```text
for step in build.steps:
    print step
    run step in project root
    if exit code != 0:
        stop
```

构建失败必须满足：

```text
Remote Connect = 0
Remote Writes = 0
State Changes = 0
```

---

## 8.2 npm / pnpm

示例：

```toml
[build]
steps = [
  "pnpm install --frozen-lockfile",
  "pnpm run build"
]
```

也可使用：

```toml
steps = [
  "npm ci",
  "npm run build"
]
```

---

## 8.3 Composer

开发阶段更新依赖：

```bash
composer update
git add composer.json composer.lock
git commit
```

部署阶段使用：

```bash
composer install \
  --no-dev \
  --prefer-dist \
  --optimize-autoloader \
  --no-interaction
```

工具不应在部署时自动运行 `composer update`，避免产生不可重复依赖版本。

---

## 8.4 自定义编译

例如：

```toml
[build]
steps = [
  "make release",
  "./scripts/build-assets.sh"
]
```

工具无需理解具体技术栈。

---

# 9. Git 源码差异设计

## 9.1 State 中有 Last Commit

执行：

```bash
git diff --no-renames --name-status -z LAST_COMMIT..HEAD
```

只处理：

- `A`：上传；
- `M`：上传；
- `D`：删除。

---

## 9.2 明确禁用 Rename Detection

使用：

```bash
--no-renames
```

统一转换：

```text
Rename = Delete Old + Add New
Copy   = Add New
```

这样可以避免：

- Rename 推断错误；
- Copy Baseline 错误；
- Duplicate Content 被误识别为 Copy；
- Old Path 残留；
- Planner 引入额外状态语义。

---

## 9.3 State 中没有 Last Commit

首次部署或 `--full`：

```text
读取 HEAD 下所有受管文件
全部生成 UPLOAD
```

默认不删除任何未知远端内容。

---

## 9.4 工作区未提交内容

默认部署只读取 Git Commit。

检测到未提交内容时：

```text
WARNING: uncommitted changes are not included
```

默认继续还是阻止，可配置：

```toml
[source]
require_clean_worktree = true
```

推荐默认：

```text
false
```

但必须清晰提示。

---

# 10. 构建产物 Manifest

## 10.1 为什么需要 Manifest

`dist/`、`build/`、`vendor/` 往往不进入 Git。

因此需要一个非常轻量的本地文件记录上次成功部署的产物。

---

## 10.2 Manifest 内容

```json
{
  "public/dist/app.js": {
    "sha256": "...",
    "size": 10240
  },
  "vendor/autoload.php": {
    "sha256": "...",
    "size": 2048
  }
}
```

---

## 10.3 差异规则

| 上次 Manifest | 当前本地文件 | 操作 |
|---|---|---|
| 不存在 | 存在 | Upload |
| Hash 不同 | 存在 | Upload |
| Hash 相同 | 存在 | Unchanged |
| 存在 | 不存在 | Delete |
| 从未记录 | 远端存在 | 不处理 |

---

## 10.4 删除安全

产物删除只能发生在：

```text
上次 Manifest 中存在
且当前本地已经删除
且 delete_removed = true
```

绝不扫描远端并删除“本地没有”的未知文件。

---

# 11. 轻量 State

## 11.1 存储位置

推荐：

```text
.git/git-deploy/<target>.json
```

例如：

```text
.git/git-deploy/dev.json
.git/git-deploy/prod.json
```

优点：

- 自动与项目绑定；
- 不污染仓库工作区；
- 不提交到 Git；
- 方便删除和重建；
- 多环境相互隔离。

---

## 11.2 State 格式

```json
{
  "schema": 1,
  "target": "prod",
  "target_fingerprint": "sftp:host:22:/www/wwwroot/project",
  "last_commit": "abc123...",
  "deployed_at": 1784192400,
  "outputs": {
    "public/dist/app.js": {
      "sha256": "...",
      "size": 10240
    }
  }
}
```

---

## 11.3 State 只在全部成功后更新

要求：

```text
Build Success
    ↓
All Upload/Delete Success
    ↓
Atomic Write State
```

任意中途失败：

```text
State 保持旧值
```

重新执行同一命令会重新计算完整变化集并继续覆盖。

---

# 12. 失败与恢复模型

## 12.1 不再做复杂事务

不再追求多文件全局原子性。

现实中的 FTP/SFTP 本身也无法提供跨多个文件的全局事务。

新的可靠性原则是：

> 操作幂等、状态延迟提交、失败后重跑收敛。

---

## 12.2 上传到一半失败

例如 100 个文件上传到第 60 个失败：

```text
Remote 已更新部分文件
Local State 未更新
```

再次执行：

```bash
git-deploy prod
```

仍会以旧 State 生成本次完整变化集。

已经成功的文件会再次覆盖，其余文件继续上传。

---

## 12.3 删除操作

删除必须幂等：

- 文件存在：删除；
- 文件不存在：视为成功；
- 权限错误：失败；
- 网络失败：重试。

---

## 12.4 自动重试

推荐默认：

```toml
[deploy]
retries = 3
retry_delay = 2
```

逐文件重试，而不是重跑整个 Build。

---

## 12.5 回滚

不再提供自动 Rollback。

推荐流程：

```bash
git revert <bad-commit>
git-deploy prod
```

或者：

```bash
git checkout <good-commit>
git-deploy prod --full
```

Git 本身负责版本历史，部署工具只负责同步。

---

# 13. SFTP 设计

## 13.1 保留能力

- SSH Config；
- `ssh_host_alias`；
- SSH Agent；
- 1Password SSH Agent 间接支持；
- Host Key Checking；
- 自动创建目录；
- 上传进度；
- 逐文件重试；
- 临时文件上传；
- Rename 替换；
- 删除文件。

---

## 13.2 删除能力

- Chown；
- Chgrp；
- 自动 Owner/Group；
- Remote Hook；
- Health Check；
- Expected Mode Lineage；
- Remote Drift；
- Remote Manifest。

服务器目录权限应由用户提前准备：

```text
deploy 用户对 remote_root 有正确写权限
Web 用户通过组权限或部署目录权限读取
```

---

## 13.3 临时文件替换

流程：

```text
upload target.git-deploy.tmp
    ↓
close
    ↓
rename temp → target
```

当服务器不支持覆盖 Rename 时：

- 使用明确、可恢复的兼容路径；
- 不应捕获所有 OSError 后直接删除 Target；
- 失败时尽量保留旧文件。

---

# 14. FTP 设计

## 14.1 保留能力

- Passive Mode；
- Password Env；
- 自动创建目录；
- Upload；
- Delete；
- Retry；
- Progress；
- Binary Mode。

---

## 14.2 明确限制

FTP 不保证：

- 原子替换；
- POSIX 权限；
- Owner/Group；
- 服务器身份认证；
- Remote Command；
- Health Check。

不要为 FTP 模拟完整 SFTP 语义。

---

# 15. 新代码结构

```text
src/git_deploy/
├── __init__.py
├── cli.py
├── config.py
├── builder.py
├── git.py
├── planner.py
├── manifest.py
├── deployer.py
├── progress.py
├── errors.py
└── transports/
    ├── __init__.py
    ├── base.py
    ├── sftp.py
    └── ftp.py
```

---

## 15.1 模块职责

### `cli.py`

- 参数解析；
- 加载配置；
- 调用 Build、Plan、Deploy；
- 输出结果；
- 返回退出码。

### `config.py`

- TOML 解析；
- 默认值；
- Path 解析；
- Target 校验；
- Protect/Exclude 校验。

### `builder.py`

- 串行执行 Build Steps；
- 输出日志；
- 构建失败终止。

### `git.py`

- 获取 HEAD；
- 检查工作区；
- Git Diff；
- 全量文件列表；
- 路径标准化。

### `planner.py`

- 合并 Source Changes；
- 合并 Output Changes；
- 生成 Upload/Delete；
- Protect 检查；
- 冲突检查。

### `manifest.py`

- 读取轻量 State；
- 扫描 Outputs；
- 计算 Hash；
- 原子写入 State。

### `deployer.py`

- 构建最终操作队列；
- 调用 Transport；
- 重试；
- 进度；
- 成功后提交 State。

### `transports/`

只负责文件操作，不理解 Git、Build 和 State。

---

# 16. 仓库与分支策略

## 16.1 保留旧版本

保留（实施时采用当时最新的 v0.3.3）：

```text
v0.3.3
```

并创建：

```text
legacy/v0.3
```

该分支只用于：

- 查看旧代码；
- 紧急安全修复；
- 保留旧文档；
- 必要时重新构建旧版本。

不继续增加功能。

---

## 16.2 创建重构分支

```bash
git switch main
git pull

git branch legacy/v0.3
git push origin legacy/v0.3

git switch -c rewrite/v1-lite
git push -u origin rewrite/v1-lite
```

不使用 Orphan Branch，以便：

- 保留完整历史；
- 查看删除 Diff；
- 复用传输代码；
- 方便 Review；
- 必要时 Cherry-pick。

---

## 16.3 最终分支结构

```text
main
└── 最终极简稳定版本

rewrite/v1-lite
└── 破坏性重构开发分支

legacy/v0.3
└── 冻结的 Stateful 旧架构

v0.3.3
└── 旧架构最后 Tag

v1.0.0-alpha.1
v1.0.0-beta.1
v1.0.0
└── 极简版本发布序列
```

---

# 17. 兼容策略

## 17.1 不兼容旧配置

v1 不读取 v0.3 的复杂配置。

原因：

- 字段语义已经变化；
- 自动迁移会引入大量边界条件；
- 运行时兼容会重新增加复杂度；
- 新配置非常短，人工迁移成本低。

---

## 17.2 不读取旧 State

忽略：

- Expected State；
- CAS；
- Transactions；
- Manifests；
- Generation；
- Rollback History。

旧 State 可以留在磁盘中，不影响 v1。

---

## 17.3 可选一次性迁移脚本

非首版必需。

未来可提供：

```bash
git-deploy migrate-config old-deploy.toml > deploy.toml
```

该工具只做配置转换，不成为运行时兼容层。

---

# 18. 实施里程碑

## M0：冻结旧架构

- 创建 `legacy/v0.3`；
- 创建 `rewrite/v1-lite`；
- README 标记 v0.3 为 Legacy Stateful；
- 写入 v1-lite Scope；
- 停止旧架构功能开发；
- 冻结旧配置格式。

### 验收

- `v0.3.3` Tag 可继续安装；
- Legacy Branch 可重新构建；
- Rewrite Branch 独立开发；
- 新需求不再进入旧架构。

---

## M1：最小 CLI 与配置

- 默认部署入口；
- Target 选择；
- `--dry-run`；
- `--skip-build`；
- `--full`；
- 新 TOML Config；
- 配置校验；
- 最小错误模型。

### 验收

```bash
git-deploy --help
git-deploy --dry-run
git-deploy prod --dry-run
```

---

## M2：本地构建

- Build Steps；
- Shell 执行；
- Stdout/Stderr 实时输出；
- Timeout；
- Build Failure；
- `git-deploy build`。

### 验收

- pnpm 成功；
- npm 成功；
- Composer 成功；
- 任一命令失败时零 Remote Connect。

---

## M3：Git 源码 Planner

- Last Commit；
- HEAD；
- `--no-renames`；
- Add；
- Modify；
- Delete；
- Full Upload；
- Include/Exclude；
- Protect；
- Uncommitted Warning。

### 验收

- Rename 正确表现为 Delete + Upload；
- Duplicate Content Add 不被误判为 Copy；
- Protected 文件绝不进入操作清单；
- 首次部署产生完整 Upload。

---

## M4：Output Manifest

- Output 扫描；
- SHA256；
- Upload；
- Delete Removed；
- State JSON；
- Atomic Write；
- Multi-target State Isolation。

### 验收

- `dist/` 增量同步；
- `vendor/` 增量同步；
- Hash 未变化不上传；
- 未知 Remote 文件不删除；
- State 丢失可通过 `--full` 重建。

---

## M5：SFTP

- SSH Config；
- SSH Agent；
- Host Key；
- Directory Create；
- Upload；
- Temp Rename；
- Delete；
- Retry；
- Progress。

### 验收

- Node 前端部署；
- PHP 部署；
- 混合项目部署；
- 网络中断重跑收敛；
- 失败不更新 State。

---

## M6：FTP

- Passive Mode；
- Password Env；
- Directory Create；
- Upload；
- Delete；
- Retry；
- Progress。

### 验收

- FTP-only 项目完成完整部署；
- 重复执行幂等；
- 删除不存在文件不失败；
- 不声明 POSIX 权限保证。

---

## M7：真实项目验证

至少覆盖：

### Node 前端

```text
pnpm install
pnpm build
同步 dist
删除旧 Hash Asset
```

### PHP

```text
composer install
同步 PHP Source
同步 vendor
保护 .env/runtime/uploads
```

### PHP + Node

```text
pnpm build
composer install
同步 Source + dist + vendor
```

### 故障

```text
上传中断
State 不更新
重新执行
最终收敛
```

---

## M8：删除旧架构

删除：

- `application/`；
- Expected State；
- CAS；
- Transaction；
- Rollback；
- Recovery；
- Migration；
- Policy Migration；
- Build Cache；
- Docker Runner；
- 1Password Build；
- Artifact Planner；
- Combined Planner；
- History/Verify；
- 旧复杂测试；
- 旧状态文档。

保留可复用：

- SFTP 连接；
- FTP 连接；
- SSH Config；
- Progress；
- 部分配置解析；
- 路径安全检查。

---

# 19. 原子 TODO

## Phase A：仓库准备

- [x] 创建 `legacy/v0.3`
- [x] 创建 `rewrite/v1-lite`
- [x] 新增 v1 Scope 文档
- [x] 标记 v0.3 Legacy
- [x] 禁止旧架构功能合入
- [x] 建立重构 PR

## Phase B：基础骨架

- [x] 重写 CLI Parser
- [x] 实现默认 Deploy 入口
- [x] 实现 Target 选择
- [x] 实现新 Config Model
- [x] 实现统一 Error 类型
- [x] 实现退出码约定
- [x] 实现 Verbose 日志

## Phase C：Builder

- [x] Build Step Model
- [x] Shell Executor
- [x] 实时日志
- [x] Timeout
- [x] Failure Stop
- [x] Skip Build
- [x] Build-only 命令
- [x] Build 单元测试

## Phase D：Git Planner

- [x] 读取 HEAD
- [x] 读取 Last Commit
- [x] Full File List
- [x] Diff `--no-renames`
- [x] Add
- [x] Modify
- [x] Delete
- [x] Include
- [x] Exclude
- [x] Protect
- [x] Uncommitted Warning
- [x] Rename 测试
- [x] Duplicate Content 测试

## Phase E：Output Manifest

- [x] Output Mapping
- [x] Directory Scan
- [x] SHA256
- [x] Add
- [x] Modify
- [x] Delete Removed
- [x] Unknown Remote Protection
- [x] Target State Path
- [x] State Atomic Write
- [x] State Schema
- [x] State Corruption Handling
- [x] Full Rebuild

## Phase F：Plan Merge

- [x] 合并 Source/Output Operations
- [x] Remote Path 冲突检查
- [x] Protect 最终检查
- [x] Upload/Delete 去重
- [x] Deterministic Sort
- [x] Dry-run Renderer
- [x] Summary Renderer

## Phase G：SFTP

- [x] SSH Config
- [x] SSH Host Alias
- [x] SSH Agent
- [x] Host Key
- [x] Connect Timeout
- [x] Directory Create
- [x] Upload Stream
- [x] Temp Rename
- [x] Delete
- [x] Retry
- [x] Progress
- [x] Close
- [x] Real SFTP Integration Test

## Phase H：FTP

- [x] Password Env
- [x] Passive Mode
- [x] Connect
- [x] Directory Create
- [x] Upload
- [x] Delete
- [x] Retry
- [x] Progress
- [x] Close
- [x] FTP Integration Test

## Phase I：Deployer

- [x] Build Before Connect
- [x] Plan Before Connect
- [x] Confirmation
- [x] Operation Queue
- [x] Per-file Retry
- [x] Failure Stops State Commit
- [x] Success Atomic State Commit
- [x] Re-run Convergence
- [x] Full Mode
- [x] Exit Summary

## Phase J：Doctor

- [x] Config Check
- [x] Git Check
- [x] Build Command Check
- [x] Output Path Check
- [x] State Check
- [x] Target Check
- [x] SFTP Connect Check
- [x] FTP Connect Check
- [x] Remote Root Check

## Phase K：清理旧代码

- [x] 删除 Application Layer
- [x] 删除 State Engine
- [x] 删除 CAS
- [x] 删除 Transaction
- [x] 删除 Rollback
- [x] 删除 Recovery
- [x] 删除 Migration
- [x] 删除 Docker Build
- [x] 删除 Build Cache
- [x] 删除 Artifact Baseline
- [x] 删除 History/Verify
- [x] 删除旧文档
- [x] 删除旧测试
- [x] 更新 README

## Phase L：发布

- [x] `v1.0.0-alpha.1`
- [x] Node 项目试用
- [x] PHP 项目试用
- [x] FTP 项目试用
- [ ] 修复 Alpha 问题
- [ ] `v1.0.0-beta.1`
- [ ] 连续日常使用验证
- [ ] `v1.0.0`

---

# 20. 验收标准

## 20.1 认知负荷

用户只需要理解：

```text
Build Steps
Source
Outputs
Target
Last Successful Commit
```

用户不需要理解：

```text
Generation
Transition
CAS
Manifest
Transaction
Lineage
Policy
Recovery
```

---

## 20.2 操作复杂度

一次配置后，正常部署必须只需要：

```bash
git-deploy
```

多环境最多增加一个参数：

```bash
git-deploy prod
```

---

## 20.3 构建可靠性

- Build 失败时零 Remote Connect；
- Build 失败时 State 不更新；
- Build 日志实时可见；
- Build 命令退出码准确。

---

## 20.4 文件安全

- `.env` 永不上传；
- Protected 文件永不删除；
- Uploads 永不触碰；
- Runtime 永不触碰；
- 未知 Remote 文件永不删除；
- Output 删除只针对旧 Manifest 记录。

---

## 20.5 故障恢复

- 上传失败时 State 不更新；
- 重复执行可收敛；
- 文件重复覆盖安全；
- 文件重复删除安全；
- State 丢失时 `--full` 可恢复；
- 不要求用户执行 Recover 命令。

---

## 20.6 性能

- 未变化文件不上传；
- 大型 `vendor/` 仅上传 Hash 变化文件；
- Build Output 扫描可接受；
- State JSON 保持轻量；
- 不产生持续增长的 CAS。

---

# 21. 风险与控制

## 风险一：删除事务后多文件部署不是原子操作

接受该限制。

控制方式：

- Build 完成后才连接；
- 单文件尽量原子替换；
- State 延迟提交；
- 操作幂等；
- 失败后重跑。

---

## 风险二：没有 Remote Drift

个人单控制器场景中可接受。

控制方式：

- 不删除未知 Remote 文件；
- Full Mode 不清空 Remote；
- 文档明确不要同时使用多个发布器管理相同路径。

---

## 风险三：没有自动 Rollback

接受。

控制方式：

- Git Revert；
- 重新部署；
- 必要时 `--full`；
- 服务器级备份由服务器管理工具负责。

---

## 风险四：Shell Build Step 具有本机权限

明确视为可信配置。

控制方式：

- 配置文件不接收不可信输入；
- README 明确 Build Step 会直接执行；
- 工具不伪装为沙箱。

---

# 22. 最终原则

v1-lite 必须长期遵守：

## 原则一：一条命令

日常部署必须能够通过：

```bash
git-deploy
```

完成。

## 原则二：概念预算

新增一个用户概念时，必须删除或替代一个旧概念。

## 原则三：失败重跑

默认恢复方式是：

```bash
git-deploy
```

而不是引入新的恢复子系统。

## 原则四：Git 管源码，Manifest 管产物

不再建设第三套发布历史。

## 原则五：未知远端内容不处理

只管理工具明确拥有的文件。

## 原则六：不兼容优于错误兼容

v1 使用新配置和新 State，不保留旧运行时兼容层。

## 原则七：先满足真实项目

新能力必须先证明能够简化真实 Node、PHP 和 FTP/SFTP 项目，而不是先完善抽象。

---

# 23. 最终实施建议

采用以下路径：

```text
保留 v0.3.3 Tag
    ↓
建立 legacy/v0.3
    ↓
创建 rewrite/v1-lite
    ↓
冻结旧架构
    ↓
重新实现最小核心
    ↓
真实 Node/PHP 项目验证
    ↓
删除旧代码
    ↓
合并 main
    ↓
发布 v1.0.0
```

最终产品应从：

```text
Local Release Management Platform
```

回归为：

```text
Git-aware Build & Deployment Sync Tool
```

即：

> **构建、找差异、上传，仅此而已。**

---

# 24. 实施变更记录

- 2026-07-16：实施基线从方案调研时的 v0.3.2 校正为已发布的 v0.3.3，避免丢失后续可靠性修复。
- 2026-07-16：SFTP Target 增加可选 `known_hosts_file`，用于自动化环境固定 Host Key；不改变默认读取系统 known_hosts 的行为。
- 2026-07-16：`outputs.remote = "."` 允许显式映射远端根目录，最终路径仍经过 protect、冲突和越界检查。
- 2026-07-16：Phase L 的 Beta、连续日常使用与 v1.0.0 保留为发布后阶段；本轮发布目标为 `v1.0.0-alpha.1`。
- 2026-07-16：重构分支已推送并建立 Draft PR #5（`rewrite/v1-lite` → `main`）。
- 2026-07-16：已发布 GitHub prerelease `v1.0.0-alpha.1`；下载复核 wheel SHA256 为 `c20b85a0aef2430954e3a471e64770eb2891c39a6fcf4b00e7416b92643cffbb`，sdist SHA256 为 `d813d5f6935a125c81c5da889b68007f5ee9dece848ca37a793e17b64504311f`。
