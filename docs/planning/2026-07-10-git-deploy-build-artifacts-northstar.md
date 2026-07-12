# git-deploy 期望远端状态与构建产物北极星

> 状态：下一迭代，待实施（目标版本 v0.2）。
> 依据：用户要求把“持久化期望远端状态快照”与 Composer `vendor/`、前端 `dist/`、Go 二进制等构建产物整合进下一迭代；当前 v0.1.5 实现核查：`gitrepo.py` 只在本次进程内读取真实提交或临时 Git index 合成快照，`state.py` 只保存逐次部署 manifest/backup，尚无跨部署的当前状态指针。

## 北极星目标

为每一个“部署目标”（服务器身份 + 项目 + `remote_root`）维护一份不可变的期望远端状态快照和一个原子 current pointer。后续 `--revisions` 不再把所选最早提交的父提交当作远端真实基线，而是把选中的 first-parent patch 应用到 current snapshot 的持久化源码 tree；远端漂移检查比较“远端实际内容”与“current snapshot 期望内容”，部署或回滚成功后再原子推进状态指针。

在该状态模型上，为目标 tree 物化隔离的 detached Git worktree，通过宿主进程或受约束的 Docker 容器执行本地构建，收集 Git 未跟踪的生成产物，并将“源码差量 + 构建产物差量 + 期望状态迁移”作为一个可校验、可备份、可回滚、可恢复的部署事务。连续范围的目标 tree 通常来自真实 commit；不连续选择和历史回滚后的目标 tree 可能只存在于工具自己的持久化 object store，不能假设它有仓库中的真实 commit ID。

目标完成后，以下场景应成为一等能力：

- 连续选择性部署：远端已组合应用 `B + D` 后，执行 `--revisions E` 会把 `E` 应用到 `B + D` 的当前状态，而不是错误假设远端完整等于 `D`。
- 幂等重试：同一批 commit patch 已记录在 current snapshot 时再次选择，不重复应用，也不创建空版本或执行钩子。
- 漂移保护：人工修改、错误目录、部分部署或状态丢失不会被“历史中存在 deployment ID”掩盖。
- 精确回滚：回滚既恢复远端真实 bytes，也产生一条新的状态迁移；不能只改 manifest 状态而遗留错误 current pointer。
- PHP/Composer：在目标提交中执行 `composer install --no-dev`，部署生成的 `vendor/`。
- Node/Vite：在目标提交中执行 `npm ci && npm run build`，部署生成的 `dist/`。
- Go：在目标提交中执行 `go build`，部署生成的 Linux 二进制。
- Docker 构建沙箱：项目可显式选择固定镜像，在只挂载隔离 worktree 的容器中执行同一组构建命令；git-deploy 仍只部署文件产物，不构建、发布或部署容器镜像。
- 1Password CLI 凭据注入：host/Docker runner 均可由 `op run` 在命令存活期间解析 `op://` secret references 并注入白名单环境变量；git-deploy 不读取、保存或打印秘密明文。
- 混合项目：同一次部署中既发布 Git 跟踪源码，也发布一个或多个构建产物目录或文件。
- FTP/FTPS/SFTP：构建始终在本地隔离工作区执行，远端只接收最终文件；FTP 不依赖远程 shell。

## v0.2 功能优先级与阶段门禁

v0.2 保留一个最终 GA，但按收益和风险拆成三个必须顺序通过的内部门禁；后一阶段失败不得推翻前一阶段已经验证的结论：

| 优先级 | 阶段 | 范围 | 阶段完成信号 |
|---|---|---|---|
| P0 | Gate A：可信状态与事务 | named remote/target identity、三层 fingerprint、current state、patch composition、漂移/no-op、crash recovery、最新部署回滚、state inspect/verify/bootstrap/recover | 不依赖任何语言构建工具、Docker 或 1Password 即可通过完整状态与 fake transport 门禁 |
| P1 | Gate B：host 构建产物 | detached worktree、host runner、artifact collector/manifest、streaming、统一 source/artifact 事务、Composer + Node + Go fixture、build CLI | host-only 配置可独立部署和回滚构建产物，不依赖 Docker/1Password 实现 |
| P1 | Gate C：Docker 与 1Password | Docker runner/fingerprint/真实 daemon 生命周期、`op run` host/Docker 包装、secret cache bypass、dry-run 与泄漏门禁 | Docker 真实 daemon 自动测试通过；1Password fake-contract 通过，真实 vault 保持独立人工增强 |
| P2 | GA 收口 | project × remote 隔离矩阵、协议矩阵、迁移/安全文档、完整发布门禁 | Gate A/B/C 全部通过后才允许 v0.2 GA |

显式 GC 删除和较旧非最新 deployment 的派生回滚移至 v0.3。v0.2 默认保留全部 state/CAS/Git objects，只实现最新成功 deployment 的状态化回滚，避免低优先级复杂能力拖慢核心收益交付。

## 当前边界

v0.1.5 的部署链路是：

1. 解析一个或多个 `COMMIT` / `FROM..TO` 选择器；不连续选择在临时 Git index 和临时 object directory 中合成目标树。
2. 通过 Git object database 直接读取目标文件 bytes，不 checkout，也不修改主工作区、分支、暂存区或仓库对象库。
3. 把最早所选提交的第一父提交推断为远端基线，校验本次待操作路径的 SHA-256。
4. 备份远端原文件。
5. 上传远端临时文件并 rename，最后处理删除。
6. 执行可用的远程钩子和健康检查；失败时自动恢复备份。

因此 v0.1.5 有以下确定限制：

- `DeploymentStore` 只有 deployment manifest 和 before backup，没有“当前期望状态”或 generation pointer。
- `DeploymentManifest` 只记录本次触碰路径，不能重建远端完整组合状态。
- 不连续选择生成的 target tree 位于临时 object directory，规划结束即清理，下一次部署不能以它为源码基线。
- 单提交固定以 `COMMIT^1` 为基线；远端已由多次选择性部署形成组合状态时会产生结构性误报，常规流程被迫依赖 `--force`。
- 同一 commit patch 缺少跨部署的已应用标识，无法可靠区分“安全重试”与“再次叠加补丁”。
- 部署成功、manifest 状态和未来 current pointer 尚无统一事务；进程崩溃后的恢复规则未定义。

