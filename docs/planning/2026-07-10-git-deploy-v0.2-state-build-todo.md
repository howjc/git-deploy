# git-deploy v0.2 期望状态与构建产物 TODO 清单

> 依据：`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md`（M1-M10）与 `docs/audit/2026-07-12-git-deploy-v0.2-plan-audit.md`。
> 当前基线：git-deploy v0.1.5；已有 deployment manifest/backup，但没有 current state、持久化合成 tree 或 transaction journal。
>
> **实施约定（硬性，随清单对实施者生效——实施者可能是 Claude、Codex 或其他 agent）：**
> 1. 每条完成后必须实际运行该条 DoD 命令，未运行不得标“已完成”。
> 2. DoD 无法执行（缺工具、缺授权、缺外部环境）时，该条标“受阻”，写 `tmp/agent-handoff/<task-id>/escalation.md` 留言（≤30 行、面向决策），暂停该条，禁止绕过。
> 3. 禁止手工编辑生成产物（lock 文件、构建输出等）；工具链缺失 = 受阻。`uv.lock` 只能由 `uv lock`/`uv sync` 等官方命令更新。
> 4. 连续 3 条任务无法按 DoD 验证时停止实施，等待用户决策。
> 5. 状态变化同步更新本清单状态列；同时“进行中”≤ 2 条，禁止批量翻状态。
> 6. 标注“由用户代验”的 DoD 项，实施者完成自动验证部分后将该条标“进行中”并列明待代验内容，不得自行标“已完成”。
> 7. 自动主线只使用临时 Git 仓库、fixture 和 fake/in-memory transport；真实 FTP/FTPS/SFTP 联调是独立人工增强，不得读取、输出或记录真实密码、私钥、token。

## 固定实施口径

- v0.2 的远端基线是 current snapshot，不是 `COMMIT^1` 或 `FROM..TO` 的 `FROM`。
- `COMMIT`/`FROM..TO` 只选择 first-parent patch；已成功应用的 transition ID 必须跨部署持久化并幂等去重，ID 绑定 commit + 第一父身份而不是纯 diff 内容。
- transition ID 是状态谱系中的一次性事件；Git revert 后重新引入旧改动必须提交 revert-of-revert/新 commit，只有 git-deploy rollback 才恢复该 deployment 引入前的 transition 集。
- 状态采用不可变 snapshot + content-addressed objects + generation current pointer；不得用一个可变 JSON 同时表示 current 和历史。
- `--force` 只能放行已确认的远端内容漂移，不能绕过 target identity、state hash、generation、锁或 transaction recovery 门禁。
- v0.2 只承诺单控制端本地排他锁；不得把实现描述为支持跨机器并发部署。
- 普通 `--dry-run` 只能只读已有状态；不得创建 worktree、构建、连接远端或写 state/CAS/journal/deployment。
- build runner 默认 `host`，可显式选择 `docker`；Docker 只负责在隔离 worktree 中生成文件产物，不构建/发布镜像，也不改变远端事务语义。
- Docker 模式不得回退宿主执行；只允许挂载隔离 worktree，不挂载仓库、home、SSH Agent、Docker socket 或 state/CAS，普通 dry-run 不 inspect/pull/run。
- Docker tag 在真实构建前解析为不可变 image ID；image identity、platform、network、pull policy 和 UID/GID 映射进入 build fingerprint，敏感环境变量值不得进入 argv、日志或状态。
- 1Password 首版只通过正式版 `op run` 解析配置中的 `op://` references；不得使用 `--no-masking`、`op read`/`inject`、beta Environments 或自动登录，host/Docker 子进程不得继承任何 `OP_*` 认证变量。
- 1Password 注入的构建固定禁用 build cache；自动测试只使用 fake `op` 和哨兵值，真实账号/vault/service account 是独立人工增强且不得输出任何敏感值。
- 注入等同于授权所选 revision 的构建代码读取秘密；执行摘要必须显示 target tree/命令/变量名和信任警告，不得把 runner 无泄漏测试表述为能阻止构建脚本把秘密写入 artifact。
- state 只表达期望，不能证明远端当前内容；真实 deploy 的重复 patch 必须只读校验相关远端路径后才能返回 `already deployed`。
- v0.2 的 state/CAS/Git objects 全部保留，不提供真实删除 GC；任何清理能力移至 v0.3。

## 实施优先级与阶段门禁

| 优先级 | 阶段 | 任务范围 | 交付目标 |
|---|---|---|---|
| P0 | Gate A | T00、S/P/D/R/C（不含已取消 GC 实现）与 V2A | named remote→physical target、三层 fingerprint、可信 current state、事务恢复、最新回滚和状态运维闭环；不依赖构建工具/Docker/op |
| P1 | Gate B | B00H、B01/B01R/B02/B03/B04/B05/B06-B10、I02H/I02N/I02G/I03 与 V2B | host runner 独立完成 Composer/Node/Go artifact 的构建、差量、流式部署和回滚 |
| P1 | Gate C | B00D/B00O、B01D/B01O/B01DR/B01OR、B03D/B03E/B03O/B03OD、B05D/B05O、B10D/B10O、I02/I02D/I02O 与 V2C | Docker 真实 daemon 与 1Password fake-contract 汇合，不反向阻塞 Gate A/B |
| P2 | GA | I01/I04/I05；U01-U03 为 GA 后可选增强 | project × remote 隔离、迁移/安全文档、完整自动门禁；真实远端、跨平台 Docker 和真实 vault 不反向阻塞自动 GA |

