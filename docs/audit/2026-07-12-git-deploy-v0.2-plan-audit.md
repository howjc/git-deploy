# git-deploy v0.2 迭代计划审计报告

## 1. 审计范围

- 审计对象：
  - `docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md`
  - `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md`
- 对照现状：当前分支 `agent/multi-remote-environments`，HEAD `ce6f7ca`，以及工作区中尚未提交的 1Password 规划增量。
- 对照实现：`src/git_deploy/config.py`、`src/git_deploy/state.py`、`README.md` 中已经落地的 named remote 契约。
- 方法：按正确性、安全、性能、兼容性、可维护性与测试可验证性审查；解析 TODO 依赖图，并运行当前基线测试与工具链版本检查。
- 量化结果：TODO 共 64 条，未知依赖 0，循环依赖 0；只有一个根任务 `T00`，最长依赖链 23 层；当前代码基线 `uv run pytest -q` 为 32 passed。

本报告审计“计划质量与预期收益”，不代表 v0.2 功能已经实现或验收通过。

## 2. 发现清单

### P0 阻断

无。

### P1 重要

#### P1-1：全局环境预检把可选工具变成全部状态任务的硬前置

- 位置：`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:37`，并受同文件 `:7-10` 的验证/熔断纪律约束。
- 问题：唯一根任务 `T00` 同时要求 Composer、Node/npm、Go、Docker 和 1Password CLI。按照清单纪律，任一工具缺失都会把 `T00` 标为受阻，继而阻断完全不需要这些工具的 `target_id`、CAS、current pointer 和 transaction 工作。
- 影响：可选 build runner/provider 反向阻塞核心状态模型；不同实施机很难只承担状态模块；熔断规则可能在真正开始编码前被触发。
- 建议：保留只覆盖 Python/Git/uv/基础回归的 `T00`；新增语言构建预检、`B00D` Docker 预检、`B00O` 1Password 预检，并只让相应实现任务依赖它们。Docker daemon 和真实 1Password 认证继续与纯版本检查分离。

#### P1-2：host artifact 主线错误依赖 Docker + 1Password 完整实现

- 位置：`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:103-104`。
- 问题：核心 `B05` build fingerprint/cache 依赖 `B03OD`，而 `B03OD` 又依赖 Docker runner 与 1Password wrapper。这样即使项目只使用默认 `runner=host` 且没有 secret provider，也不能继续 artifact manifest/diff 主线。
- 影响：计划宣称 Docker/1Password 可选，依赖图却把它们放到所有构建产物能力的必经路径；增加关键路径、集成爆炸半径和返工成本。
- 建议：把 `B05` 收敛为 runner-neutral 的 fingerprint/cache 核心，只依赖 host runner interface、collector 和 CAS；另设 Docker fingerprint conformance 与 1Password cache-bypass conformance 任务。`B06` 依赖核心 `B05`，最终发布门禁再汇合所有可选后端。

#### P1-3：两个承诺能力未接入发布依赖图

- 位置：冲突门禁 `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:56`，非最新回滚 `:76`，发布门禁 `:127`。
- 问题：依赖图显示 `P04` 和 `R02` 是无下游 terminal task；`I05` 不依赖它们。实施者可以沿依赖图完成 `I05`，同时让“缺失依赖/分叉/冲突门禁”或“较旧非重叠回滚”仍为待办。
- 影响：前者是远端写入前的正确性门禁，漏接可能允许错误 patch 计划进入部署；后者则导致北极星 M5/DoD 17 与发布状态不一致。
- 建议：让 `P05` 或 `P07` 显式依赖 `P04`；若 `R02` 保留为 v0.2 承诺，则让 `R03`/`I04`/`I05` 之一依赖它。若不希望它阻塞首版，应从 v0.2 DoD 移至后续迭代，而不是留在无下游分支。

#### P1-4：v0.2 规划尚未吸收已经落地的 named remote 契约