- 只能部署 Git 已跟踪文件。
- 不会生成或部署被 `.gitignore` 排除的 `vendor/`、`dist/`、二进制等产物。
- 不能安全执行目标提交自己的 Composer、npm、Go 构建流程。
- 生成产物不属于 Git blob，无法直接得到 FROM 侧远端漂移校验基线。
- 当前上传前会把目标文件读入内存；大规模依赖目录不适合继续沿用全量内存模型。
- 同一 revision selection 的远端文件已全部等于目标快照时，v0.1.5 仍会生成一个 `snapshots=[]` 的 deployment ID，并继续执行 post commands/health，造成无效历史记录和无必要副作用。

## 核心原则

### 1. current snapshot 是唯一远端基线

- 每个部署目标必须有稳定 `target_id`；它只覆盖规范化协议端点、项目和远端根目录等物理身份字段，不得包含 remote alias、密码、私钥、token、build 或 secret 配置。
- named remote 是配置选择入口，不是物理目标身份。同一 endpoint/project/remote_root 即使通过两个 alias 访问，也必须共享 `target_id`、state 和锁；不同 endpoint 或 remote_root 必须隔离。alias 重命名本身不创建新目标。
- current snapshot 不可变，至少记录 schema version、state ID、generation、parent state ID、source tree ID、已应用 transition IDs、受管文件条目、artifact provenance、创建它的 deployment ID 和配置指纹。
- 受管文件条目至少记录远端相对路径、owner、存在性、SHA-256、字节数、可执行位和可解析 content reference。
- 普通部署只能通过 compare-and-swap 从读取到的 generation 推进 current pointer；并发或状态身份不匹配必须在远端写入前阻断。
- 第一次没有 current snapshot 时，允许按 v0.1.5 规则推断 Git 基线并完成远端哈希验证；第一次成功部署后必须落下 target snapshot。也提供显式 bootstrap/inspect/verify 入口处理状态迁移和丢失恢复。
- 状态缺失、损坏、repository identity 变化或 current tree/object 不可解析时默认阻断；不得静默回退到 Git 父提交或自动使用 `--force`。
- 本轮以单控制端、本地持久化状态为边界；跨机器共享状态和可靠远端分布式锁不作为隐含能力。

### 2. revision selectors 描述补丁，不描述远端基线

- `COMMIT` 仍表示该提交相对第一父提交的 patch；merge commit 以第一父提交为主线。
- `FROM..TO` 表示选择 first-parent 历史中 `FROM` 之后至 `TO` 的 patch，`FROM` 只决定选择边界。
- 选中的 patch 必须应用到 current snapshot 的 source tree，而不是把 `FROM` 或 `COMMIT^1` 当作当前远端整体状态。
- snapshot 必须记录状态谱系中已成功应用的 commit transition ID；ID 至少绑定 Git object format、commit object ID 和第一父 object ID（root 使用固定 sentinel），不得只用内容型 `git patch-id`。bootstrap/inferred baseline 把其 first-parent ancestry 标为已应用，重复选择和范围重叠时去重。
- commit transition ID 是一次性部署事件：Git 中后续 revert 不会让原 commit ID 自动变为“未应用”；如需重新引入已撤销改动，必须创建新的 revert-of-revert/新 commit。只有 git-deploy rollback 成功恢复 before state 时，才移除该 deployment 引入的 transition IDs。
- patch 依赖被省略且无法三方合并时继续在本地规划阶段失败，不得夹带未选择 commit 的完整文件内容。
- v0.2 只承诺最新成功 deployment 的状态化回滚：生成新的派生 snapshot，并同步恢复该 deployment 引入前的 patch 集。较旧非最新 deployment 回滚移至 v0.3。

### 3. Git tree 是唯一源码输入

- target tree 恰好对应真实 commit 时，可通过 `git worktree add --detach <path> <target-commit>` 创建构建目录。
- current state 为组合 tree、选择不连续或历史回滚后，必须从持久化 source tree 物化隔离 worktree，并校验最终 tree ID 与 deployment plan 的 target tree ID 完全一致。
- 不复制当前 working tree，不读取其中的未提交文件、ignored 文件或本地 `.env`。
- 构建产物必须位于该 detached worktree 内，并受路径边界检查。
- current 与 target 构建使用各自 state tree 内容，禁止用当前分支文件替代。

### 4. 构建与远端部署解耦

- Composer、npm、Go 等命令只以本机临时 worktree 为源码输入；执行后端可以是宿主进程或显式配置的 Docker 容器。
- Docker 只是本地构建执行后端，不改变 artifact manifest、远端传输、事务、回滚或状态语义；远端服务器不需要 Docker。
- Docker 后端只把 worktree 挂载为容器工作区，容器生成的文件必须回写该 worktree 后再由统一 artifact collector 收集。
- 1Password 是 BuildRunner 外层的可选 secret provider：host 模式包装实际 argv，Docker 模式包装受限的 `docker run --env NAME`；解析后的值不进入 Docker argv、容器配置摘要或父进程状态。
- FTP/FTPS/SFTP 只负责远端读写，不在 FTP 上模拟远程命令。
- SFTP 的 `post_commands` 仍是部署后操作，不承担生成构建产物的职责。
- 构建失败发生在任何远端写入之前，不得创建成功部署记录。

### 5. 产物也必须有状态基线

- 每次构建生成 artifact manifest，至少记录相对路径、SHA-256、字节数、可执行位、来源 tree、revision selectors 和 build fingerprint。
- target artifact manifest 决定上传后的目标状态。
- current snapshot 中的 artifact manifest 决定远端预期基线和产物删除集合；常规后续部署不得重新猜测或重建“FROM 状态”。
- 第一次没有 artifact 状态时，可在独立 worktree 中构建推断出的源码基线，或要求显式 artifact bootstrap；两种路径都必须在远端写入前明确显示。
- 不允许仅构建 target 后凭远端目录列表猜测删除项。

### 6. 一个部署事务，一套状态迁移和回滚快照

