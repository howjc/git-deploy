# git-deploy v0.2 期望状态与构建产物 TODO 清单

> 依据：`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md`（M1-M10）。
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
- state 只表达期望，不能证明远端当前内容；真实 deploy 的重复 patch 必须只读校验相关远端路径后才能返回 `already deployed`。
- state/CAS/Git objects 默认不自动淘汰；GC 必须按引用可达性工作，dry-run 是默认入口。

## A. 环境与契约

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| T00 | 环境预检：确认 v0.2 自动门禁工具链 | 实施环境、`pyproject.toml` | 记录 `python --version`、`git --version`、`uv --version`、`composer --version`、`node --version`、`npm --version`、`go version`、`docker --version`；执行 `uv run pytest -q`、`uvx ruff check src tests`、`uvx ty check src`、`uv build --clear` 全部通过 | — | 待办 |
| S01 | 固化 `target_id` 配置解析契约 | `config.py`、`models.py`、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k target_id` 通过，覆盖显式值、规范化默认值、非法值；输出中不含 password/token | T00 | 待办 |
| S02 | 实现非敏感 target/repository/config fingerprint | 新增状态身份模块、`tests/test_target_identity.py` | `uv run pytest tests/test_target_identity.py -q` 通过，断言 host/port/user/remote_root/include 变化会改变相应指纹，password 值变化不会进入或改变身份 payload | S01 | 待办 |
| S03 | 冻结 immutable state schema v1 | `models.py`、新增 expected-state 模块、`tests/test_expected_state.py` | `uv run pytest tests/test_expected_state.py -q -k schema` 通过，覆盖 canonical JSON、state ID、generation、parent、source tree、transition IDs、file owner/content ref、未知 schema 拒绝 | S02 | 待办 |
| S04 | 扩展 deployment manifest lineage 并保持旧格式可读 | `models.py`、`state.py`、旧 manifest fixture | `uv run pytest tests/test_expected_state.py -q -k manifest_compat` 通过，v0.1.5 manifest 可 history/verify 且 lineage 明确为空；无 current 时 legacy rollback 可读，已有 current 时阻断；新 manifest 往返保持 before/after state 与 transaction ID | S03 | 待办 |
| S05 | 实现 SHA-256 content-addressed object store | 新增 object-store 模块、`tests/test_state_objects.py` | `uv run pytest tests/test_state_objects.py -q -k cas` 通过，覆盖原子写、重复写、权限、读取复算和篡改拒绝 | T00 | 待办 |
| S06 | 实现持久化 Git object store 与 alternate 校验 | Git 仓库适配、新增 state Git store、`tests/test_state_git_store.py` | `uv run pytest tests/test_state_git_store.py -q` 通过；合成 tree 在新进程中仍可读取，主仓库 index/ref/object 数不变化，alternate/repository identity 不匹配时阻断 | S02, S05 | 待办 |
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
| P05 | 从 current/target tree 生成 source 文件状态差量 | planner、content provider、`tests/test_state_planner.py` | `uv run pytest tests/test_state_planner.py -q -k source_diff` 通过，新增、修改、删除、rename、mode、include/exclude、protected 路径均以 current tree 为 before | P02, S03, S05 | 待办 |
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
| R02 | 较旧非重叠 deployment 回滚生成派生状态 | rollback planner、`tests/test_state_rollback.py` | `uv run pytest tests/test_state_rollback.py -q -k non_latest` 通过；后续未触碰路径时允许逆操作，有路径重叠时远端写调用为 0 并明确阻断 | R01 | 待办 |
| R03 | history/verify 展示并校验 state lineage | CLI、state/history 输出、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k 'state_lineage or history or verify'` 通过，显示 target/generation/before/after state；旧 manifest 输出 `state: legacy` | S04, R01 | 待办 |

## D. 状态运维 CLI

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| C01 | 增加只读 `state inspect` | CLI、expected-state store、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_inspect` 通过，显示 target ID、generation、tree、patch、artifact、未完成 transaction 摘要且不连接远端、不写状态 | P07, R03 | 待办 |
| C02 | 增加本地 `state verify` | CLI、expected-state/CAS/Git stores、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_verify_local` 通过，复算 current/state/CAS/Git objects 和 identity fingerprint；不连接远端、不修改损坏证据 | C01 | 待办 |
| C03 | 增加只读 `state verify --remote` | CLI、fake transport、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_verify_remote` 通过，检查 current snapshot 全部受管路径，报告 match/absent/drift；远端写调用和本地状态写调用均为 0 | C02, D01 | 待办 |
| C04 | 增加显式 `state bootstrap` | CLI、bootstrap service、fake transport、`tests/test_state_bootstrap.py` | `uv run pytest tests/test_state_bootstrap.py -q -k command` 通过，已知 revision/empty 两种模式先只读验证受管远端路径，再以 generation 1 写 current；unknown adopt 不受支持，dry-run 不写 | P06, D01, S09 | 待办 |
| C05 | 增加 `state recover` 决策入口 | CLI、transaction recovery、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_recover` 通过，可显示决策、执行可证明的 finalize/restore；无法自动决定时保持 `manual_recovery_required` 并给出不含敏感值的操作摘要 | D09, C01 | 待办 |