`R02` 在 v0.2 改为“拒绝非最新回滚”的兼容门禁；派生回滚能力移至 v0.3。`G01`/`G02` 的真实删除 GC 已取消并移至 v0.3；v0.2 仅由 `C06` 固化“无删除入口、对象全部保留”的契约。

## A. 环境与契约

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| T00 | Gate A 核心环境预检 | 实施环境、`pyproject.toml` | 记录 `python3 --version`、`git --version`、`uv --version`；执行 `uv run pytest -q`、`uvx ruff check src tests`、`uvx ty check src`、`uv build --clear` 全部通过；不要求 Composer/Node/Go/Docker/op | — | 待办 |
| S01 | 固化 named remote→physical `target_id` 契约 | `config.py`、`models.py`、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k 'target_id or remote_identity'` 通过；target ID 绑定规范化 endpoint/project/remote_root 且不含 alias/凭据；两个 alias 指向同一物理目标时 ID 相同，不同 endpoint/root 时不同；显式 ID 冲突拒绝 | T00 | 待办 |
| S02 | 实现 physical target identity fingerprint | 新增 target identity 模块、`tests/test_target_identity.py` | `uv run pytest tests/test_target_identity.py -q -k physical` 通过；host/port/user/project/remote_root 变化改变 identity，alias/password/token/build 配置变化不改变 identity payload；默认 state root 为 `targets/<target-id>` | S01 | 待办 |
| S02P | 实现 managed-state policy fingerprint | target identity/policy 模块、`tests/test_target_identity.py` | `uv run pytest tests/test_target_identity.py -q -k managed_policy` 通过；repository identity、include/exclude/protected、artifact destinations 任一变化触发 policy mismatch，build command/image/secret reference 变化不触发 target/policy mismatch | S02 | 待办 |
| S03 | 冻结 immutable state schema v1 | `models.py`、新增 expected-state 模块、`tests/test_expected_state.py` | `uv run pytest tests/test_expected_state.py -q -k schema` 通过，覆盖 canonical JSON、state ID、generation、parent、source tree、transition IDs、physical target/policy fingerprint、file owner/content ref、未知 schema 拒绝 | S02P | 待办 |
| S04 | 扩展 deployment manifest lineage 并保持旧格式可读 | `models.py`、`state.py`、旧 manifest fixture | `uv run pytest tests/test_expected_state.py -q -k manifest_compat` 通过，v0.1.5 manifest 可 history/verify 且 lineage 明确为空；无 current 时 legacy rollback 可读，已有 current 时阻断；新 manifest 往返保持 before/after state 与 transaction ID | S03 | 待办 |
| S05 | 实现 SHA-256 content-addressed object store | 新增 object-store 模块、`tests/test_state_objects.py` | `uv run pytest tests/test_state_objects.py -q -k cas` 通过，覆盖原子写、重复写、权限、读取复算和篡改拒绝 | T00 | 待办 |
| S06 | 实现持久化 Git object store 与 alternate 校验 | Git 仓库适配、新增 state Git store、`tests/test_state_git_store.py` | `uv run pytest tests/test_state_git_store.py -q` 通过；合成 tree 在新进程中仍可读取，主仓库 index/ref/object 数不变化，alternate/repository identity 不匹配时阻断 | S02P, S05 | 待办 |
| S07 | 实现 content-addressed immutable state 文件 | `state.py`/新 expected-state store、`tests/test_expected_state.py` | `uv run pytest tests/test_expected_state.py -q -k immutable` 通过，覆盖 canonical hash、重复写、临时文件清理、读取复算和篡改拒绝 | S03, S05, S06 | 待办 |
| S08 | 实现原子 current generation pointer | expected-state store、`tests/test_expected_state.py` | `uv run pytest tests/test_expected_state.py -q -k current_pointer` 通过，覆盖 `os.replace`、generation CAS、旧 generation 拒绝、损坏 current 拒绝和失败后临时文件清理 | S07 | 待办 |
| S09 | 实现 `target_id` 本地排他锁 | 新增 target lock、`tests/test_state_lock.py` | `uv run pytest tests/test_state_lock.py -q` 通过；两个进程竞争同一 target 仅一个获锁，不同 target 不互阻，异常退出后锁可恢复 | S01, S08 | 待办 |
| S10 | 冻结 transaction journal 状态机 | 新增 transaction 模块、`tests/test_transaction.py` | `uv run pytest tests/test_transaction.py -q -k state_machine` 通过，非法状态迁移拒绝，journal 原子写并覆盖七种北极星状态 | S03, S08 | 待办 |

## B. 基于 current state 的源码规划

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| P01 | 定义 first-parent commit transition ID 与展开结果 | `gitrepo.py`、状态模型、`tests/test_state_composer.py` | `uv run pytest tests/test_state_composer.py -q -k patch_id` 通过，ID 绑定 object format/commit/第一父（root sentinel）；单提交、range、merge、范围重叠稳定有序，内容相同但 commit 不同的 revert-of-revert ID 不同 | S03, S06 | 待办 |
| P02 | 在 current source tree 上合成未应用 patch | `gitrepo.py`/新增 state composer、`tests/test_state_composer.py` | `uv run pytest tests/test_state_composer.py -q -k current_tree` 通过，fixture 明确断言 `B + D` 后选择 `E` 得到 `B + D + E` 且不引入 `C` | P01, S08 | 待办 |
| P03 | 实现 transition 重复与范围重叠幂等去重 | state composer、`tests/test_state_composer.py` | `uv run pytest tests/test_state_composer.py -q -k idempotent` 通过，重复 singleton/range、交叠 range 均不再次修改 tree；Git revert 后原 transition ID 仍视为已应用，新 revert-of-revert commit 可正常选择 | P02 | 待办 |
| P04 | 保留缺失依赖、分叉与合并冲突门禁 | state composer、Git fixture | `uv run pytest tests/test_state_composer.py -q -k 'conflict or diverge or dependency'` 通过，失败发生于远端连接前且不写 current/journal | P02, S10 | 待办 |
| P05 | 从 current/target tree 生成 source 文件状态差量 | planner、content provider、`tests/test_state_planner.py` | `uv run pytest tests/test_state_planner.py -q -k source_diff` 通过，新增、修改、删除、rename、mode、include/exclude、protected 路径均以 current tree 为 before | P04, S03, S05 | 待办 |
| P06 | 实现首次无状态的推断基线计划 | planner、state bootstrap service、`tests/test_state_bootstrap.py` | `uv run pytest tests/test_state_bootstrap.py -q -k inferred` 通过，普通/merge/root 首次计划与 v0.1.5 文件结果兼容，baseline first-parent ancestry 标为已应用；dry-run/失败不创建 generation | P05, S08 | 待办 |
| P07 | 实现 state identity、对象完整性和未完成事务前置门禁 | plan/deploy 入口、`tests/test_state_guards.py` | `uv run pytest tests/test_state_guards.py -q` 通过，target/config/repository mismatch、state/CAS 损坏、未完成 transaction 均阻断；`--force` 不放行 | P05, S08, S10 | 待办 |
| P08 | 实现基于 applied transition 集的无状态迁移计划 | planner、CLI plan/dry-run 输出、`tests/test_state_planner.py` | `uv run pytest tests/test_state_planner.py -q -k static_noop` 通过，重复 selectors 在 plan/dry-run 中标明“远端未校验”，不连接远端、不创建 deployment/state/journal | P03, P05 | 待办 |

## C. 状态化部署与恢复

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| D01 | 把远端漂移检查基线切换到 current snapshot | `executor.py`、remote checks、`tests/test_state_executor.py` | `uv run pytest tests/test_state_executor.py -q -k drift` 通过；actual=current 放行、actual=target 视为满足、第三种内容阻断，错误显示 current/target 语义 | P05, P07 | 待办 |
| D02 | 在远端 mutation 前 stage after state 与 prepared journal | executor、state/transaction store、`tests/test_state_executor.py` | `uv run pytest tests/test_state_executor.py -q -k prepared` 通过；stage 可完整解析，current pointer 未推进，任何 staging 失败时 transport 写调用为 0 | D01, S10 | 待办 |
| D03 | 成功部署后原子推进 current generation | executor、manifest/state store、`tests/test_state_executor.py` | `uv run pytest tests/test_state_executor.py -q -k commit_state` 通过；remote verify/hooks/health 后 current 从 before CAS 到 after，manifest lineage 与 journal 一致 | D02, S09 | 待办 |
| D04 | 自动恢复成功时保持 before state | executor recovery、`tests/test_state_executor.py` | `uv run pytest tests/test_state_executor.py -q -k auto_restore_state` 通过；上传、删除、hook、health 任一点失败且恢复成功后 bytes 与 current generation 都等于 before | D03 | 待办 |
| D05 | 自动恢复失败时进入人工恢复门禁 | executor recovery、transaction CLI guard | `uv run pytest tests/test_state_executor.py -q -k manual_recovery_required` 通过；journal 保留证据，current 不伪推进，下一次 deploy 明确阻断 | D04, P07 | 待办 |
| D06 | 实现重复 patch 的只读远端 no-op 验证 | executor remote checks、`tests/test_state_executor.py` | `uv run pytest tests/test_state_executor.py -q -k repeated_noop` 通过；匹配 current 时返回 `already deployed` 且写/backup/hook/health 为 0，第三种内容按 drift 阻断 | D01, P08 | 待办 |
| D07 | 实现部分 target 满足的 effective plan 过滤 | executor effective plan、`tests/test_state_executor.py` | `uv run pytest tests/test_state_executor.py -q -k partial_target` 通过；只备份和变更尚未满足路径，manifest snapshots 只含 mutation 路径且 after state 表达完整 target | D03, D06 | 待办 |
| D08 | 实现新 target 的 state-only transition | executor/state transaction、`tests/test_state_executor.py` | `uv run pytest tests/test_state_executor.py -q -k reconciliation` 通过；覆盖“远端已达 target”和“managed diff 为空”两类，远端写/backup/hooks/health 均为 0，generation CAS 推进并保存审计记录；未完成 transaction 时阻断 | D03, D07, S10 | 待办 |
| D09 | 实现 transaction crash recovery 决策器 | transaction recovery service、`tests/test_transaction_recovery.py` | `uv run pytest tests/test_transaction_recovery.py -q` 通过，分别注入 prepared、remote_mutating、remote_verified、state_committed 崩溃点并断言自动恢复/完成或人工门禁 | D03, D04, D05, D08 | 待办 |
| R01 | 最新 deployment 回滚同步恢复 current state | rollback executor、`tests/test_state_rollback.py` | `uv run pytest tests/test_state_rollback.py -q -k latest` 通过；远端 before bytes、source/artifact entries、applied transition IDs 和 generation 形成可审计新状态 | D09 | 待办 |
| R02 | 明确拒绝非最新 deployment 的状态化回滚 | rollback selector、`tests/test_state_rollback.py` | `uv run pytest tests/test_state_rollback.py -q -k non_latest` 通过；current 已建立时非最新 deployment 在远端连接/状态写入前拒绝并提示能力移至 v0.3；不得执行部分逆操作 | R01 | 待办 |
| R03 | history/verify 展示并校验 state lineage | CLI、state/history 输出、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k 'state_lineage or history or verify'` 通过，显示 remote alias、target ID、generation、before/after state；旧 manifest 输出 `state: legacy` | S04, R02 | 待办 |