- Git 跟踪文件与 artifact 文件合并为单一 deployment plan。
- 同一远端路径只能由一个来源拥有；源码与 artifact 路径重叠必须在远端写入前拒绝。
- 所有待修改或删除的远端文件先备份，再开始上传。
- 上传/替换先执行，删除最后执行。
- 构建产物部署失败时，源码和产物必须一起恢复到部署前状态。
- 回滚仍以 deployment ID 为准，不要求重新构建历史提交。
- after snapshot 必须在远端写入前完成 staging，但 current pointer 只能在远端文件、post commands 和 health checks 全部成功后推进。
- 进程在“远端已验证、current pointer 尚未推进”之间崩溃时，transaction journal 必须让下一次命令明确完成提交或恢复 before 状态，禁止继续新部署。
- 自动回滚成功时 current pointer 保持 before state；自动回滚失败时 transaction 标为需要人工恢复，不得伪装为可继续状态。

### 7. 保留 dry-run 的零副作用语义

- 普通 `deploy --dry-run` 只解析配置、Git diff 和预期构建步骤。
- 普通 dry-run 不创建 worktree、不执行包管理器、不连接远端、不写 deployment state。
- 普通 dry-run 可以只读 current snapshot，但不得推进 generation、写 transaction journal、补齐 object cache 或执行 bootstrap。
- `--dry-run --check-remote` 只检查已有可用基线；不得为了远端检查隐式执行构建。
- 新增显式本地构建验证入口，例如 `git-deploy build PROJECT --revisions COMMIT`；它可以创建临时 worktree 和产物 manifest，但绝不连接或修改远端。
- CLI 的最终命名可在原子 TODO 的配置契约任务中固定，但不得改变上述副作用边界。

### 8. 目标状态去重，不按 revision selection 字符串去重

- plan/普通 dry-run 先根据 current snapshot 的已应用 patch 集和 target tree 报告“无状态迁移”；它们不连接远端，不能宣称远端已经部署完成。
- 真实 deploy 即使没有待应用的新 patch，也必须只读校验这些 selectors 涉及的 current 路径；全部匹配 current snapshot 后才返回明确的 `already deployed`，发生漂移则阻断。
- `already deployed` 不创建 deployment ID、不创建 state/journal、不保存空 manifest、不执行远端写入、post commands 或 health checks。
- current snapshot 尚未推进、但远端所有计划路径已经精确等于 target 时，执行 state-only reconciliation：不上传/删除、不创建 deployment backup、不运行 post commands/health，但以 CAS 推进 generation 并保存 `reconciled` 审计记录。
- target source tree/transition lineage 发生变化但 managed remote diff 为空时，也执行本地 state-only transition；不连接远端，但必须推进 generation，避免后续 patch 仍基于过期 source tree。
- 存在未完成 transaction 时禁止普通 reconciliation，必须先由 recovery 决策器处理，避免把崩溃窗口误判成人工外部部署。
- 同一组 selectors 如果远端被回滚、人工修改或只完成部分文件，不得仅因历史中存在相同选择记录而跳过。
- 部分路径已等于 target snapshot 时，仅把剩余需要修改或删除的路径纳入 effective plan、before snapshot 和 deployment manifest；已满足路径不重复上传，也不进入回滚快照。
- `DELETE + 远端已不存在` 与 `UPLOAD + 远端哈希已等于 target snapshot` 都属于目标状态已满足。
- 远端内容既不等于 current snapshot、也不等于 target snapshot 时仍是 drift，默认阻断；去重不得弱化漂移保护。
- 普通本地 dry-run 因不连接远端，只能显示静态计划，不能宣称 `already deployed`；只读远端检查可以报告目标状态，但不得写部署记录。

## 期望状态契约

本地状态目录采用不可变对象 + 原子指针，不把一个可变 JSON 同时当历史和 current：

```text
<local_state_dir>/targets/<target-id>/
  current.json
  states/<state-id>.json
  objects/sha256/<prefix>/<digest>
  git/objects/...
  transactions/<transaction-id>.json
  deployments/<deployment-id>/manifest.json
```

- `current.json` 只保存 schema version、`target_id`、generation 和 current state ID，通过同目录临时文件 + `os.replace` 原子更新。
- `states/<state-id>.json` 是 canonical JSON 的内容寻址不可变快照；同一 state ID 内容不一致必须视为损坏。
- `objects/sha256` 保存 artifact、spool 和无法由持久化 Git tree 解析的受管文件 bytes；写入后必须复算哈希再发布。
- `git/objects` 保存合成 source tree 所需的新 Git objects，并以项目 Git object database 作为只读 alternate；current source tree 无法解析时阻断。
- `transactions` 是崩溃恢复 journal，至少区分 `prepared`、`remote_mutating`、`remote_verified`、`state_committed`、`reconciled`、`recovered`、`manual_recovery_required`。
- deployment manifest 增加 before/after state ID、before/after generation、引入的 transition IDs、配置/目标指纹和 transaction ID；旧 manifest 继续可 history/verify，但没有 state lineage 时不得伪造。legacy rollback 只允许在 current state 尚未建立时执行，建立后必须阻断并引导显式迁移/bootstrap。
- v0.2 不自动清理也不提供真实删除 GC；state/CAS/Git objects 全部保留。v0.3 再基于 current pointer、未完成 transaction、可回滚 deployment 和有效 build cache 设计 mark-and-sweep。
- 文件 owner 至少区分 `source` 与具体 artifact mapping；同一路径 owner 冲突在计划阶段阻断。
- 密码、私钥、token、环境变量值和远端文件明文不得进入 state JSON；需要持久化的 bytes 只进入权限受控的 object/backup 文件。

## 目标配置契约

配置需要表达构建命令、受控环境变量和产物映射。建议契约如下，最终字段名由实现前的 schema 测试固化：

