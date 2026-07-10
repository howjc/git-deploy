# git-deploy 构建产物部署北极星

> 状态：下一迭代，待实施（目标版本 v0.2）。
> 依据：用户要求把 Composer `vendor/`、前端 `dist/`、Go 二进制等构建产物纳入下一迭代；当前 v0.1 实现核查：`gitrepo.py` 直接通过 `git cat-file` 读取目标提交中的已跟踪文件，不创建临时工作区，也不执行构建。

## 北极星目标

在不读取当前脏工作区、不改变既有 commit-range 部署和精确回滚语义的前提下，为每个待部署提交创建隔离的 detached Git worktree，在其中执行受配置约束的本地构建，收集 Git 未跟踪的生成产物，并将“源码差量 + 构建产物差量”作为一个可校验、可备份、可回滚的部署事务。

目标完成后，以下场景应成为一等能力：

- PHP/Composer：在目标提交中执行 `composer install --no-dev`，部署生成的 `vendor/`。
- Node/Vite：在目标提交中执行 `npm ci && npm run build`，部署生成的 `dist/`。
- Go：在目标提交中执行 `go build`，部署生成的 Linux 二进制。
- 混合项目：同一次部署中既发布 Git 跟踪源码，也发布一个或多个构建产物目录或文件。
- FTP/FTPS/SFTP：构建始终在本地隔离工作区执行，远端只接收最终文件；FTP 不依赖远程 shell。

## 当前边界

v0.1 的部署链路是：

1. 解析 `FROM..TO` 并读取 Git name-status diff。
2. 通过 Git object database 直接读取目标文件 bytes，不 checkout。
3. 校验远端文件与 FROM commit 的 SHA-256 基线。
4. 备份远端原文件。
5. 上传远端临时文件并 rename，最后处理删除。
6. 执行可用的远程钩子和健康检查；失败时自动恢复备份。

因此 v0.1 有以下确定限制：

- 只能部署 Git 已跟踪文件。
- 不会生成或部署被 `.gitignore` 排除的 `vendor/`、`dist/`、二进制等产物。
- 不能安全执行目标提交自己的 Composer、npm、Go 构建流程。
- 生成产物不属于 Git blob，无法直接得到 FROM 侧远端漂移校验基线。
- 当前上传前会把目标文件读入内存；大规模依赖目录不适合继续沿用全量内存模型。

## 核心原则

### 1. 提交是唯一源码输入

- 构建目录必须来自 `git worktree add --detach <path> <commit>`。
- 不复制当前 working tree，不读取其中的未提交文件、ignored 文件或本地 `.env`。
- 构建产物必须位于该 detached worktree 内，并受路径边界检查。
- FROM 与 TO 构建使用各自提交内容，禁止用当前分支文件替代。

### 2. 构建与远端部署解耦

- Composer、npm、Go 等命令只在本机临时 worktree 内执行。
- FTP/FTPS/SFTP 只负责远端读写，不在 FTP 上模拟远程命令。
- SFTP 的 `post_commands` 仍是部署后操作，不承担生成构建产物的职责。
- 构建失败发生在任何远端写入之前，不得创建成功部署记录。

### 3. 产物也必须有基线

- 每次构建生成 artifact manifest，至少记录相对路径、SHA-256、字节数、可执行位、来源提交和 build fingerprint。
- TO manifest 决定上传后的目标状态。
- FROM manifest 决定远端预期基线和产物删除集合。
- 优先复用本地缓存中与 `FROM commit + build fingerprint` 完全匹配的 manifest；缓存缺失时，在独立 worktree 中补建 FROM。
- 不允许仅构建 TO 后凭远端目录列表猜测删除项。

### 4. 一个部署事务，一套回滚快照

