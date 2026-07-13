# git-deploy v0.2 开发就绪度复审报告

## 1. 审计范围

- 审计对象：
  - `docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md`
  - `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md`
  - `docs/audit/2026-07-12-git-deploy-v0.2-plan-audit.md` 的 P1/P2 整改闭环
- 实现基线：分支 `agent/multi-remote-environments`，HEAD `ed86c78`；当前包版本 `0.1.5`，已有 named remote/default remote，尚无 v0.2 current state、transaction 和 artifact 能力。
- 排除项：工作区未跟踪的 v0.3 TUI 规划文档不属于本次 v0.2 审计范围。
- 审计目标：判断调整后的计划能否不再补充设计而直接按 TODO 进入开发。
- 审计方法：按正确性、安全、性能、兼容性、可维护性与测试可验证性复核计划；解析 TODO 依赖图；执行 T00 核心门禁和后续工具版本/daemon 可用性探测。

### 实测结果

- TODO：90 条，其中 active 88、已取消 2；未知依赖 0、循环依赖 0。
- 唯一 active 根任务：`T00`；active terminal 仅 `U01/U02/U03`；最长依赖链 26 层。
- Gate 隔离：`V2A`、`V2B` 均没有 Gate C Docker/1Password 祖先；`I05` 显式汇合 `V2A/V2B/V2C`。
- T00：Python 3.12.3、Git 2.43.0、uv 0.11.6；`uv run pytest -q` 为 32 passed，Ruff、ty 和隔离目录构建全部通过。
- 后续工具探测：Composer 2.9.5、Node 25.2.1、npm 11.18.0、Go 1.25.4、Docker client/daemon 29.6.1、1Password CLI 2.34.1 均可执行。本次未运行尚不存在的语言 fixture、scratch image fixture 或 fake `op` contract，也未连接真实 vault/远端。

## 2. 发现清单

### P0 阻断

无。

### P1 重要

#### P1-1：physical target canonical payload 对认证用户是否属于身份存在冲突

- 位置：`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:49-50`；`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:70-71,210,274`。
- 问题：S01 和北极星把 physical identity 定义为规范化 protocol endpoint/project/remote root，并排除凭据；紧接着 S02 又要求 `user` 变化必须改变 identity。计划没有说明 username 是认证属性还是 protocol endpoint 的组成部分，也没有冻结 protocol、默认端口、hostname 大小写/IPv6 和 username 的 canonical 规则。
- 影响：两个 alias 访问相同物理目录但使用不同账号时，实施者可能生成两个 target ID、两套 state 和两把锁；另一种实现又可能把本应隔离的 endpoint 错误合并。S01/S02 是后续目录布局、锁和迁移的根契约，写完再改会触发状态迁移。
- 建议：在 S01 前或 S01 DoD 内增加 canonical payload 字段表与规范化测试，明确 username/protocol 是否参与 physical identity。若 username 只代表认证主体，建议移到独立 access-policy/config fingerprint；无论最终选择哪种口径，都应增加“同 host/root 不同 user”和“同 host/root 不同 protocol”的共享/隔离测试。

#### P1-2：崩溃恢复只写了 `os.replace`，没有冻结持久化保证

- 位置：`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:123-125,164-168`；`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:54-59,79-86`。
- 问题：计划要求 transaction journal 驱动崩溃恢复，但 state/CAS/current/journal 的发布协议只明确临时文件与 `os.replace`。这能提供路径替换原子性，却没有定义文件 flush/fsync、父目录 fsync、权限设置顺序、主机掉电/内核重启后的保证以及 orphan temp/journal 恢复规则。
- 影响：如果实现者把“进程中断恢复”扩张成“主机崩溃后状态可靠”，可能出现远端已经修改但 journal/current 元数据未持久化的窗口；不同实现也会产生不一致的 durability 语义。
- 建议：在 S05/S07/S08/S10 之前冻结 durability 边界。生产目标若包含主机重启，应要求 write→flush/fsync→replace→parent fsync，并用故障注入/重开进程测试 orphan 处理；若 v0.2 只保证普通进程退出，应在北极星、CLI 恢复文档和 DoD 明确把掉电/文件系统故障列为非目标。