```toml
[projects.official-v2]
repository = "."
remote_root = "/"
target_id = "official-v2-prod"
include = ["app/**", "config/**", "public/**", "route/**", "view/**"]

[projects.official-v2.build]
runner = "docker"
commands = [
  ["composer", "install", "--no-dev", "--classmap-authoritative", "--no-interaction"],
]
timeout = 900
env_allowlist = ["COMPOSER_AUTH"]

[projects.official-v2.build.docker]
image = "composer@sha256:<immutable-digest>"
platform = "linux/amd64"
network = "bridge"
pull_policy = "missing"

[projects.official-v2.build.onepassword.env]
COMPOSER_AUTH = "op://build/composer/auth"

[[projects.official-v2.artifacts]]
source = "vendor"
destination = "vendor"
kind = "tree"
```

配置规则：

- `target_id` 可显式配置；省略时由不含凭据的规范化服务器端点、项目名和 `remote_root` 计算。remote alias 只用于选择/显示，不进入默认 `target_id`；解析结果必须显示在 plan/history/state 命令中。
- 当前 v0.1.5 的 `<project>/remotes/<alias>` 历史迁移到 target-id 目录必须显式执行：同一物理目标的多个 alias 不能静默各自建立 current state；alias 重命名不能丢失历史。
- 项目级 build/artifact 配置可作为公共默认；`projects.NAME.remotes.REMOTE` 可整体覆盖 build、artifacts、Docker 和 1Password 配置，不做数组/命令的隐式深合并。plan 必须显示最终来源层级，避免 dev 继承 prod secret reference。
- 配置身份分三层：physical target identity 变化要求迁移/bootstrap；managed-state policy（repository identity、include/exclude/protected、artifact destinations）变化默认阻断并要求显式迁移；build fingerprint 变化只触发重新构建/cache miss，不改变 `target_id`。
- 命令优先使用 argv 数组，不默认经过 shell 解释。
- 确需 shell 管道或变量展开时必须显式启用，并在 plan 中标明。
- `env_allowlist` 只声明允许继承的变量名；变量值不得进入日志、manifest 或部署历史。
- `runner` 只允许 `host`（默认）或 `docker`；选择 `docker` 时必须配置 image，宿主执行模式不得静默回退。
- Docker image 可写 tag 或 digest，但真实构建前必须解析出本地不可变 image ID；构建指纹记录 image ID，tag 指向变化必须 cache miss。生产样例优先使用 digest。
- Docker `platform`、`network`（首版仅 `none`/`bridge`）和 `pull_policy`（`never`/`missing`）必须显式进入配置契约；默认 `network=none`、`pull_policy=never`，避免普通构建隐式联网或拉取镜像。
- `pull_policy=missing` 只允许显式 build/deploy 在远端 mutation 前拉取缺失镜像；普通 plan/dry-run 不得执行 `docker inspect`、`docker pull` 或 `docker run`。
- `build.onepassword.env` 是环境变量名到 `op://` secret reference URI 的映射；变量名必须同时存在于 `env_allowlist`，禁止 `OP_*`、空名、重复名和非 secret-reference 明文值。
- 1Password CLI 只允许固定的 `op run -- ...` 执行路径，禁止 `--no-masking`、shell 拼接、`op read` 明文回传、`op inject` 落盘和自动 `op signin`；缺少 CLI/认证/权限时在构建及远端连接前失败。
- 交互式本地使用可复用已认证 CLI/桌面集成；CI/共享环境优先使用仅授权所需 vault 的 service account。`OP_SERVICE_ACCOUNT_TOKEN`、`OP_CONNECT_*`、`OP_SESSION_*` 等认证变量只提供给 `op`，启动实际构建命令前必须全部移除。
- 首版只支持正式版 CLI 的 `op://` secret references，不依赖仍处于 beta 的 1Password Environments `--environment` 参数；后者待官方稳定后另行扩展。
- 使用 1Password 注入的构建在 v0.2 默认禁用 build cache，因为 secret rotation 不应通过记录或哈希秘密值来探测；不得提供绕过开关伪装为可安全缓存。