- Git 跟踪文件与 artifact 文件合并为单一 deployment plan。
- 同一远端路径只能由一个来源拥有；源码与 artifact 路径重叠必须在远端写入前拒绝。
- 所有待修改或删除的远端文件先备份，再开始上传。
- 上传/替换先执行，删除最后执行。
- 构建产物部署失败时，源码和产物必须一起恢复到部署前状态。
- 回滚仍以 deployment ID 为准，不要求重新构建历史提交。

### 5. 保留 dry-run 的零副作用语义

- 普通 `deploy --dry-run` 只解析配置、Git diff 和预期构建步骤。
- 普通 dry-run 不创建 worktree、不执行包管理器、不连接远端、不写 deployment state。
- `--dry-run --check-remote` 只检查已有可用基线；不得为了远端检查隐式执行构建。
- 新增显式本地构建验证入口，例如 `git-deploy build PROJECT --to COMMIT`；它可以创建临时 worktree和产物 manifest，但绝不连接或修改远端。
- CLI 的最终命名可在原子 TODO 的配置契约任务中固定，但不得改变上述副作用边界。

## 目标配置契约

配置需要表达构建命令、受控环境变量和产物映射。建议契约如下，最终字段名由实现前的 schema 测试固化：

```toml
[projects.official-v2]
repository = "."
remote_root = "/"
include = ["app/**", "config/**", "public/**", "route/**", "view/**"]

[projects.official-v2.build]
commands = [
  ["composer", "install", "--no-dev", "--classmap-authoritative", "--no-interaction"],
]
timeout = 900
env_allowlist = ["COMPOSER_AUTH"]

[[projects.official-v2.artifacts]]
source = "vendor"
destination = "vendor"
kind = "tree"
```

配置规则：

- 命令优先使用 argv 数组，不默认经过 shell 解释。
- 确需 shell 管道或变量展开时必须显式启用，并在 plan 中标明。
- `env_allowlist` 只声明允许继承的变量名；变量值不得进入日志、manifest 或部署历史。
- artifact `source` 必须是 worktree 相对路径，禁止绝对路径、`..`、symlink 逃逸。
- artifact `destination` 必须位于项目 `remote_root` 下。
- `kind=file` 与 `kind=tree` 均需支持。
- artifact 路径必须继续遵守 `.env`、私钥、证书和项目 `protected` 规则。
- build 配置变化必须改变 build fingerprint，禁止错误复用旧缓存。

## 目标架构

| 组件 | 责任 |
|------|------|
| `WorktreeManager` | 创建 detached worktree、处理并发锁、捕获中断并可靠清理、执行 `git worktree prune` 兼容恢复 |
| `BuildRunner` | 在 worktree 根目录执行 argv 命令、控制 timeout、环境变量白名单、日志脱敏和退出码 |
| `ArtifactCollector` | 校验 artifact 边界，拒绝 symlink/submodule/特殊文件，生成文件级 manifest |
| `BuildCache` | 以 commit + build fingerprint 缓存 manifest；缓存只用于优化，不改变正确性 |
| `CombinedPlanner` | 合并 Git diff 与 FROM/TO artifact diff，检测远端路径冲突并生成统一操作序列 |
| `DeploymentExecutor` | 以流式或磁盘 spool 方式上传，避免把大型 artifact tree 全部加载进内存 |
| `DeploymentStore` | 在现有 manifest 中记录 artifact provenance、构建指纹和统一 before/after 快照 |

## 构建指纹

build fingerprint 至少覆盖：

- 完整 FROM/TO commit ID。
- 构建命令 argv、工作目录、timeout 配置。
- artifact source/destination/kind 配置。
- 允许继承的环境变量名称，不记录敏感值。
- 影响依赖解析的 lock 文件内容哈希，例如 `composer.lock`、`package-lock.json`、`uv.lock`、`go.sum`。
- `git-deploy` 工具版本和 artifact manifest schema 版本。
- 影响产物兼容性的目标平台声明，例如 `GOOS/GOARCH` 或 PHP platform 配置。