## D. 状态运维 CLI

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| C00 | 增加 legacy/named-remote 历史迁移 dry-run 计划器 | state discovery、CLI、`tests/test_state_migration.py` | `uv run pytest tests/test_state_migration.py -q -k plan` 通过；发现旧 `<project>/deployments` 与 `<project>/remotes/<alias>`，按 physical target 分组，列出复制/冲突/共享；同 ID 内容冲突或两个 alias 状态不兼容时阻断且零写入 | S02, S04 | 待办 |
| C00A | 生成并校验 legacy/named-remote 迁移 staging | state migration、`tests/test_state_migration.py` | `uv run pytest tests/test_state_migration.py -q -k staging` 通过；按 C00 计划复制 manifest/backup 到隔离 staging，复算内容和冲突，失败删除 staging且不触碰 legacy/target-id 目录 | C00 | 待办 |
| C00B | 原子发布 legacy/named-remote 历史迁移 | state migration、target lock、`tests/test_state_migration.py` | `uv run pytest tests/test_state_migration.py -q -k publish` 通过；`--yes` 后在 target lock 下原子发布已校验 staging 并写迁移记录；失败保留旧目录且新目录不可见，不删除 legacy 证据 | C00A, S09 | 待办 |
| C01 | 增加只读 `state inspect` | CLI、expected-state store、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_inspect` 通过，显示 remote alias、physical target ID、policy fingerprint、generation、tree、patch、artifact、legacy migration 和未完成 transaction 摘要且不连接远端、不写状态 | P07, C00B | 待办 |
| C02 | 增加本地 `state verify` | CLI、expected-state/CAS/Git stores、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_verify_local` 通过，复算 current/state/CAS/Git objects 和 identity fingerprint；不连接远端、不修改损坏证据 | C01 | 待办 |
| C03 | 增加只读 `state verify --remote` | CLI、fake transport、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_verify_remote` 通过，检查 current snapshot 全部受管路径，报告 match/absent/drift；远端写调用和本地状态写调用均为 0 | C02, D01 | 待办 |
| C04 | 增加显式 `state bootstrap` | CLI、bootstrap service、fake transport、`tests/test_state_bootstrap.py` | `uv run pytest tests/test_state_bootstrap.py -q -k command` 通过，已知 revision/empty 两种模式先只读验证受管远端路径，再以 generation 1 写 current；unknown adopt 不受支持，dry-run 不写 | P06, D01, S09 | 待办 |
| C04P | 增加 managed-state policy 迁移 dry-run 计划器 | CLI、policy migration、fake transport、`tests/test_state_policy_migration.py` | `uv run pytest tests/test_state_policy_migration.py -q -k plan` 通过；include/exclude/protected/artifact destination 变化列出 old/new managed paths 与所需只读验证，普通 deploy 继续阻断且本地/远端写均为 0 | D01, S02P | 待办 |
| C04A | 执行 managed-state policy CAS 迁移 | policy migration、fake transport、target lock、`tests/test_state_policy_migration.py` | `uv run pytest tests/test_state_policy_migration.py -q -k execute` 通过；先只读验证 old/new managed paths，再以 generation CAS 写新 policy state且远端写为 0；漂移/并发/失败保持旧 generation | C04P, S09 | 待办 |
| C05 | 增加 `state recover` 决策入口 | CLI、transaction recovery、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_recover` 通过，可显示决策、执行可证明的 finalize/restore；无法自动决定时保持 `manual_recovery_required` 并给出不含敏感值的操作摘要 | D09 | 待办 |
| C06 | 冻结 v0.2 state object 全量保留契约 | CLI、state stores、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_no_gc` 通过；help 无真实删除入口，未知 `state gc` 明确拒绝；inspect/verify/recover 均不删除 state/CAS/Git/cache/deployment 文件 | C02, C05 | 待办 |

## E. 构建产物能力

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| B00H | Gate B host 构建工具链预检 | 实施环境、host fixture | 记录 `composer --version`、`node --version`、`npm --version`、`go version`；运行无网络 Composer/Node/Go 最小 fixture 命令；任一缺失只阻断 Gate B，不反向改变 Gate A 状态 | T00 | 待办 |
| B00D | Gate C Docker 环境预检 | 实施环境、Docker daemon | `docker --version`、`docker info`、`go version` 通过；用最小 Go helper 编译静态二进制并从本地生成 scratch image，运行后删除且不访问 registry；失败只阻断 Docker 分支 | T00 | 待办 |
| B00O | Gate C 1Password CLI 预检 | 实施环境、fake op fixture | `op --version` 通过，并运行完全本地 fake `op run` contract smoke；不要求真实登录/vault/token，失败只阻断 1Password 分支 | T00 | 待办 |
| B01 | 固化 host build/artifact TOML schema | `config.py`、模型、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k 'host_build or artifact'` 通过，覆盖默认/显式 host runner、argv、timeout、cwd、env allowlist、file/tree mapping 和非法路径 | B00H, S02P | 待办 |
| B01D | 固化 Docker build runner 配置契约 | `config.py`、build 模型、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k docker_build` 通过，覆盖 image、platform、`none/bridge` network、`never/missing` pull policy、默认值和非法 runner；Docker 配置不能注入任意 `docker run` 参数 | B00D, B01 | 待办 |
| B01O | 固化 1Password secret provider 配置契约 | `config.py`、build 模型、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k onepassword` 通过；只接受 env name→`op://` reference 映射，name 必须属于 `env_allowlist` 且不能以 `OP_` 开头；拒绝明文、空/重复名、未知 provider、beta environment ID 和输出 URI/值的配置摘要 | B00O, B01 | 待办 |
| B01R | 固化 project × remote 的 host build/artifact 覆盖规则 | `config.py`、build/artifact 模型、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k remote_host_build_override` 通过；project 配置为默认，remote 层对 host build/artifacts 整体替换且不隐式深合并；plan 可追踪来源 | B01, S01 | 待办 |
| B01DR | 固化 project × remote 的 Docker 覆盖规则 | `config.py`、Docker build 模型、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k remote_docker_override` 通过；remote Docker 配置整体替换且不影响 host 默认；缺失 remote override 时按明确继承规则解析 | B01R, B01D | 待办 |
| B01OR | 固化 project × remote 的 1Password 覆盖规则 | `config.py`、secret provider 模型、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k remote_onepassword_override` 通过；remote 1Password 映射整体替换，dev 不继承 prod reference，摘要只显示变量名/来源层 | B01R, B01O | 待办 |
| B02 | 从真实或持久化合成 tree 物化隔离 worktree | 新增 worktree manager、`tests/test_worktree.py` | `uv run pytest tests/test_worktree.py -q` 通过，current/target tree ID 精确一致，成功/异常/中断后清理，主工作区脏文件不进入 | P02, S06 | 待办 |
| B03 | 实现受限 host BuildRunner | 新增 build runner、`tests/test_build_runner.py` | `uv run pytest tests/test_build_runner.py -q -k host` 通过，覆盖 argv 无 shell、cwd、timeout 终止进程组、env 白名单、敏感值脱敏和非零退出；执行摘要警告 host 拥有当前用户 filesystem/network 权限 | B01R, B02 | 待办 |
| B03D | 实现 Docker CLI 命令与镜像身份适配器 | 新增 Docker build backend、`tests/test_docker_build_runner.py` | `uv run pytest tests/test_docker_build_runner.py -q -k 'command or image'` 通过；fake Docker 断言 inspect/pull policy、不可变 image ID、固定 worktree mount、UID/GID、platform/network、no-new-privileges，且无 home/repository/SSH Agent/socket/state mount 和任意参数注入 | B01DR, B02 | 待办 |
| B03E | 实现 Docker 构建生命周期与 BuildRunner 接线 | Docker build backend、runner facade、`tests/test_docker_build_runner.py` | `uv run pytest tests/test_docker_build_runner.py -q -k 'lifecycle or env or timeout'` 通过；成功/非零/超时/Ctrl-C 均按 stop/kill/remove 清理，产物归属当前 UID/GID，env 值不进 argv/log；清理失败阻断且远端调用为 0，绝不回退 host | B03, B03D | 待办 |
| B03O | 实现 host runner 的 `op run` 安全包装器 | secret runner、fake `op`、`tests/test_onepassword_runner.py` | `uv run pytest tests/test_onepassword_runner.py -q -k host` 通过；固定 `op run --` 且无 shell/`--no-masking`/read/inject，最小环境解析 reference，实际命令只收到声明变量并移除全部 `OP_*`；缺 CLI/认证/权限/非零退出在远端连接前失败且日志无 sentinel | B01OR, B03 | 待办 |
| B03OD | 将 `op run` 安全包装器接入 Docker runner | secret runner、Docker backend、`tests/test_onepassword_runner.py` | `uv run pytest tests/test_onepassword_runner.py -q -k docker` 通过；调用链为 op→受限 docker，Docker + 1Password 强制 digest 且显示 network/镜像信任摘要；Docker argv 只含 `--env NAME`，client/container 无 `OP_*`；失败/中断完成清理且远端调用为 0 | B03O, B03E | 待办 |
| B04 | 实现 artifact collector 安全边界 | 新增 artifact collector、`tests/test_artifacts.py` | `uv run pytest tests/test_artifacts.py -q -k collector` 通过，file/tree、mode/hash/size 正确，绝对路径、`..`、symlink、submodule、FIFO/socket/device 全部拒绝 | B01, B02 | 待办 |
| B05 | 实现 runner-neutral build fingerprint 与 artifact cache | build cache、CAS、`tests/test_build_cache.py` | `uv run pytest tests/test_build_cache.py -q -k core` 通过；tree/commands/cwd/timeout/lock/tool/artifact mapping/host runner 任一变化 miss，完全一致 hit；build fingerprint 变化不改变 target ID/policy fingerprint | B03, B04, S05 | 待办 |
| B05D | 实现 Docker build fingerprint conformance | Docker backend、build cache、`tests/test_build_cache.py` | `uv run pytest tests/test_build_cache.py -q -k docker` 通过；不可变 image ID、platform、network、pull policy、UID/GID 任一变化 miss；不改变 physical target/managed policy | B05, B03E | 待办 |
| B05O | 实现 1Password cache-bypass conformance | secret runner、build cache、`tests/test_build_cache.py` | `uv run pytest tests/test_build_cache.py -q -k onepassword` 通过；启用 1Password 时固定 bypass cache，state 只含 provider/变量名/不透明摘要，不含 reference URI/值；secret rotation 必定重新构建 | B05, B03OD | 待办 |
| B06 | 以 current artifact manifest 生成 target artifact 差量 | artifact planner、state schema、`tests/test_artifact_planner.py` | `uv run pytest tests/test_artifact_planner.py -q` 通过，新增、修改、删除、mode、首次 baseline build/bootstrap 和缺失基线阻断均覆盖 | B05, S08 | 待办 |
| B07 | 合并 source/artifact 计划并执行 owner 冲突门禁 | combined planner、`tests/test_combined_planner.py` | `uv run pytest tests/test_combined_planner.py -q` 通过，统一 current/target file state；source/artifact、artifact/artifact 目标路径冲突在远端连接前失败 | P05, B06 | 待办 |
| B08 | 把目标内容读取改为逐文件 provider/spool | planner/executor/content provider、`tests/test_streaming.py` | `uv run pytest tests/test_streaming.py -q` 通过；500 MB 多文件 fixture 断言额外峰值 RSS、单 chunk、spool 磁盘预算均不超过测试常量，成功/失败后临时文件清理 | B07 | 待办 |
| B09 | 将 source/artifact 纳入同一远端事务与 state 迁移 | executor、rollback、`tests/test_combined_transaction.py` | `uv run pytest tests/test_combined_transaction.py -q` 通过，任一 source/artifact 上传、删除、hook、verify 失败都恢复统一 before bytes 与 before state | B08, D09, R01 | 待办 |
| B10 | 增加 host build CLI 并保持 dry-run 边界 | CLI、README、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k 'host_build_command or host_build_dry_run'` 通过；host build 可写 cache 但不连接远端；普通 deploy dry-run 不创建 worktree/构建/写状态，执行前显示 target tree/命令和 host 权限警告 | B05, B09 | 待办 |
| B10D | 接入 Docker build CLI/dry-run | CLI、Docker backend、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k docker_build_command` 通过；显式 build/deploy 按策略 inspect/pull/run，普通 dry-run 调用为 0；缺 daemon 不回退 host | B10, B05D | 待办 |
| B10O | 接入 1Password build CLI/dry-run | CLI、secret runner、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k onepassword_build_command` 通过；dry-run 不调用 op，仅显示 provider/变量名/信任警告；真实执行不写 secret cache且 host/Docker 失败均发生在远端连接前 | B10D, B05O | 待办 |