#### P1-3：artifact 首次基线是否允许采纳未知远端内容自相矛盾

- 位置：`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:112,289-294,334,394`；`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:101,130`。
- 问题：artifact 差量章节允许显式 bootstrap 当前远端文件并标记“无法证明来源”；安全边界和完成定义又规定 unknown remote state 不支持 adopt，只能从已知 Git revision/empty 基线验证后 bootstrap。C04 只定义 known revision/empty，B06 却继续写“baseline build/bootstrap”。
- 影响：实施者可能把来源未知的远端 artifact 持久化为可信 current，后续据此删除或回滚文件；另一位实施者则可能完全拒绝该流程，导致 CLI、文档和测试契约分叉。
- 建议：开发前选定唯一口径。保守方案是删除“采纳未知远端文件”路径，只允许从已知 source baseline 可复现构建或显式 empty；若必须支持导入，则应另立受限 migration 任务，定义逐文件确认、provenance=`unknown`、允许的后续操作和不可伪造回滚边界。

#### P1-4：Docker 环境变量注入与“Docker inspect 无 secret”保证不相容

- 位置：`docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md:102,309-314`；`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:125,155`。
- 问题：设计使用 `op run -- docker run --env NAME`，这能避免 secret value 出现在进程 argv，但 Docker daemon 会把解析后的环境变量写入容器配置，具备 daemon 权限的操作者可通过 inspect 读取。北极星同时要求解析值不得出现在 Docker inspect，按当前环境变量方案无法成立。
- 影响：测试可能只证明 git-deploy 没有打印 inspect 输出，却把它误写成 secret 不存在于 daemon metadata；用户会获得错误的安全预期。
- 建议：冻结威胁模型并修正文案/DoD：保证 secret 不进入 git-deploy argv、日志、state、manifest 和其捕获的 inspect 输出，但明确本地 Docker daemon/管理员属于可信边界，容器配置在存活期间可能暴露环境变量，并要求可靠删除容器。若产品要求 daemon metadata 也不可见，则不能继续使用普通容器环境变量，需要改用受控临时文件/tmpfs 或其他 secret transport，并重新定义“注入环境变量”的实现边界。

#### P1-5：阶段门禁命令没有完整覆盖其宣称的关键契约

- 位置：`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:157-159`。
- 问题：V2A 宣称覆盖 transaction/crash recovery，但命令未包含 `tests/test_transaction.py` 和 `tests/test_transaction_recovery.py`；V2C 宣称覆盖 Docker/op/config，却未包含 `tests/test_config.py`，因此不会重新运行 B01D/B01O/B01DR/B01OR 的配置与 dev/prod secret override 契约。
- 影响：单条任务完成时测试曾通过，不代表阶段集成后没有回归；V2A/V2C 可能在关键 transaction 或配置隔离已经破坏时仍显示绿色，直到最终 I05 才暴露。
- 建议：V2A 显式加入两个 transaction 测试文件；V2C 加入 `tests/test_config.py`，并优先使用稳定 pytest marker/测试文件分组替代宽泛 `-k` 过滤。Gate 的命令应独立证明该 Gate 的退出条件。

### P2 建议

#### P2-1：内部 Gate 已解耦，但单个 Gate A 仍有较长交付链

- 位置：`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:37-40,48-105,157-164`。
- 评价：首轮审计的可选依赖耦合已修复，但 active 任务仍有 88 条，最长路径 26 层；Gate A 一次汇合 43 个祖先任务。若只在 V2A 汇总反馈，状态 schema、patch composer、transaction 与 CLI 的集成问题仍可能较晚出现。
- 建议：不必再改对外版本范围，但实施时在 Gate A 内记录三个内部 checkpoint：身份/schema/store、composer/plan、transaction/rollback/ops；每个 checkpoint 跑当时的累计回归后再继续。

#### P2-2：部分 TODO 的验证面可能超过“半天原子任务”