## E. 构建产物能力

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| B01 | 固化 host build/artifact TOML schema | `config.py`、模型、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k 'host_build or artifact'` 通过，覆盖默认/显式 host runner、argv、timeout、cwd、env allowlist、file/tree mapping 和非法路径 | S02 | 待办 |
| B01D | 固化 Docker build runner 配置契约 | `config.py`、build 模型、`tests/test_config.py` | `uv run pytest tests/test_config.py -q -k docker_build` 通过，覆盖 image、platform、`none/bridge` network、`never/missing` pull policy、默认值和非法 runner；Docker 配置不能注入任意 `docker run` 参数 | B01 | 待办 |
| B02 | 从真实或持久化合成 tree 物化隔离 worktree | 新增 worktree manager、`tests/test_worktree.py` | `uv run pytest tests/test_worktree.py -q` 通过，current/target tree ID 精确一致，成功/异常/中断后清理，主工作区脏文件不进入 | P02, S06 | 待办 |
| B03 | 实现受限 host BuildRunner | 新增 build runner、`tests/test_build_runner.py` | `uv run pytest tests/test_build_runner.py -q -k host` 通过，覆盖 argv 无 shell、cwd、timeout 终止进程组、env 白名单、敏感值脱敏和非零退出 | B01, B02 | 待办 |
| B03D | 实现 Docker CLI 命令与镜像身份适配器 | 新增 Docker build backend、`tests/test_docker_build_runner.py` | `uv run pytest tests/test_docker_build_runner.py -q -k 'command or image'` 通过；fake Docker 断言 inspect/pull policy、不可变 image ID、固定 worktree mount、UID/GID、platform/network、no-new-privileges，且无 home/repository/SSH Agent/socket/state mount 和任意参数注入 | B01D, B02 | 待办 |
| B03E | 实现 Docker 构建生命周期与 BuildRunner 接线 | Docker build backend、runner facade、`tests/test_docker_build_runner.py` | `uv run pytest tests/test_docker_build_runner.py -q -k 'lifecycle or env or timeout'` 通过；成功/非零/超时/Ctrl-C 均按 stop/kill/remove 清理，产物归属当前 UID/GID，env 值不进 argv/log；清理失败阻断且远端调用为 0，绝不回退 host | B03, B03D | 待办 |
| B04 | 实现 artifact collector 安全边界 | 新增 artifact collector、`tests/test_artifacts.py` | `uv run pytest tests/test_artifacts.py -q -k collector` 通过，file/tree、mode/hash/size 正确，绝对路径、`..`、symlink、submodule、FIFO/socket/device 全部拒绝 | B01, B02 | 待办 |
| B05 | 实现 build fingerprint 与 artifact cache | build cache、CAS、`tests/test_build_cache.py` | `uv run pytest tests/test_build_cache.py -q` 通过，tree/config/lock/tool/runner 任一变化 miss；Docker tag 解析后的 image ID、platform、network、pull policy、UID/GID 任一变化 miss，完全相同 hit；环境值不进 fingerprint/log | B03E, B04, S05 | 待办 |
| B06 | 以 current artifact manifest 生成 target artifact 差量 | artifact planner、state schema、`tests/test_artifact_planner.py` | `uv run pytest tests/test_artifact_planner.py -q` 通过，新增、修改、删除、mode、首次 baseline build/bootstrap 和缺失基线阻断均覆盖 | B05, S08 | 待办 |
| B07 | 合并 source/artifact 计划并执行 owner 冲突门禁 | combined planner、`tests/test_combined_planner.py` | `uv run pytest tests/test_combined_planner.py -q` 通过，统一 current/target file state；source/artifact、artifact/artifact 目标路径冲突在远端连接前失败 | P05, B06 | 待办 |
| B08 | 把目标内容读取改为逐文件 provider/spool | planner/executor/content provider、`tests/test_streaming.py` | `uv run pytest tests/test_streaming.py -q` 通过，以大文件和多文件 fixture 断言不会预加载全部 target bytes，临时 spool 在成功/失败后清理 | B07 | 待办 |
| B09 | 将 source/artifact 纳入同一远端事务与 state 迁移 | executor、rollback、`tests/test_combined_transaction.py` | `uv run pytest tests/test_combined_transaction.py -q` 通过，任一 source/artifact 上传、删除、hook、verify 失败都恢复统一 before bytes 与 before state | B08, D09, R01 | 待办 |
| B10 | 增加显式本地 build 验证入口并保持 dry-run 边界 | CLI、README、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k 'build_command or build_dry_run'` 通过；build 可写本地 cache 但不连接远端；host/docker 普通 deploy dry-run 均不创建 worktree、不 inspect/pull/run、不写状态 | B05, B09 | 待办 |