仅当指纹完全一致时才能复用构建 manifest。缓存命中不能跳过远端漂移检查。

## 产物差量规则

对 FROM 和 TO artifact manifest 做路径级比较：

| 差异 | 远端操作 |
|------|----------|
| TO 新增路径 | upload，预期 FROM 不存在 |
| 哈希或可执行位变化 | replace，预期远端匹配 FROM 哈希 |
| FROM 存在、TO 缺失 | delete，预期远端匹配 FROM 哈希 |
| 内容和模式均相同 | 不产生操作 |

第一轮 artifact 部署若不存在可信 FROM manifest，必须选择以下显式路径之一：

1. 自动构建 FROM，得到可验证基线。
2. 用户使用明确的 bootstrap 模式，将当前远端文件作为 before snapshot，并在 plan 中标记无法证明其来源。

不得静默把“无 FROM manifest”当成“远端不存在”。

## 安全要求

- 构建目标提交等同于执行该提交中的代码，实际执行前必须显示项目、提交和命令摘要。
- 构建进程默认使用最小环境，不继承生产数据库密码、FTP 密码、SSH Agent socket或其他无关敏感变量。
- 允许的 registry token 仅按变量名透传，任何日志和异常消息都不得输出值。
- 不把项目 `.env`、用户 home 下凭据或现有 working tree ignored 文件复制进 worktree。
- worktree 和 artifact cache 权限默认仅当前用户可读写。
- 拒绝 artifact 中的 symlink、socket、device、FIFO 和路径穿越。
- 每项目设置构建超时；超时必须终止完整进程组并清理 worktree。
- 同一 repository/project 同时只能有一个构建部署事务，防止缓存和 worktree 互相覆盖。
- 远端临时文件名加入 deployment ID 和随机因子，避免并发部署只依赖 PID 冲突。

## 非目标

- 不在生产服务器上执行 Composer、npm、Go 编译。
- 不自动执行数据库迁移或尝试回滚数据库 schema/data。
- 不把 Docker image、Kubernetes、容器编排纳入 v0.2。
- 不实现通用 CI/CD 平台、审批流或多阶段环境晋级。
- 不把生产 `.env`、证书、私钥打入构建产物。
- 不保证第三方包仓库永久可用；离线缓存和私有 registry 属于部署环境配置。
- 不在本轮支持 Git submodule 或 artifact symlink。

## 里程碑

| 里程碑 | 目标 | 完成信号 |
|--------|------|----------|
| M1 | 配置与 manifest 契约冻结 | build/artifact TOML schema、build fingerprint 和 manifest 版本有解析/兼容测试 |
| M2 | 隔离 worktree 生命周期 | FROM/TO worktree 可创建；成功、失败、超时、Ctrl-C 后均清理；当前脏工作区不影响结果 |
| M3 | 本地构建与产物采集 | Composer/npm/Go fixture 至少覆盖两类；生成文件、目录和可执行位进入 artifact manifest |
| M4 | 产物基线与统一计划 | FROM/TO artifact diff 与 Git diff 合并；新增、修改、删除、冲突、漂移均有测试 |
| M5 | 流式部署与事务回滚 | 大文件和多文件不全量驻留内存；任一步失败可恢复源码和 artifact 的 before snapshot |
| M6 | CLI 与 dry-run 兼容 | `all`、项目独立 range、普通 dry-run、远端只读检查、本地 build 验证边界稳定 |
| M7 | official-v2 首个真实配置样例 | `vendor/` 构建映射可在 fixture/临时 FTP transport 中完整演练；真实生产 FTP 联调独立由用户代验 |
| M8 | 发布门禁 | 单元/集成测试、Ruff、类型检查、uv build、隔离 uv tool install 全部通过，v0.1 无构建项目行为不回归 |

## 完成定义

必须全部满足：