- 位置：北极星仅以“服务器身份 + 项目 + remote_root”定义目标（`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:8,57,196-197`）；TODO 的 identity 和隔离任务见 `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:38-39,122`。当前实现已在 `src/git_deploy/config.py:70-107,220-255` 解析 named remotes，并在 `src/git_deploy/state.py:23-26` 按 remote 隔离历史。
- 问题：计划没有冻结 `remote name`、规范化 endpoint、项目 remote overrides、`default_remote` 与 `target_id/state path` 的关系，也没有说明 build/Docker/1Password 配置能否被 dev/prod 分别覆盖。
- 影响：实施时可能出现 dev/prod 复用错误 current state、同一物理目标重复 bootstrap、prod secret reference 被 dev 继承，或 v0.1.5 named-remote 历史迁移后不可见。
- 建议：在 S01/S02 增加 named remote identity/迁移矩阵；明确“物理 target identity”和“用户 remote alias”是否一一对应；为 build、artifact、Docker、1Password 定义 base/remote override 规则；把 I01 扩展为 project × remote 的 state/lock/cache/journal/secret 隔离测试。

#### P1-5：target/config/build 三类 fingerprint 边界不够明确

- 位置：`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:57,196-216,240-250`；对应任务 `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:38-39,103`。
- 问题：文档同时使用 target identity、repository/config fingerprint、build fingerprint，但没有逐字段冻结归属。Docker image、构建命令或 1Password reference 变化应触发重新构建；不应被误判为物理目标改变而要求 state bootstrap。相反，remote root 或受管路径策略变化不能只做 cache miss。
- 影响：实现者可能把所有配置塞进一个 fingerprint，造成无谓 bootstrap；也可能漏掉受管路径/远端身份变化，错误复用 current state。
- 建议：冻结三层 canonical payload：① physical target identity（endpoint/project/root）；② managed-state policy（repository identity、include/exclude/protected、artifact destinations）；③ build fingerprint（tree、commands、runner/image、tool/lock、secret provider policy）。为每个字段写“变化后的动作”：继续、cache miss、阻断迁移或重新 bootstrap。

#### P1-6：Docker 对外宣称支持之前没有真实 daemon 自动门禁

- 位置：fake Docker fixture `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:98-99,123`；真实 daemon 仅作为发布后非阻塞人工项 `:129`；发布门禁位于 `:127`。
- 问题：fake CLI 能验证 argv 组合，却无法证明真实 Docker 的 mount ownership、UID/GID、signal/timeout、stop/kill/remove、网络和跨平台文件权限语义。
- 影响：最容易在真实环境失效的生命周期行为未进入 GA 门禁，可能出现 root-owned artifact、残留容器或 Ctrl-C 后继续运行构建。
- 建议：增加无需 registry/外部服务的真实 Docker 自动集成测试，使用预置或测试阶段本地构建的最小 image，至少覆盖成功、非零、超时、Ctrl-C、UID/GID 和容器清理，并让 I05 依赖。真实 1Password vault 仍可按外部系统策略保留为 U03 人工增强。

#### P1-7：一个版本承载 64 个任务和 23 层关键路径，缺少可发布切片

- 位置：里程碑 `docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:311-324`；唯一最终门禁 `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:127`。
- 问题：计划把持久化状态、patch composition、事务恢复、回滚、运维 CLI、artifact、streaming、Docker、1Password 和 GC 放在一个发布终点。虽然任务原子化良好，但价值直到很深的依赖链末端才统一释放。
- 影响：周期长、集成反馈晚；任一晚期 Docker/secret 问题可能推迟已经可用的 state correctness 能力；大范围重构同时发生会加大回归定位难度。
- 建议：至少增加三个内部 release gate：A）M1-M5 状态/事务闭环；B）host artifact + streaming；C）Docker + 1Password。GC、较旧 deployment 回滚可依据优先级放到 D 切片。若版本号必须保持 v0.2，也应使用 v0.2-alpha1/alpha2/rc 的明确退出条件。