## F. 状态对象回收

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| G01 | 实现 state/CAS 引用可达性 GC 计划器 | expected-state/CAS/build-cache stores、`tests/test_state_gc.py` | `uv run pytest tests/test_state_gc.py -q -k mark_sweep` 通过，current、未完成 transaction、可回滚 deployment、有效 build cache 全部标记保留，仅列出真正不可达对象 | S07, S10, R01, B05 | 待办 |
| G02 | 实现持久化 Git object store 安全回收 | state Git store、`tests/test_state_gc.py` | `uv run pytest tests/test_state_gc.py -q -k git_objects` 通过，受 current/retained state 引用的 tree/blob 可跨进程读取，不可达临时 objects 可回收，主仓库 object database 不变化 | G01, S06 | 待办 |
| C06 | 增加默认 dry-run 的 `state gc` | CLI、GC service、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_gc` 通过；默认只打印对象数/字节数，真实删除要求 `--yes`，删除后 `state verify`、rollback fixture 和 build cache retained fixture 仍通过 | G01, G02, C02 | 待办 |

## G. 集成、文档与发布

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|----|------|---------|----------------|------|------|
| I01 | 验证 `all` 的 target/state/lock/build 隔离 | CLI、多项目 fixture、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k all_state_isolation` 通过，一个项目失败不改变另一个项目的 current、CAS、journal 或远端 fixture | B10, S09 | 待办 |
| I02 | 增加 official-v2 Docker Composer 构建 fixture 与样例配置 | `deploy.example.toml`、fake Docker fixture、`tests/test_official_v2_build.py` | `uv run pytest tests/test_official_v2_build.py -q` 通过，digest 固定的 Docker Composer 配置与确定性 fake CLI 覆盖 `vendor/` 新增、修改、删除、漂移、no-op、失败恢复和回滚；自动测试不连接 registry/daemon | B10 | 待办 |
| I03 | 完成 FTP/FTPS/SFTP fake transport 端到端矩阵 | transport integration tests | `uv run pytest tests/test_transport.py tests/test_combined_transaction.py -q` 通过，三协议不访问真实服务器且断言相同 state/rollback 语义 | B09 | 待办 |
| I04 | 编写 v0.1.5 状态迁移、bootstrap、inspect、verify、recover、gc 操作文档 | `README.md`、规划/迁移文档、CLI help snapshot | `uv run pytest tests/test_cli.py -q -k help` 通过；文档包含首次部署、状态丢失、配置身份变化、未完成事务、GC dry-run 和禁止常态化 `--force` 的命令流程 | B10, C01, C02, C03, C04, C05, C06 | 待办 |
| I05 | 执行 v0.2 完整发布门禁 | 全项目、package metadata、`uv.lock` | `uv run pytest -q`、`uvx ruff check src tests`、`uvx ty check src`、`uv build --clear` 全部通过；在 `<项目根>/tmp` 隔离 `uv tool install` 后版本/help/单提交/组合提交及 Docker 配置 dry-run smoke 通过，断言 dry-run 的 fake Docker 调用为 0 | I01, I02, I03, I04 | 待办 |
| U01 | 真实 FTP/FTPS 隔离目录人工增强验证 | 用户提供的非生产测试目录和最小权限账号 | **由用户代验**：先 dry-run，再只读 remote check，再部署 fixture、重复 no-op、回滚；记录文件哈希和清理结果，不记录密码；未执行不阻塞 I05 自动主线 | I05 | 待办 |
| U02 | 真实 Docker daemon 隔离构建人工增强验证 | 本地/CI Docker daemon、预置 digest 镜像、临时仓库 | **由用户代验**：使用非生产临时仓库和预置 digest 镜像运行 host/docker 等价 fixture，验证 UID/GID、network none/bridge、超时/中断清理及 artifact hash；不挂载凭据、不连接远端，未执行不阻塞 I05 自动主线 | I05 | 待办 |

## 完成门禁

- [x] 每条 TODO 都有可执行或可观察的 DoD。
- [x] 清单含 T00 环境预检，并覆盖 Python、Git、uv、Composer、Node/npm、Go、Docker 和发布命令。
- [x] 已声明北极星依据与 v0.1.5 当前边界。
- [x] 已包含验证纪律、受阻留言、禁止绕过、熔断、状态同步和用户代验约定。
- [x] 真实远端联调与 fake transport 自动主线分离，且明确敏感配置不得进入日志/文档/对话。
- [x] 各任务以单一产出为主，依赖显式且无循环依赖。
- [x] 状态基线任务位于构建产物任务之前。
- [x] Docker runner 已拆分配置、命令/镜像身份、生命周期接线、fingerprint、dry-run 和真实 daemon 人工增强，且依赖无环。

## 变更记录

| 日期 | 变更内容 | 原因 |
|---|---|---|
| 2026-07-10 | 首版：整合 current snapshot、revision patch 幂等、事务恢复与构建产物任务 | 解决重复选择性部署不能继续以 Git 父提交作为远端基线的问题 |
| 2026-07-12 | 将 Docker 构建沙箱拆入 B01D/B03D/B03E，并扩展 B05/B10/I02/I05/U02 | 用户要求构建产物阶段可选择在 Docker 中执行，同时保持 dry-run、凭据和远端事务边界 |