1. 未配置 build/artifacts 的现有项目，其 plan、deploy、verify、rollback 行为与 v0.1 兼容。
2. 普通 `--dry-run` 不创建 worktree、不执行构建、不连接远端、不写本地 deployment state。
3. detached worktree 始终对应请求的完整 commit ID；主工作区脏文件无法进入源码或 artifact。
4. 构建失败、超时和用户中断均发生在远端写入前，并可靠清理临时 worktree。
5. 构建命令默认不经 shell，环境变量按名称白名单继承，敏感值不进入日志和 manifest。
6. artifact collector 拒绝绝对路径、父目录穿越、symlink 和特殊文件。
7. FROM/TO artifact manifest 能确定新增、修改、删除和可执行位变化。
8. 无可信 FROM artifact 基线时部署必须阻断或要求显式 bootstrap，不得假设远端为空。
9. 源码与 artifact 远端路径冲突在任何远端写入前阻断。
10. artifact 远端漂移默认阻断部署；`--force` 行为与源码漂移保持一致并仍保存真实 before snapshot。
11. 上传采用逐文件流式读取或磁盘 spool，集成测试证明内存使用不随全部 artifact 总大小线性累积。
12. 部署中途失败时，已修改的源码和 artifact 均恢复；新增文件删除；原有删除文件恢复。
13. deployment ID 回滚不需要重新构建 FROM/TO，完全依赖部署前保存的快照。
14. FTP/FTPS 构建产物部署不依赖远程 shell；SFTP `post_commands` 保持可选。
15. `all` 中各项目使用独立 worktree、构建指纹和提交范围；一个项目失败后不得误用另一项目产物。
16. official-v2 fixture 使用锁定依赖执行 Composer 构建，验证 `vendor/` 文件新增、修改、删除、漂移和回滚。
17. 自动测试只使用临时仓库、fixture 和内存/本地 mock transport；生产 FTP 账号和密码不进入自动验证。
18. 真实 FTP/FTPS 联调作为独立用户代验项，需明确测试目录、最小权限账号、无生产覆盖风险和清理步骤。

## 真实联调边界

自动主线使用 fixture 和 fake transport 完成，不依赖真实服务器。生产或准生产 FTP 联调仅作为可选人工增强：

- 使用独立测试目录，不直接指向线上站点根目录。
- FTP 账号仅授予该测试目录读写权限。
- 先执行普通 dry-run，再执行只读远端检查，再部署无业务副作用 fixture。
- 验证 deployment ID 回滚后目录内容哈希恢复。
- 密码只通过环境变量提供，不写入配置、日志、TODO 或验收报告。
- plain FTP 的明文传输风险必须由用户明确接受；服务器支持时优先 FTPS。

## 决策记录

| 日期 | 决策 | 原因 |
|------|------|------|
| 2026-07-10 | v0.2 使用 detached Git worktree，而不是复制当前工作区 | 保证构建输入严格对应提交，并隔离脏文件和 ignored 文件 |
| 2026-07-10 | 构建只在本地执行，远端只接收产物 | 同时兼容 FTP/FTPS/SFTP，避免生产服务器承担编译和依赖解析 |
| 2026-07-10 | artifact 使用 FROM/TO manifest 建立差量和漂移基线 | 生成文件不属于 Git blob，必须另建可验证来源 |
| 2026-07-10 | Git 源码与 artifact 合并成一个部署事务 | 部分成功会造成源码与依赖版本不一致，必须统一备份和回滚 |
| 2026-07-10 | 普通 dry-run 继续保持零连接、零构建、零写入 | `--dry-run` 是现有工具的重要安全能力，不能因 build 功能改变语义 |
| 2026-07-10 | v0.2 改为流式或 spool 上传 | `vendor/`、`dist/` 等目录可能很大，不能继续把所有目标 bytes 同时驻留内存 |
| 2026-07-10 | 数据库迁移保持人工独立流程 | 文件回滚无法可靠逆转 schema 和业务数据变更 |