#### P1-8：host runner 与 Docker image 的信任模型仍需更明确

- 位置：构建安全边界 `docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:272-288`，Docker image/tag 规则 `:202-204`，完成定义 `:368-380`。
- 问题：host runner 只是工作目录隔离，不是操作系统沙箱，所选 commit 的代码仍可访问当前用户能访问的 filesystem/network。Docker image ID 只能保证“同一内容”，不能证明镜像可信；当 network=bridge 且注入 1Password secret 时，恶意构建代码或镜像可外传秘密。
- 影响：用户可能把 detached worktree 误解为安全沙箱，或把 digest 误解为供应链认证。
- 建议：所有 build（不仅 secret build）执行前显示“不可信代码执行”警告；文档明确 host runner 拥有宿主用户权限。1Password + Docker 组合优先要求 digest、可信镜像来源和显式 network；可考虑默认只允许 Docker secret build，host secret build 需额外确认。镜像签名若不做，明确列为非目标而不是隐含保证。

### P2 建议

#### P2-1：性能 DoD 缺少可比较阈值

- 位置：streaming 任务 `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:106`，北极星 DoD 29 `docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:358`。
- 问题：“内存不随总大小线性累积”方向正确，但没有 fixture 规模、RSS 上限、单文件 chunk 或 spool 磁盘预算。
- 影响：不同实现都可能声称通过，性能回归难以量化。
- 建议：固定例如 500 MB 多文件 fixture、峰值额外 RSS 上限、chunk 上限和临时磁盘清理断言；把绝对值设为可按 CI 环境调整的测试常量。

#### P2-2：1Password 全量禁用缓存安全但成本偏高

- 位置：`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:209,245,378`，对应 `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:103,124`。
- 评价：v0.2 默认 bypass 是正确的保守策略，能避免为探测 secret rotation 而接触秘密值。
- 影响：Composer registry auth 这类只控制下载权限、不改变锁定输出的 secret 也会导致每次重建。
- 建议：v0.2 保持现状；后续只在可证明的 lockfile + immutable image 模式下研究显式 `auth_only` 策略，不应在本轮扩大范围。

#### P2-3：GC 和较旧回滚的收益低于核心状态/构建闭环

- 位置：GC `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:114-116`，较旧回滚 `:76`。
- 问题：两者都有价值，但实现与验证复杂度较高，不是解决当前“错误基线”和“不能部署构建产物”的最短路径。
- 影响：若资源受限，会稀释 M1-M8 的交付速度。
- 建议：保留设计接口和不可删除默认值；把真实删除 GC、较旧非重叠回滚作为独立切片。若仍承诺 v0.2 完成，则必须按 P1-3 接入门禁。

## 3. 功能收益分析