1Password 执行契约依据官方文档：[`op run` command reference](https://www.1password.dev/cli/reference/commands/run)、[Load secrets into the environment](https://www.1password.dev/cli/secrets-environment-variables) 和 [Service Accounts](https://www.1password.dev/service-accounts)。规划只依赖正式版 secret-reference 行为；官方标记为 beta 的 Environments 参数不进入 v0.2 契约。
- artifact `source` 必须是 worktree 相对路径，禁止绝对路径、`..`、symlink 逃逸。
- artifact `destination` 必须位于项目 `remote_root` 下。
- `kind=file` 与 `kind=tree` 均需支持。
- artifact 路径必须继续遵守 `.env`、私钥、证书和项目 `protected` 规则。
- build 配置变化必须改变 build fingerprint，禁止错误复用旧缓存。

## 目标架构

| 组件 | 责任 |
|------|------|
| `TargetIdentity` | 从规范化 endpoint/project/remote_root 生成稳定物理 `target_id`，让同一目标的多个 alias 共享 state/lock |
| `ManagedPolicyFingerprint` | 覆盖 repository identity、受管源码策略和 artifact destinations；变化时阻断并要求显式迁移 |
| `ExpectedStateStore` | 读写不可变 state、CAS bytes、持久化 Git objects 和原子 current generation pointer |
| `StateComposer` | 在 current source tree 上应用未应用的 commit patch，生成 target tree、patch 集和统一文件状态 |
| `TransactionJournal` | 记录远端事务阶段，启动时阻止带病继续并驱动完成提交或恢复 before state |
| `TargetLock` | 对同一 `target_id` 提供进程级排他锁和 generation compare-and-swap；明确不宣称跨机器分布式锁 |
| `WorktreeManager` | 从真实 commit 或合成 target tree 物化 detached worktree、校验 tree ID、处理并发锁、捕获中断并可靠清理 |
| `BuildRunner` | 统一宿主/Docker 后端，在 worktree 根目录执行 argv 命令、控制 timeout、环境变量白名单、日志脱敏和退出码 |
| `DockerBuildBackend` | 解析不可变 image identity，构造受限 `docker run`，管理 pull/stop/kill/remove 生命周期，并保证产物归属当前用户 |
| `OnePasswordRunWrapper` | 校验 secret references，以最小环境调用 `op run`，隔离/移除 `OP_*` 认证变量，并让 host/Docker 子进程仅获得声明的构建变量 |
| `ArtifactCollector` | 校验 artifact 边界，拒绝 symlink/submodule/特殊文件，生成文件级 manifest |
| `BuildCache` | 以 source tree + build fingerprint 缓存 manifest；缓存只用于优化，不改变正确性 |
| `CombinedPlanner` | 合并 current/target source diff 与 artifact diff，检测 owner/路径冲突并生成统一操作序列 |
| `DeploymentExecutor` | 按 current/target 状态检查远端并过滤 effective plan；以流式或 spool 方式执行远端事务 |
| `DeploymentStore` | 保留 deployment manifest 和精确 before backup，并关联 transaction 与 before/after state lineage |

## 构建指纹

build fingerprint 至少覆盖：

- 完整 baseline/target tree ID，以及原始 revision selectors 和解析后的提交序列。
- 构建命令 argv、工作目录、timeout 配置。
- 构建 runner；Docker 模式还覆盖解析后的 image ID、平台、网络、pull policy 和影响容器文件权限的用户映射。
- secret provider 类型和环境变量名；1Password reference 仅以不透明配置摘要参与 config fingerprint，不记录 URI 明文或解析值。启用 1Password 时 build cache 固定禁用。
- artifact source/destination/kind 配置。
- 允许继承的环境变量名称，不记录敏感值。
- 影响依赖解析的 lock 文件内容哈希，例如 `composer.lock`、`package-lock.json`、`uv.lock`、`go.sum`。
- `git-deploy` 工具版本和 artifact manifest schema 版本。
- 影响产物兼容性的目标平台声明，例如 `GOOS/GOARCH` 或 PHP platform 配置。

仅当指纹完全一致时才能复用构建 manifest。缓存命中不能跳过远端漂移检查。

三层 fingerprint 的变化动作固定如下：

| 层级 | 典型字段 | 变化动作 |
|---|---|---|
| physical target identity | protocol endpoint、project、remote_root | 阻断普通部署，要求迁移/bootstrap；不同 alias 指向同一物理字段时仍共享状态 |
| managed-state policy | repository identity、include/exclude/protected、artifact destinations | 阻断普通部署，要求显式 policy migration 后生成新 state |
| build fingerprint | source tree、commands、runner/image、tool/lock、secret provider policy | 重新构建或 cache miss；不得改变 target ID 或要求远端 state bootstrap |

## 产物差量规则

对 current 和 target artifact manifest 做路径级比较：

| 差异 | 远端操作 |
|------|----------|
| target 新增路径 | upload，预期 current 不存在 |
| 哈希或可执行位变化 | replace，预期远端匹配 current 哈希 |
| current 存在、target 缺失 | delete，预期远端匹配 current 哈希 |
| 内容和模式均相同 | 不产生操作 |

第一轮 artifact 部署若不存在可信 current manifest，必须选择以下显式路径之一：

1. 自动构建推断出的 source baseline，得到可验证 artifact 基线。
2. 用户使用明确的 bootstrap 模式，将当前远端文件作为 before snapshot，并在 plan 中标记无法证明其来源。

不得静默把“无 current artifact manifest”当成“远端不存在”。

## 安全要求

- 构建目标 tree 等同于执行所选提交组合中的代码，实际执行前必须显示项目、selectors、target tree ID 和命令摘要。
- detached worktree 只隔离输入，不是操作系统沙箱；host runner 仍拥有当前用户可访问的 filesystem/network 权限，所有 host build 都必须显示明确警告。
- 构建进程默认使用最小环境，不继承生产数据库密码、FTP 密码、SSH Agent socket或其他无关敏感变量。
- 允许的 registry token 仅按变量名透传，任何日志和异常消息都不得输出值。
- 不把项目 `.env`、用户 home 下凭据或现有 working tree ignored 文件复制进 worktree。
- worktree 和 artifact cache 权限默认仅当前用户可读写。
- 拒绝 artifact 中的 symlink、socket、device、FIFO 和路径穿越。
- 每项目设置构建超时；超时必须终止完整进程组并清理 worktree。
- Docker 后端不得挂载宿主仓库根目录、用户 home、SSH Agent、Docker socket、git-deploy state/CAS 或任意额外路径；唯一业务挂载是隔离 worktree 到固定容器工作区。
- Docker 容器默认禁用额外 capabilities、启用 `no-new-privileges`，以当前宿主 UID/GID 写产物，并使用隔离 HOME/临时目录；不允许 privileged、host network、host PID/IPC 或用户自定义任意 `docker run` 参数逃逸安全策略。
- Docker 构建超时或中断必须按 stop/kill/remove 顺序回收已命名容器；清理失败要保留不含敏感值的容器标识并阻断远端写入。
- Docker daemon 连接所需宿主环境与传给构建容器的 `env_allowlist` 分离；构建环境变量值不得出现在命令行、日志、fingerprint、manifest 或 state。
- image ID/digest 只证明内容身份，不证明镜像可信。Docker + 1Password 必须使用 digest 引用、显示镜像来源与 network 模式并要求显式确认；镜像签名验证不在 v0.2 保证范围。
- `op run` 必须保持默认输出遮蔽，git-deploy 自身也只能记录变量名和 provider 状态；错误、进度、traceback、子进程 argv、Docker inspect 和测试快照均不得包含 reference URI、service account token 或解析值。
- 启动 `op` 时只继承 CLI 正常运行所需的最小宿主环境、现有 `OP_*` 认证变量和待解析 reference；启动真实 host/Docker 子进程前移除所有 `OP_*`，且注入变量名以外的秘密不得继续传播。
- 自动测试使用 fake `op` fixture 和哨兵 secret，断言 argv/log/state/cache/Docker 参数均无泄漏；真实 1Password vault、账号、token 和 item 内容只做独立人工增强验证。
- 执行前必须显示将获得秘密的项目、target tree、命令摘要和变量名，并明确提示：被选提交中的构建代码一旦获得变量，就有能力主动读取或写入产物；git-deploy 只能约束自身和 runner 的泄漏，不能对不可信构建脚本提供 DLP 保证。
- 同一 repository/project 同时只能有一个构建部署事务，防止缓存和 worktree 互相覆盖。
- 每次 deploy/rollback/state mutation 都必须持有同一 `target_id` 的本地排他锁；检测到未完成 transaction 时只允许 inspect/recover，不允许新部署。
- state 文件和 object store 默认权限仅当前用户可读写；canonical JSON、content hash 和 generation 在读取时重新验证。
- 远端临时文件名加入 deployment ID 和随机因子，避免并发部署只依赖 PID 冲突。

## 非目标

- 不在生产服务器上执行 Composer、npm、Go 编译。
- 不自动执行数据库迁移或尝试回滚数据库 schema/data。
- 不构建、签名、推送或部署 Docker image；Docker 仅作为本机 artifact 构建沙箱。
- 不纳入 Docker Compose、Kubernetes、容器编排、运行时发布或远端 Docker daemon。
- 不管理 1Password vault/item、不同步秘密、不执行交互登录、不把 secret 注入远端 post command，也不支持将解析值写入 `.env`/配置文件/artifact。
- 不在 v0.2 依赖 beta 版 1Password Environments；只支持正式版 `op run` 可解析的 `op://` references。
- 不扫描 artifact 内容判断是否包含秘密，也不宣称能阻止恶意/失误的构建脚本把已注入值写入文件；秘密只应授予可信 revision，产物内容仍需项目自己的安全门禁。
- 不实现通用 CI/CD 平台、审批流或多阶段环境晋级。
- 不把生产 `.env`、证书、私钥打入构建产物。
- 不保证第三方包仓库永久可用；离线缓存和私有 registry 属于部署环境配置。
- 不在本轮支持 Git submodule 或 artifact symlink。
- 不在 v0.2 宣称支持多个控制端并发部署同一目标；跨机器状态同步、远端 lease 和分布式锁另立后续迭代。
- 不采纳来源不明的远端文件作为可信 current snapshot；v0.2 bootstrap 只接受已知 Git revision 或 empty 基线并先做只读验证，unknown adopt 不支持。
- 不在 v0.2 自动按天数/数量淘汰部署历史；自动 retention 策略需在显式 GC 和恢复测试稳定后另行设计。
- 不在 v0.2 提供 state/CAS/Git object 的真实删除 GC；所有对象默认保留，mark-and-sweep 移至 v0.3。
- 不在 v0.2 支持较旧非最新 deployment 的派生回滚；只支持最新成功 deployment，非最新回滚移至 v0.3。

## 里程碑

| 里程碑 | 目标 | 完成信号 |
|--------|------|----------|
| M1 | 目标身份与状态契约冻结 | named remote→physical target 解析、三层 fingerprint、state/current/transaction schema、manifest 兼容策略和 target-id 目录布局有解析及损坏测试 |
| M2 | 持久化 source tree 与状态对象 | 真实/合成 tree 可跨进程解析；CAS、canonical state ID、generation pointer 和本地锁通过原子性测试 |
| M3 | 基于 current state 的 revision 规划 | `B + D` 后选择 `E`、范围重叠、重复 patch、冲突、状态丢失等 fixture 全部通过 |
| M4 | 状态化部署、no-op、reconciliation 与漂移检查 | 远端只与 current/target 比较；重复选择经只读远端验证后零写入；远端已达新 target 时只产生可审计状态迁移 |
| M5 | 最新回滚与崩溃恢复 | 最新成功 deployment 回滚同步生成状态；关键崩溃点可恢复或明确进入 `manual_recovery_required` |
| M6 | 构建配置与隔离 worktree | current/target tree 均可物化；host/docker runner 契约冻结；成功、失败、超时、Ctrl-C 后清理；脏工作区不影响结果 |
| M7 | 本地构建、秘密注入、产物采集与缓存 | Composer + Node + Go fixture 覆盖 host，Docker runner/1Password wrapper 独立汇合；artifact manifest、image identity、secret 隔离、fingerprint、CAS 和安全边界有测试 |
| M8 | 统一差量、流式部署与事务回滚 | source/artifact owner 无冲突；大文件不全量驻留内存；任一步失败恢复 bytes 与 before state |
| M9 | CLI、迁移与 official-v2 样例 | state/build/inspect/verify/recover 入口、named remote 历史迁移、dry-run 边界、project × remote 隔离和 official-v2 Composer fixture 完整 |
| M10 | 发布门禁 | 单元/集成测试、Ruff、类型检查、uv build、隔离 uv tool install 全部通过；v0.1.5 manifest 可读 |

## 完成定义

必须全部满足：

1. `target_id` 不含任何凭据或 remote alias；两个不同 endpoint/remote root 不得静默复用 state，同一物理目标的多个 alias 必须共享 state/lock。
2. physical target、managed-state policy 和 build fingerprint 字段及变化动作完全分离；篡改 state、CAS bytes、generation 或 identity/policy fingerprint 时读取失败并阻断部署。
3. current pointer 使用原子 replace 和 generation compare-and-swap；两个本地并发部署至多一个进入远端 mutation。
4. current source tree 与它依赖的 Git objects 可跨进程解析；临时规划目录清理后仍能作为下一次 revision 基线。
5. 首次没有状态时沿用 v0.1.5 推断基线和远端哈希门禁；首次成功部署后创建 generation 1，失败或 dry-run 不创建 current state。
6. 已存在状态后，`COMMIT` 与 `FROM..TO` 都应用到 current source tree；`FROM` 不再被解释为远端整体基线。
7. fixture 证明远端状态为 `B + D` 时选择 `E` 生成 `B + D + E`，不会引入被跳过的 `C`，也不会要求远端完整匹配 `D`。
8. current snapshot 记录状态谱系中的 commit transition IDs；bootstrap 标记 first-parent ancestry，重复/重叠选择跳过，Git revert 后重新引入必须使用新 commit。
9. patch 依赖缺失、first-parent 历史分叉或三方合并冲突在连接远端前失败，current pointer 和 transaction 目录不变化。
10. 源码远端检查使用 current snapshot 的完整 SHA-256；实际内容等于 target 时视为已满足，既不等于 current 也不等于 target 时默认漂移阻断。
11. `--force` 只跳过远端内容漂移，不能跳过 state 损坏、target identity 不匹配、generation 冲突或未完成 transaction。
12. after snapshot 在远端 mutation 前完成 staging；只有远端 verify、post commands 和 health checks 全部成功后才能推进 current pointer。
13. 部署失败并自动恢复成功时 current pointer 保持 before state；自动恢复失败时 journal 进入 `manual_recovery_required`，后续 deploy 被阻断。
14. 在 prepared、remote_mutating、remote_verified、state_committed 各阶段模拟进程中断，下一次命令能确定恢复、完成提交或给出不可自动决定的明确状态。
15. deployment manifest 保存 before/after state lineage；v0.1.5 旧 manifest 仍可 history/verify，legacy rollback 只在尚无 current state 时保持旧行为，建立状态后明确阻断。
16. 最新 deployment 回滚后远端 bytes、current source/artifact state 和已应用 patch 集恢复到 before state。
17. v0.2 对非最新 deployment 回滚明确拒绝并提示该能力移至 v0.3；不得执行部分逆操作或伪造新 state。
18. current snapshot 已包含全部所选 patch 时，plan/dry-run 只报告无状态迁移；真实 deploy 只读校验相关 current 路径后才返回 `already deployed`，漂移仍阻断。
19. current 尚未推进但远端全部等于 target，或新 transition 只有 unmanaged source 变化时，执行 state-only transition；不修改远端、不创建 deployment backup、不运行 hooks/health，但生成可审计 state generation。
20. 部分路径已等于 target 时，manifest 只记录实际 mutation 路径；状态迁移仍准确表达完整 target snapshot。
21. 普通 `--dry-run` 只读已有 current state，不连接远端、不创建 worktree、不构建、不写 CAS/state/journal/deployment。
22. `--dry-run --check-remote` 只读检查已有 source/artifact 基线；缺少必须构建的 artifact 基线时明确报告，不能隐式构建。
23. detached worktree 计算出的 tree ID 始终等于计划中的 target tree ID；主工作区脏文件无法进入源码或 artifact。
24. 构建失败、超时和用户中断均发生在远端写入前，并可靠清理临时 worktree和完整进程组。
25. 构建命令默认不经 shell，环境变量按名称白名单继承，敏感值不进入日志、manifest 或 state。
26. artifact collector 拒绝绝对路径、父目录穿越、symlink、submodule 和特殊文件。
27. current/target artifact manifest 能确定新增、修改、删除、可执行位变化和 owner；无可信首次基线时必须构建 baseline 或显式 bootstrap。
28. source 与 artifact 远端路径冲突在任何远端写入前阻断；artifact 漂移和 `--force` 规则与 source 一致。
29. 上传采用逐文件流式读取或磁盘 spool；500 MB 多文件 fixture 的额外峰值 RSS、单 chunk 和 spool 预算由测试常量给出并断言，成功/失败后临时文件均清理。
30. source/artifact 部署中途失败时共同恢复真实 before bytes；回滚不要求重新构建历史产物。
31. FTP/FTPS 构建产物部署不依赖远程 shell；SFTP `post_commands` 保持可选。
32. project × named remote 矩阵按 physical target 隔离或共享 target ID、锁、state、worktree、构建指纹和 transaction；一个目标失败不得污染另一目标，dev 不得继承 prod secret/build override。
33. official-v2 fixture 使用锁定依赖执行 Composer 构建，验证 `vendor/` 新增、修改、删除、漂移、no-op 和回滚。
34. 自动测试只使用临时仓库、fixture 和内存/本地 mock transport；生产 FTP 账号和密码不进入自动验证。
35. 真实 FTP/FTPS 联调保持独立用户代验项，使用隔离测试目录、最小权限账号并记录清理结果。
36. legacy/alias history migration、`state inspect`、本地完整性 verify、只读 remote verify、显式 bootstrap、policy migration 和 recover 均有独立 CLI 契约；任何 dry-run/只读命令不得修改远端或状态。
37. unknown remote state 不支持静默 adopt；状态丢失时只能从已知 Git revision/empty 基线验证后 bootstrap，或由人工恢复流程决策。
38. v0.2 不提供真实删除 GC；所有 state/CAS/Git objects 默认保留，任何 CLI 都不得删除 current lineage、transaction、deployment backup/state 或 build cache 引用。
39. `runner=host` 保持宿主 argv 执行语义；`runner=docker` 必须使用配置镜像且缺少 Docker、镜像或 daemon 时在远端连接前失败，不得回退 host。
40. Docker 容器只挂载隔离 worktree，不能访问宿主仓库、home、SSH Agent、Docker socket、state/CAS 或任意未声明宿主路径。
41. Docker image tag 在执行前解析为不可变 image ID；image、platform、network、pull policy 或用户映射任一变化都会使 build cache miss；Docker + 1Password 必须使用 digest 并显示镜像/network 信任摘要。
42. Docker 容器以宿主 UID/GID 生成可采集产物；成功、非零退出、超时和 Ctrl-C 后容器均被删除，清理失败时远端写调用为 0。
43. Docker 构建变量仍按 `env_allowlist` 传入且值不出现在 argv、日志、fingerprint、manifest/state；Docker daemon 客户端环境不自动进入容器。
44. plan/普通 dry-run 只显示 Docker 构建摘要，不 inspect/pull/run；显式 build/deploy 仅按配置 pull policy 在远端 mutation 前解析或拉取镜像。
45. host/Docker runner 均可通过 `op run` 解析 `op://` references；git-deploy 配置只接受 reference URI，实际构建进程只得到声明在 `env_allowlist` 的变量名和值。
46. `op` 缺失、未认证、reference 不可读或权限不足时，在创建 transaction、连接远端或启动 Docker 容器前失败，且不得回退普通环境变量或明文配置。
47. `OP_SERVICE_ACCOUNT_TOKEN`、`OP_CONNECT_*`、`OP_SESSION_*` 等认证变量只对 `op` 可见；host 构建进程和 Docker client/container 环境均不存在任何 `OP_*` 变量。
48. `op run --no-masking`、`op read` 和 `op inject` 不在执行路径中；fake fixture 的 sentinel secret/reference/token 不出现在 argv、stdout/stderr、异常、progress、fingerprint、manifest、state 或 Docker inspect 参数。
49. 启用 1Password secret provider 的构建固定绕过 build cache；secret rotation 后必定重新执行构建，系统不读取或持久化秘密值来计算 cache key。
50. plan/普通 dry-run 只显示 `provider=1password` 和变量名，不执行 `op` 或认证；真实 vault/service account 验证是独立人工增强项，不阻塞 fake `op` 自动主线。
51. 执行前确认摘要明确列出 target tree、构建命令和注入变量名，并警告被选构建代码可读取这些值；自动测试只证明 git-deploy/runner 不泄漏，不把该结论扩张为 artifact DLP。
52. 所有 host build 都明确提示其拥有当前用户权限；真实 Docker daemon 自动门禁覆盖成功、非零、超时、Ctrl-C、UID/GID、mount 和容器清理，且不连接 registry。

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
| 2026-07-10 | v0.2 先落地持久化 current snapshot，再接构建产物 | 重复选择性部署和 artifact 差量都依赖可信的跨部署基线，不能继续以 Git 父提交代替远端状态 |
| 2026-07-10 | revision selectors 只表达待应用 patch，远端基线唯一来自 current snapshot | `B + D` 等组合状态不是任一原始 commit；混用“选择边界”和“远端基线”会制造结构性漂移误报 |
| 2026-07-10 | state 使用不可变快照、内容寻址对象和 generation pointer | 支持审计、幂等、回滚与崩溃恢复，避免一个可变文件同时承担历史和 current |
| 2026-07-10 | commit transition ID 绑定 commit 与第一父身份，不使用纯内容 patch-id | revert-of-revert 可能与原 diff 内容相同但必须是新的部署事件；直接重复旧 commit 才应幂等跳过 |
| 2026-07-10 | 重复 patch 的真实 deploy 仍执行只读远端校验 | current state 只是期望值，不能证明远端未被人工修改；no-op 不得掩盖漂移 |
| 2026-07-10 | current 落后但远端已精确达到新 target 时执行 state-only reconciliation | 既避免重复上传和空 deployment，又不能让 current pointer 永久停留在旧状态 |
| 2026-07-10 | v0.2 默认不自动淘汰状态，先设计引用可达性 GC | current、回滚、崩溃恢复和 build cache 共享对象；按时间盲删会破坏恢复能力 |
| 2026-07-10 | v0.2 只承诺单控制端本地锁，不伪装成跨机器分布式锁 | FTP/SFTP 缺少统一可靠 lease 原语；跨控制端并发需要独立协议和远端状态设计 |
| 2026-07-10 | `--force` 不得绕过状态完整性与事务门禁 | 强制覆盖只解决已确认的远端内容漂移，不能修复损坏状态、并发 generation 或半完成事务 |
| 2026-07-10 | v0.2 使用 detached Git worktree，而不是复制当前工作区 | 保证构建输入严格对应提交，并隔离脏文件和 ignored 文件 |
| 2026-07-10 | 构建只在本地执行，远端只接收产物 | 同时兼容 FTP/FTPS/SFTP，避免生产服务器承担编译和依赖解析 |
| 2026-07-10 | artifact 使用 current/target manifest 建立差量和漂移基线 | 生成文件不属于 Git blob，必须纳入持久化期望状态 |
| 2026-07-10 | Git 源码与 artifact 合并成一个部署事务 | 部分成功会造成源码与依赖版本不一致，必须统一备份和回滚 |
| 2026-07-10 | 普通 dry-run 继续保持零连接、零构建、零写入 | `--dry-run` 是现有工具的重要安全能力，不能因 build 功能改变语义 |
| 2026-07-10 | v0.2 改为流式或 spool 上传 | `vendor/`、`dist/` 等目录可能很大，不能继续把所有目标 bytes 同时驻留内存 |
| 2026-07-10 | 数据库迁移保持人工独立流程 | 文件回滚无法可靠逆转 schema 和业务数据变更 |
| 2026-07-10 | 重复部署按远端目标状态去重，不按 revision selection 或历史记录去重 | 相同 selectors 可能对应已部署、已回滚、部分部署或人工漂移；只有远端完整达到目标状态才是真正 no-op |
| 2026-07-10 | no-op 不创建空版本，也不运行 post commands/health | 空 deployment ID 无回滚价值，钩子和健康检查可能产生无必要副作用 |
| 2026-07-10 | 构建输入以 target tree ID 为准，不假设每个目标都有真实 commit | 不连续 revision selection 会产生合成 tree；构建必须与实际部署源码快照完全一致 |
| 2026-07-12 | Docker 作为可选本地构建 runner，不作为部署目标 | 隔离 Composer/npm/Go 工具链并提高环境一致性，同时保持 artifact/state/FTP/SFTP 事务模型不变 |
| 2026-07-12 | Docker 默认不联网、不拉镜像，执行时解析不可变 image ID | 防止 dry-run 或普通构建产生隐式外部副作用，并确保 tag 漂移正确触发 build cache miss |
| 2026-07-12 | 1Password 通过 `op run` 外层包装 host/Docker runner | 让解析值只存在于构建子进程生命周期，不由 git-deploy 读取或通过 Docker argv 传递 |
| 2026-07-12 | 1Password 注入构建默认禁用缓存，首版不接 beta Environments | secret rotation 无法在不接触秘密值的前提下可靠改变 cache key；优先采用正式稳定的 secret-reference 契约 |
| 2026-07-12 | v0.2 按 Gate A 状态、Gate B host artifact、Gate C Docker/1Password 分阶段收口 | 审计发现 64 个任务和 23 层关键路径需要可独立验证的收益切片 |
| 2026-07-12 | named remote alias 不进入 physical target ID，同一物理目标共享 state/lock | 防止两个 alias 指向同一 endpoint/root 时建立双状态和双锁并发写同一目标 |
| 2026-07-12 | fingerprint 分为 physical target、managed policy、build 三层 | 避免 build/image/secret 配置变化误触发 bootstrap，也避免远端/受管路径变化只产生 cache miss |
| 2026-07-12 | GC 真实删除与非最新回滚移至 v0.3 | 两者收益低于可信状态、最新回滚和 artifact 主线，后置可缩短 v0.2 关键路径 |