- 位置：`docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md:86,123,132,154`。
- 评价：D09 同时覆盖四种崩溃阶段，B03E 覆盖完整 Docker 生命周期，B08 包含 500 MB 性能门禁，I02D 同时构建本地 image 并覆盖成功/失败/超时/Ctrl-C/UID/GID/清理。这些任务目标合理，但实现和调试很可能超过半天。
- 建议：实施前按测试 fixture、核心 happy path、失败/中断注入拆为子任务，保持原 ID 作为汇总门禁；不要在清单中一次把父任务标完成。

## 3. 首轮审计整改复核

| 原发现 | 复核结论 |
|---|---|
| P1-1 可选工具阻塞 T00 | 已闭环；T00 实测通过，Gate A 不依赖 Composer/Node/Go/Docker/op |
| P1-2 host 主线依赖 Docker/1Password | 已闭环；V2B 无 Gate C optional ancestors |
| P1-3 P04/R02 未接发布图 | 已闭环；P04→P05，R02→R03→V2A→I05 |
| P1-4 named remote 未入契约 | 大体闭环，但 canonical identity 的 username/protocol 口径仍需按本报告 P1-1 收紧 |
| P1-5 三层 fingerprint 边界 | 已闭环；physical/policy/build 已分层 |
| P1-6 缺真实 Docker daemon 门禁 | 已闭环；B00D/I02D/V2C/I05 已接线，当前 daemon 可用 |
| P1-7 缺可发布切片 | 已闭环；V2A/V2B/V2C 分阶段汇合 |
| P1-8 host/image 信任模型 | host/image 来源边界已闭环；Docker secret metadata 仍需按本报告 P1-4 修正 |
| P2-1 性能阈值不明确 | 已闭环；B08 固定 500 MB fixture 与测试常量 |
| P2-2 1Password cache 成本 | 已接受保守策略；固定 bypass |
| P2-3 GC/旧回滚优先级低 | 已闭环；移至 v0.3，v0.2 仅保留拒绝/无删除契约 |

## 4. 测试与开发就绪度评估

- 当前实现基线健康，T00 所列核心命令全部可执行并通过；因此不是工具链或既有回归阻塞。
- Composer、Node/npm、Go、Docker daemon 和 `op` CLI 当前均存在，说明 Gate B/C 没有显而易见的环境级硬阻断；但 B00H/B00D/B00O 仍必须在对应任务开始时按 fixture DoD 正式执行并更新状态。
- TODO 图本身无未知依赖和循环，Gate A/B 与可选 Docker/op 已正确解耦；实施者可以严格沿依赖推进。
- 当前工作区还有两份未跟踪 v0.3 规划文档。开始业务代码前应先提交这些文档或使用独立分支/worktree，避免 v0.2 实施提交混入无关文件；这不是计划 P0/P1，但属于开工卫生要求。
- 本次为只读审计，未把 T00 从“待办”改为“已完成”。正式实施者必须重新运行 T00，并按清单纪律同步状态。

## 5. 总体评价

结论：**暂不建议原样直接全面进入开发；先修订 5 项 P1 契约后即可开工，不需要返工整体架构。**

计划方向、优先级、依赖图和环境基础均已达到较高成熟度，无 P0，也没有理由推翻 Gate A/B/C。阻止“原样直接开工”的原因是 P1-1 至 P1-4 都位于 foundational schema/安全边界：若在 S01/S05/B06/B03OD 编码后再决定，会引入状态迁移、兼容返工或错误安全承诺；P1-5 则会削弱阶段门禁可信度。

推荐开工顺序：

1. 先只修改北极星/TODO，关闭 P1-1 至 P1-5；
2. 提交或隔离当前 v0.3 未跟踪文档；
3. 正式执行 T00 并更新为“已完成”；
4. 从 S01 的 canonical identity contract test 和 S05 的 durability contract test 开始；
5. Gate A 内按 identity/schema/store → composer/plan → transaction/rollback/ops 三个 checkpoint 累计验证。

审计判定：`changes-required-before-development`。预计属于小范围规划修订，完成后可直接按原子 TODO 进入开发。