| 能力 | 主要收益 | 对当前痛点的改善 | 收益判断 |
|---|---|---|---|
| current snapshot + transition lineage | 让选择性部署基于真实期望状态，而不是猜测 Git 父提交 | 直接解决 `B + D` 后部署 `E` 的结构性漂移误报，减少常态化 `--force` | 极高，v0.2 的核心价值 |
| generation CAS + target lock + transaction journal | 把远端文件和本地状态推进变成可恢复事务 | 降低并发覆盖、半完成部署和崩溃后继续带病发布的概率 | 极高，生产安全收益 |
| current/target 漂移检查、no-op、partial target、reconciliation | 只改真正需要变化的路径，同时保留漂移门禁 | 减少重复上传、空 deployment、无意义 hooks/health 和人工判断 | 高，安全与效率双收益 |
| source + artifact 统一事务 | 支持 `vendor/`、`dist/`、Go binary，并与源码共同回滚 | 从“只传 Git 文件”升级为可覆盖常见真实发布形态 | 极高，扩大适用范围 |
| detached worktree | 构建输入精确绑定 target tree，不受主工作区脏文件影响 | 提升可复现性，避免误把本地 `.env`/ignored 文件带入发布 | 高 |
| Docker build runner | 固定构建工具链与平台，减少宿主污染 | 改善开发机/CI 差异，尤其适合 Composer、Node、Go 交叉构建 | 中高；需真实 daemon 门禁 |
| 1Password `op run` | secret 只在子进程生命周期存在，避免明文配置和日志 | 降低 registry token、Composer auth 等凭据落盘风险，便于最小权限和轮换 | 中高；不能替代对构建代码的信任 |
| named dev/prod remotes | 同一项目快速切换测试与生产且隔离历史 | 降低复制配置和误选服务器成本 | 高；必须补齐 v0.2 identity/override 契约 |
| streaming/spool | 大目录/大二进制不全部驻留内存 | 提升 vendor/dist/二进制部署的规模上限 | 高，属于 artifact 能力的必要条件 |
| state inspect/verify/bootstrap/recover | 状态损坏和崩溃不再只能手工猜测 | 提升可观测性、故障定位和恢复可操作性 | 高 |
| GC | 控制长期 object/cache 增长 | 降低本地磁盘长期膨胀 | 中，适合后置切片 |
| 较旧非重叠回滚 | 增强历史修复灵活性 | 可撤销较早且未被覆盖的部署 | 中，但复杂度高于最新回滚 |

整体收益判断：方向正确且价值显著。最值得优先兑现的是“可信 current state + 可恢复事务 + 构建产物统一部署”；Docker 和 1Password 是增强可复现性与凭据治理的第二层价值；GC 与复杂历史回滚可后置。

## 4. 测试与可验证性评估

- 优点：每条任务基本都有独立 pytest selector；状态损坏、generation 冲突、崩溃点、远端漂移、secret 泄漏和 dry-run 副作用均有明确断言，Mock/fixture 与真实外部验证边界总体清楚。
- 依赖图：64 tasks、0 unknown dependency、0 cycle，说明原子化和显式依赖基础较好；但 P1-1、P1-2、P1-3 显示“无环”不等于“发布门禁完整”。
- 当前基线：`uv run pytest -q` 通过，32 tests passed。Python、Git、uv、Composer、Node/npm、Go、Docker CLI、1Password CLI 均可执行；本次只检查版本，未连接 Docker daemon、1Password account 或任何远端。
- 缺口：真实 Docker 生命周期未进入自动发布门禁；真实 1Password 保持独立人工增强符合外部系统验证策略，但发布说明应区分 fake-contract coverage 与真实账号兼容验证。
- 本次规划文档通过 `git diff --check`；未运行任何真实远端写操作。

## 5. 建议实施顺序

1. 先修订 P1-1 至 P1-5：拆环境门禁、解耦可选 runner/provider、补齐 release dependencies、named remote 契约和 fingerprint 分层。
2. Gate A：完成 M1-M5，交付可信 state、选择性 patch、事务恢复和最新回滚闭环。
3. Gate B：完成 host artifact、collector、streaming、统一事务和 build CLI。
4. Gate C：完成真实 Docker 自动门禁、1Password fake-contract 主线和人工增强说明。
5. Gate D：根据资源决定是否把 GC 真实删除与较旧非重叠回滚纳入同一版本。

## 6. 总体评价

结论：**方向通过，但计划需修复 P1 后再按原依赖图全面实施。**

北极星对状态正确性、事务恢复、artifact 差量、Docker 与 1Password 安全边界的思考较完整，功能收益足以支撑下一迭代；主要问题不是目标错误，而是可选能力耦合进核心关键路径、named remote 尚未并入状态身份契约、发布门禁存在两个漏接任务，以及缺少可独立发布的切片。

建议审计判定：`pass-with-notes` 不足以覆盖当前依赖门禁缺口；在进入大规模实施前应按 P1 清单修订规划。修订不需要推翻架构，只需调整契约分层、依赖和发布切片。