## F. 已移出 v0.2 的状态对象回收

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| G01 | v0.3：state/CAS 引用可达性 GC 计划器 | expected-state/CAS/build-cache stores、`tests/test_state_gc.py` | 已移出 v0.2；后续以 current、transaction、deployment 和 build cache 引用设计 mark-and-sweep | — | 已取消 |
| G02 | v0.3：持久化 Git object store 安全回收 | state Git store、`tests/test_state_gc.py` | 已移出 v0.2；待 G01 恢复为后续迭代任务后再定义删除门禁 | — | 已取消 |

## G. 集成、文档与发布

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| I01 | 验证 project × named remote 的 target/state/lock/build/secret 隔离 | CLI、多项目多 remote fixture、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k remote_state_isolation` 通过；同一物理目标的两个 alias 共享 target/state/lock，不同目标隔离；一个目标失败不污染另一目标，dev 不继承 prod build/secret，`all --remote` 只操作所选 remote | B10O, S09 | 待办 |
| I02H | 增加 host Composer artifact fixture | host runner、锁定依赖 fixture、`tests/test_host_artifacts.py` | `uv run pytest tests/test_host_artifacts.py -q -k composer` 通过；无网络 fixture 覆盖 `vendor/` 新增、修改、删除、mode、cache hit/miss 和失败清理 | B10 | 待办 |
| I02N | 增加 host Node artifact fixture | host runner、无依赖 package-lock fixture、`tests/test_host_artifacts.py` | `uv run pytest tests/test_host_artifacts.py -q -k node` 通过；`npm ci && npm run build` 在无网络 fixture 生成 `dist/`，覆盖新增、修改、删除和失败清理 | B10 | 待办 |
| I02G | 增加 host Go binary artifact fixture | host runner、最小 Go module、`tests/test_host_artifacts.py` | `uv run pytest tests/test_host_artifacts.py -q -k go` 通过；无外部 module 的 `CGO_ENABLED=0 GOOS=linux GOARCH=amd64 go build` 生成可执行文件，覆盖 mode/hash/cache 和失败清理 | B10 | 待办 |
| I02 | 增加 official-v2 Docker Composer 构建 fixture 与样例配置 | `deploy.example.toml`、fake Docker fixture、`tests/test_official_v2_build.py` | `uv run pytest tests/test_official_v2_build.py -q -k docker` 通过，digest 固定的 Docker Composer 配置与 fake CLI 覆盖 `vendor/` 差量、漂移、no-op、失败恢复和回滚；不连接 registry/daemon | B10D | 待办 |
| I02D | 增加真实 Docker daemon 生命周期自动门禁 | 本地 scratch fixture image、`tests/integration/test_docker_build.py` | `uv run pytest tests/integration/test_docker_build.py -q` 通过；测试从本地静态 Go helper 构建 scratch image，不访问 registry，覆盖 mount、UID/GID、成功、非零、超时、Ctrl-C、stop/kill/remove 和零残留容器 | I02, B00D | 待办 |
| I02O | 增加 official-v2 1Password Composer auth fixture | 样例配置、fake `op` fixture、`tests/test_official_v2_build.py` | `uv run pytest tests/test_official_v2_build.py -q -k onepassword` 通过；COMPOSER_AUTH reference 经 fake op 注入 digest Docker，旋转哨兵值后 cache bypass，所有捕获输出/argv/state/manifest 不含 reference、token 或值 | I02, B10O | 待办 |
| I03 | 完成 FTP/FTPS/SFTP fake transport 端到端矩阵 | transport integration tests | `uv run pytest tests/test_transport.py tests/test_combined_transaction.py -q` 通过，三协议不访问真实服务器且断言相同 state/rollback 语义 | B09 | 待办 |
| V2A | Gate A：可信状态与事务阶段门禁 | state/source/transaction/rollback/CLI tests | `uv run pytest tests/test_target_identity.py tests/test_expected_state.py tests/test_state_*.py tests/test_cli.py -q -k 'not build'` 通过；不要求 Composer/Node/Go/Docker/op，legacy/alias 与 policy 迁移、P04 冲突、R02 非最新拒绝均被执行 | C03, C04, C04A, C05, C06, R03 | 待办 |
| V2B | Gate B：host artifact 阶段门禁 | host build/artifact/transport tests | `uv run pytest tests/test_worktree.py tests/test_build_runner.py tests/test_artifacts.py tests/test_build_cache.py tests/test_artifact_planner.py tests/test_streaming.py tests/test_combined_transaction.py tests/test_host_artifacts.py -q -k 'not docker and not onepassword'` 通过；不要求 Docker/op | I02H, I02N, I02G, I03 | 待办 |
| V2C | Gate C：Docker 与 1Password 阶段门禁 | Docker/op/config/CLI integration tests | `uv run pytest tests/test_docker_build_runner.py tests/test_onepassword_runner.py tests/test_build_cache.py tests/test_cli.py tests/test_official_v2_build.py tests/integration/test_docker_build.py -q -k 'docker or onepassword'` 通过；真实 Docker 不访问 registry，真实 1Password vault 不作为自动前置 | I02, I02D, I02O | 待办 |
| I04A | 编写 named remote/target identity 迁移文档 | `README.md`、迁移文档 | 文档示例覆盖旧 default/alias state discovery、dry-run、同物理 alias 合并、冲突阻断、staging、`--yes`、失败恢复和不删除 legacy 证据；命令与 C00/C00A/C00B help 一致 | C00B, S01 | 待办 |
| I04B | 编写 state bootstrap/policy/recover 运维文档 | `README.md`、状态运维文档 | 文档覆盖首次 bootstrap、identity mismatch、policy plan/execute、状态丢失、未完成事务、对象全量保留、非最新回滚拒绝和禁止常态化 `--force`；命令与 C01-C06 help 一致 | C03, C04, C04A, C05, C06, R02 | 待办 |
| I04C | 编写 host/Docker/1Password 构建安全文档 | `README.md`、构建文档 | 文档覆盖 project/remote override、host 用户权限、Docker image/digest/network 信任、op service account、cache bypass、dry-run 零调用和 artifact 无 DLP 保证；不含真实 reference/token/value | B10O | 待办 |
| I04 | 汇总 v0.2 CLI help 与迁移导航 | README 导航、CLI help snapshots | `uv run pytest tests/test_cli.py -q -k help` 通过；README 可从 named remote 部署入口导航到 identity/state/build/recover 文档，所有示例与 parser snapshot 一致 | I04A, I04B, I04C | 待办 |
| I05 | 执行 v0.2 GA 完整发布门禁 | 全项目、package metadata、`uv.lock` | Gate A/B/C 均已完成；`uv run pytest -q`、`uvx ruff check src tests`、`uvx ty check src`、`uv build --clear` 全部通过；在 `<项目根>/tmp` 隔离安装后完成 help、named remote、单/组合 revision、host/Docker/1Password dry-run smoke，fake Docker/op dry-run 调用均为 0 | V2A, V2B, V2C, I01, I04 | 待办 |
| U01 | 真实 FTP/FTPS 隔离目录人工增强验证 | 用户提供的非生产测试目录和最小权限账号 | **由用户代验**：先 dry-run，再只读 remote check，再部署 fixture、重复 no-op、回滚；记录文件哈希和清理结果，不记录密码；未执行不阻塞 I05 自动主线 | I05 | 待办 |
| U02 | 跨平台 Docker daemon 人工增强验证 | 用户提供的 Linux/macOS Docker 环境、预置 digest 镜像、临时仓库 | **由用户代验**：在自动 Linux 门禁之外验证目标平台的 UID/GID、network、超时/中断清理及 artifact hash；不挂载凭据、不连接远端，未执行不阻塞 I05 | I05 | 待办 |
| U03 | 真实 1Password CLI 最小权限人工增强验证 | 用户提供的测试 vault/item、桌面集成或只读 service account、临时仓库 | **由用户代验**：仅确认 `op run` 能向 host/Docker fixture 注入哨兵变量，构建进程/Docker 无 `OP_*` 且输出遮蔽；记录变量名、exit code 和清理结果，不读取/输出 reference、token 或 secret，未执行不阻塞 I05 自动主线 | I05 | 待办 |

## 审计整改追踪

| 审计发现 | 调整后的任务/决策 | 闭环信号 |
|---|---|---|
| P1-1 可选工具阻塞全局 T00 | T00 仅保留核心；B00H/B00D/B00O 分支预检 | V2A/B 不依赖 Docker/op，分支工具缺失只阻断对应 Gate |
| P1-2 host 主线依赖 Docker+1Password | B05 core、B05D、B05O；B10、B10D、B10O 分层 | B06/B09/B10 host 路径不经过 B03D/B03O |
| P1-3 P04/R02 未接发布门禁 | P05→P04；R02 改非最新拒绝；V2A→I05 | 依赖图 active terminal 仅 U01-U03 |
| P1-4 named remote 未入 v0.2 契约 | S01/S02、C00/C00A/C00B、B01R/DR/OR、I01 | 同物理 alias 共享 target/state/lock，dev/prod override 不串用 |
| P1-5 fingerprint 边界不清 | S02 physical、S02P managed policy、B05/D/O build | 三层字段和变化动作分别有测试 |
| P1-6 缺真实 Docker 自动门禁 | B00D、I02D、V2C | 本地 scratch image 覆盖生命周期且 I05 依赖 V2C |
| P1-7 单一超长发布路径 | V2A/V2B/V2C 内部门禁，I05 最终汇合 | 每个 Gate 可独立运行并保留结论 |
| P1-8 host/image 信任模型 | B03 host 权限警告、B03OD digest/network 摘要、I04C | 所有 build 显示信任边界，Docker+secret 强制 digest |
| P2-1 性能阈值不明确 | B08 固定 500 MB fixture 与 RSS/chunk/spool 测试常量 | 性能退化可量化比较 |
| P2-2 1Password cache 成本 | v0.2 保持 B05O 固定 bypass | 正确性优先，不在本轮扩大范围 |
| P2-3 GC/旧回滚优先级低 | G01/G02 已取消转 v0.3；R02 只做拒绝门禁 | v0.2 GA 无悬空承诺 |

## 完成门禁

- [x] 每条 TODO 都有可执行或可观察的 DoD。
- [x] 核心 T00 只覆盖 Python/Git/uv；Composer/Node/Go、Docker daemon、1Password CLI 分别由 B00H/B00D/B00O 预检，不互相阻塞。
- [x] 已声明北极星依据与 v0.1.5 当前边界。
- [x] 已包含验证纪律、受阻留言、禁止绕过、熔断、状态同步和用户代验约定。
- [x] 真实远端联调与 fake transport 自动主线分离，且明确敏感配置不得进入日志/文档/对话。
- [x] 各任务以单一产出为主，依赖显式且无循环依赖。
- [x] 状态基线任务位于构建产物任务之前。
- [x] Docker runner 已拆分配置、命令/镜像身份、生命周期接线、fingerprint、dry-run 和真实 daemon 自动门禁；跨平台 daemon 保持人工增强，依赖无环。
- [x] 1Password 已拆分配置、host 包装、Docker 接线、cache/dry-run、集成 fixture 和真实 vault 人工增强；自动主线不接触真实秘密。
- [x] named remote、physical target identity、managed-state policy 和 build fingerprint 已分层，并有 project × remote 隔离/迁移任务。
- [x] P04 冲突门禁、R02 非最新拒绝和 V2A/B/C 均接入 I05；真实 Docker daemon 是自动门禁，真实 1Password 保持人工增强。
- [x] G01/G02 明确标为已取消并移至 v0.3；v0.2 由 C06 固化无删除契约，不存在悬空发布承诺。

## 变更记录

| 日期 | 变更内容 | 原因 |
|---|---|---|
| 2026-07-10 | 首版：整合 current snapshot、revision patch 幂等、事务恢复与构建产物任务 | 解决重复选择性部署不能继续以 Git 父提交作为远端基线的问题 |
| 2026-07-12 | 将 Docker 构建沙箱拆入 B01D/B03D/B03E，并扩展 B05/B10/I02/I05/U02 | 用户要求构建产物阶段可选择在 Docker 中执行，同时保持 dry-run、凭据和远端事务边界 |
| 2026-07-12 | 将 1Password CLI 注入拆入 B01O/B03O/B03OD，并扩展 B05/B10/I02O/I05/U03 | 用户要求通过 `op run` 注入构建环境变量，同时保证 host/Docker、缓存、dry-run 和真实凭据边界可独立验证 |
| 2026-07-12 | 按审计重排 Gate A/B/C，拆分 T00/B05/B10，补齐 named remote、三层 fingerprint、P04/R02、真实 Docker 和语言 fixture | 修复审计报告 P1-1 至 P1-8，缩短可选能力对核心主线的阻塞链 |
| 2026-07-12 | G01/G02 取消并移至 v0.3；R02 改为 v0.2 非最新回滚拒绝门禁 | 优先交付可信状态、最新回滚和 artifact 主线，后置低收益高复杂能力 |
