# git-deploy v0.3 TUI 与高级状态运维 TODO 清单

> 依据：`docs/planning/2026-07-12-git-deploy-v0.3-tui-northstar.md`（Gate A-E）。
> 前置：v0.2 Gate A/B/C 与 GA 门禁完成；本清单不替代 v0.2 未完成任务。
>
> **实施约定（硬性，随清单对实施者生效——实施者可能是 Claude、Codex 或其他 agent）：**
> 1. 每条完成后必须实际运行该条 DoD 命令，未运行不得标“已完成”。
> 2. DoD 无法执行（缺工具、缺授权、缺外部环境）时，该条标“受阻”，写 `tmp/agent-handoff/<task-id>/escalation.md` 留言（≤30 行、面向决策），暂停该条，禁止绕过。
> 3. 禁止手工编辑生成产物（lock 文件、构建输出等）；工具链缺失 = 受阻。`uv.lock` 只能由 `uv lock`/`uv sync` 等官方命令更新。
> 4. 连续 3 条任务无法按 DoD 验证时停止实施，等待用户决策。
> 5. 状态变化同步更新本清单状态列；同时“进行中”≤ 2 条，禁止批量翻状态。
> 6. 标注“由用户代验”的 DoD 项，实施者完成自动验证部分后将该条标“进行中”并列明待代验内容，不得自行标“已完成”。
> 7. 自动主线只使用临时 Git 仓库、fixture、fake/in-memory transport 和 headless TUI；真实终端测试不得读取生产密钥或写真实服务器。

## 固定实施口径

- TUI 是 optional dependency 和现有 CLI 之上的适配器；不得在 widget/event handler 中直接访问 transport、state store 或 executor。
- CLI/TUI 共用不可变 request/result、结构化 error 和 operation event；同一 action 的业务结果与安全门禁必须一致。
- TUI 所有 mouse action 都必须有 keyboard 等价路径；点击列表只选择，不直接执行 mutation。
- prod、非最新回滚、GC delete、transaction recovery 必须输入确认短语；单击不能替代确认。
- mutation 进入 `remote_mutating` 后只能协调取消；关闭界面、Esc 或 Ctrl+C 不等于远端回滚。
- 只读 local-only 流程不得创建 worktree/cache/state，不得连接远端；remote verify 允许连接但写调用必须为 0。
- secret/reference/token/value 不得进入 view model、widget、notification、snapshot、日志或剪贴板。
- 非最新回滚与 GC 先完成 core/CLI 契约和自动门禁，再接入 TUI。

## A. 环境与公共契约

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|---|---|---|---|---|---|
| T00 | 环境预检：确认 v0.2 基线与 v0.3 核心工具链 | Python/Git/uv、v0.2 清单、全量测试 | 输出 `python --version`、`git --version`、`uv --version`；确认 v0.2 `V2A/V2B/V2C/I05` 状态为已完成；`uv run pytest -q` 通过，否则本任务受阻 | — | 待办 |
| A01 | 定义 application operation request 协议 | 新增 application models、`tests/test_application_contract.py` | `uv run pytest tests/test_application_contract.py -q -k request` 通过；plan/deploy/history/verify/rollback/state/GC request 不可变并显式包含 remote、project、side-effect level 与预期 identity/generation | T00 | 待办 |
| A02 | 定义 application result 与 error 协议 | application models/errors、`tests/test_application_contract.py` | `uv run pytest tests/test_application_contract.py -q -k 'result or error'` 通过；结果不含 renderer 对象，错误具有稳定 code/category/context 且 context 自动脱敏 | A01 | 待办 |
| A03 | 扩展 operation/progress/transaction 事件协议 | `progress.py`、application events、`tests/test_progress.py` | `uv run pytest tests/test_progress.py -q -k operation_event` 通过；事件覆盖 operation/target/warning/transaction stage/terminal result，旧 `ProgressEvent` 消费者保持可用 | A02 | 待办 |
| A04 | 实现确认策略模型 | application policy、`tests/test_confirmation_policy.py` | `uv run pytest tests/test_confirmation_policy.py -q` 通过；普通 mutation、prod、force、secret、历史回滚、GC/recover 分级，风险来自显式策略而非 alias 猜测 | A01 | 待办 |
| A05 | 实现 operation plan 防重放凭据 | application plan token、`tests/test_application_contract.py` | `uv run pytest tests/test_application_contract.py -q -k stale_plan` 通过；token 绑定 request、target identity、policy fingerprint、generation 和 plan digest，任一变化使执行拒绝 | A04 | 待办 |
| A06 | 定义协调取消状态机 | application cancellation、`tests/test_cancellation.py` | `uv run pytest tests/test_cancellation.py -q -k state_machine` 通过；区分可立即取消、等待 executor 协调、已提交和 manual recovery，不把 UI 关闭映射为成功回滚 | A03 | 待办 |

## B. Gate A：共享应用服务与 CLI 兼容

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|---|---|---|---|---|---|
| S01 | 抽取配置、remote 与项目选择服务 | `config.py`、application config service、`tests/test_application_config.py` | `uv run pytest tests/test_application_config.py -q` 通过；解析结果包含 alias/physical target/risk 摘要，切换 remote 不继承旧 project selection 或确认状态 | A02 | 待办 |
| S02 | 抽取 revision plan 应用服务 | planner facade、`tests/test_application_plan.py` | `uv run pytest tests/test_application_plan.py -q` 通过；local-only plan 返回结构化 source/artifact/build/warning，远端、state 写、worktree/build 调用均为 0 | S01, A05 | 待办 |
| S03 | 抽取 history 应用服务 | state/history facade、`tests/test_application_history.py` | `uv run pytest tests/test_application_history.py -q` 通过；分页/选择/legacy lineage 返回结构化结果且零远端调用、零状态写 | S01 | 待办 |
| S04 | 抽取 state inspect 应用服务 | state facade、`tests/test_application_state.py` | `uv run pytest tests/test_application_state.py -q -k inspect` 通过；返回 current/generation/identity/policy/transaction 摘要，损坏对象成为结构化错误且零写入 | S01 | 待办 |
| S05 | 抽取 remote verify 应用服务 | verify facade、fake transport、`tests/test_application_verify.py` | `uv run pytest tests/test_application_verify.py -q` 通过；local/remote read 模式明确，remote 模式 transport 写调用和 state 写调用均为 0 | S04 | 待办 |
| S06 | 抽取 deploy 应用服务 | executor facade、fake transport、`tests/test_application_deploy.py` | `uv run pytest tests/test_application_deploy.py -q` 通过；仅接受有效 plan token/confirmation，重复 execute 只产生一个 transaction，事件与最终 manifest 一致 | S02, A03, A04, A06 | 待办 |
| S07 | 抽取最新回滚应用服务 | rollback facade、fake transport、`tests/test_application_rollback.py` | `uv run pytest tests/test_application_rollback.py -q -k latest` 通过；预览与执行分离，identity/generation/confirmation 复核，事件与派生 state 一致 | S03, S05, A05, A06 | 待办 |
| S08 | 实现 application worker 适配器 | worker/operation controller、`tests/test_application_worker.py` | `uv run pytest tests/test_application_worker.py -q` 通过；同步服务在 worker 执行，事件有序投递；同 target mutation 互斥，重复提交被拒绝 | S06, S07 | 待办 |
| C01 | 让 argparse CLI 改用只读应用服务 | `cli.py`、CLI renderer、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k 'plan or history or verify or state'` 通过；parser/help/stdout/stderr/exit code snapshot 兼容，CLI 不直接调用 planner/store/transport | S02, S03, S04, S05 | 待办 |
| C02 | 让 argparse CLI 改用变更应用服务 | `cli.py`、CLI renderer、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k 'deploy or rollback or recover'` 通过；`--yes` 仅映射允许的确认策略，prod/高风险不能因适配器差异绕过 | S06, S07, S08, C01 | 待办 |
| V3A | Gate A：CLI 与应用服务兼容门禁 | application/CLI/既有回归 | `uv run pytest tests/test_application_*.py tests/test_cli.py tests/test_progress.py -q` 与 `uv run pytest -q` 通过；未安装 TUI 依赖时基础 CLI 仍可运行 | C02 | 待办 |

## C. Gate B：只读 TUI 与鼠标基础

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|---|---|---|---|---|---|
| P01 | 增加 Textual optional dependency | `pyproject.toml`、`uv.lock`、打包测试 | 仅用 `uv lock` 更新 lock；基础环境与 `uv sync --extra tui` 均可安装；`uv build --clear` 通过，普通 CLI import 不加载 `textual` | V3A | 待办 |
| P02 | 增加 `git-deploy tui` 懒加载入口 | parser、TUI bootstrap、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k tui_entry` 通过；安装 extra 时启动 app，缺 extra 时给出安装提示且无 traceback，其他命令不导入 TUI 模块 | P01 | 待办 |
| U01 | 实现 TUI app shell 与全局导航 | TUI app/screens/styles、`tests/tui/test_shell.py` | `uv run pytest tests/tui/test_shell.py -q` 通过；键盘可达 dashboard/plan/history/state/help/quit，页头页脚常驻且无业务副作用 | P02 | 待办 |
| U02 | 实现 remote/project 选择组件 | TUI selector/view model、`tests/tui/test_selection.py` | `uv run pytest tests/tui/test_selection.py -q` 通过；键盘/单击产生相同 selection request，始终显示 alias/target/root/risk；切换后清空旧 plan/confirmation | U01, S01 | 待办 |
| U03 | 实现 revision 输入与 local plan 屏 | TUI plan screen、`tests/tui/test_plan.py` | `uv run pytest tests/tui/test_plan.py -q -k input` 通过；支持 commit/range 多 selector、校验错误和重新计划，输入/查看过程零远端、零 state/cache/worktree 写 | U02, S02 | 待办 |
| U04 | 实现 source/artifact 差异表 | TUI plan widgets、`tests/tui/test_plan.py` | `uv run pytest tests/tui/test_plan.py -q -k diff_table` 通过；增删改、mode、size、build/secret 警告可筛选滚动，鼠标单击仅选中详情不执行 mutation | U03 | 待办 |
| U05 | 实现 history 浏览屏 | TUI history screen、`tests/tui/test_history.py` | `uv run pytest tests/tui/test_history.py -q` 通过；分页、选择与详情支持键盘/点击/滚轮，legacy/current lineage 可区分且零写入 | U02, S03 | 待办 |
| U06 | 实现 state/transaction 摘要屏 | TUI state screen、`tests/tui/test_state.py` | `uv run pytest tests/tui/test_state.py -q` 通过；显示 generation/identity/policy/object/未完成 transaction；存在未完成 transaction 时所有 mutation action 禁用 | U02, S04 | 待办 |
| U07 | 实现 verify 结果屏 | TUI verify screen、`tests/tui/test_verify.py` | `uv run pytest tests/tui/test_verify.py -q` 通过；local 与 remote-read 标识明确，match/absent/drift 可导航，fake transport 写调用为 0 | U05, U06, S05 | 待办 |
| X01 | 统一 TUI action 与键盘/鼠标绑定 | TUI actions/bindings、`tests/tui/test_input_parity.py` | `uv run pytest tests/tui/test_input_parity.py -q -k action_request` 通过；主要 action 的按键和单击生成字节等价 request，无 mouse-only/double-click-only action | U04, U05, U06, U07 | 待办 |
| X02 | 增加滚轮、焦点与可见状态门禁 | scroll/focus styles、`tests/tui/test_input_parity.py` | `uv run pytest tests/tui/test_input_parity.py -q -k 'scroll or focus'` 通过；Tab 顺序稳定、焦点可见、滚轮内容可达，关键状态不依赖 hover/颜色 | X01 | 待办 |
| X03 | 增加 headless 尺寸降级矩阵 | responsive styles、`tests/tui/test_responsive.py` | `uv run pytest tests/tui/test_responsive.py -q` 通过；60×20、80×24、120×30、160×40 可导航，关键警告/返回/确认入口不被裁掉 | X02 | 待办 |
| V3B | Gate B：只读 TUI 与输入门禁 | TUI read-only/headless tests | `uv run pytest tests/tui/test_shell.py tests/tui/test_selection.py tests/tui/test_plan.py tests/tui/test_history.py tests/tui/test_state.py tests/tui/test_verify.py tests/tui/test_input_parity.py tests/tui/test_responsive.py -q` 通过；fake transport 写调用、state 写调用均为 0 | X03 | 待办 |

## D. Gate C：部署、最新回滚与恢复交互

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|---|---|---|---|---|---|
| M01 | 实现不可变 review/confirmation 屏 | TUI confirmation view model、`tests/tui/test_confirmation.py` | `uv run pytest tests/tui/test_confirmation.py -q -k review` 通过；显示 identity/generation/diff/hooks/build/risk，plan token 变化立即禁用执行 | V3B, A04, A05 | 待办 |
| M02 | 实现高风险确认短语组件 | TUI confirmation widget、`tests/tui/test_confirmation.py` | `uv run pytest tests/tui/test_confirmation.py -q -k phrase` 通过；prod/force/secret/recover 必须精确输入短语，粘贴/点击/Enter 都不能在短语错误时提交 | M01 | 待办 |
| M03 | 接入 deploy worker | TUI deploy controller、fake transport、`tests/tui/test_deploy.py` | `uv run pytest tests/tui/test_deploy.py -q -k execute` 通过；执行调用 S06/S08，重复点击只产生一个 transaction，切屏不取消 mutation | M02, S08 | 待办 |
| M04 | 实现部署实时进度屏 | TUI progress widgets、`tests/tui/test_deploy.py` | `uv run pytest tests/tui/test_deploy.py -q -k progress` 通过；阶段/计数/字节/路径/transaction 有序显示，路径脱敏，完成后 manifest/state 与服务结果一致 | M03, A03 | 待办 |
| M05 | 实现取消与退出协调提示 | TUI lifecycle、`tests/tui/test_cancellation.py` | `uv run pytest tests/tui/test_cancellation.py -q` 通过；计划可立即取消，remote_mutating 只请求协调取消；退出显示 transaction ID/阶段/recover 命令且不伪报回滚 | M04, A06 | 待办 |
| M06 | 接入最新回滚 review/execute | TUI rollback screen、fake transport、`tests/tui/test_rollback.py` | `uv run pytest tests/tui/test_rollback.py -q -k latest` 通过；从 history 进入 review，执行复用 S07/S08，鼠标不能跳过确认，结果显示派生 generation | M02, S07 | 待办 |
| M07 | 实现启动时 transaction 恢复门禁 | TUI bootstrap/state controller、`tests/tui/test_recovery.py` | `uv run pytest tests/tui/test_recovery.py -q` 通过；未完成 transaction 优先展示 inspect/recover，deploy/rollback/GC 按钮禁用，恢复使用确认短语 | M05, U06 | 待办 |
| M08 | 增加 secret UI 全通道泄漏测试 | TUI fixtures/snapshots/log capture、`tests/tui/test_secrets.py` | `uv run pytest tests/tui/test_secrets.py -q` 通过；reference/token/sentinel 不出现在 view model、DOM 文本、notification、snapshot、异常、日志和 clipboard adapter | M04, M06, M07 | 待办 |
| V3C | Gate C：常用 mutation TUI 门禁 | deploy/rollback/recovery TUI tests | `uv run pytest tests/tui/test_confirmation.py tests/tui/test_deploy.py tests/tui/test_cancellation.py tests/tui/test_rollback.py tests/tui/test_recovery.py tests/tui/test_secrets.py -q` 通过；键盘/鼠标路径结果一致且无重复 transaction | M08 | 待办 |

## E. Gate D：非最新回滚核心能力

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|---|---|---|---|---|---|
| R01 | 定义非最新回滚 eligibility 结果协议 | rollback models、`tests/test_historical_rollback.py` | `uv run pytest tests/test_historical_rollback.py -q -k eligibility` 通过；结果区分 eligible/conflict/missing/corrupt/stale identity-policy-generation，不可证明时默认拒绝 | V3A | 待办 |
| R02 | 实现 transition 与路径重叠分析器 | rollback planner、`tests/test_historical_rollback.py` | `uv run pytest tests/test_historical_rollback.py -q -k overlap` 通过；source/artifact add/modify/delete/mode 与后续 deployment 重叠矩阵均覆盖，无 `--force` 绕过 | R01 | 待办 |
| R03 | 生成非最新回滚派生 snapshot | expected-state service、`tests/test_historical_rollback.py` | `uv run pytest tests/test_historical_rollback.py -q -k derived_state` 通过；只移除目标 deployment transition，保留后续不重叠变化，历史对象/manifest 不被改写 | R02 | 待办 |
| R04 | 实现非最新回滚远端预检 | rollback service、fake transport、`tests/test_historical_rollback.py` | `uv run pytest tests/test_historical_rollback.py -q -k remote_check` 通过；比较 current/target/actual，第三种内容阻断；预检零远端写、零 state 写 | R03, S05 | 待办 |
| R05 | 实现非最新回滚事务执行 | rollback executor/service、`tests/test_historical_rollback.py` | `uv run pytest tests/test_historical_rollback.py -q -k execute` 通过；backup/mutate/verify/hooks/state 位于同一 transaction，故障恢复 before 或进入明确 recovery 状态 | R04, S08 | 待办 |
| R06 | 接入非最新回滚 CLI 契约 | CLI renderer、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k historical_rollback` 通过；默认预览、显式执行、确认短语/plan token、冲突列表和退出码稳定 | R05, C02 | 待办 |

## F. Gate D：引用可达性 GC 核心能力

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|---|---|---|---|---|---|
| G01 | 定义 GC root 与 pin 协议 | GC models/state schema、`tests/test_state_gc.py` | `uv run pytest tests/test_state_gc.py -q -k roots` 通过；current、transaction、deployment、backup、build cache、worktree/spool 与显式 pin 的保留原因可序列化 | V3A | 待办 |
| G02 | 实现 state/CAS/build cache marker | GC marker、`tests/test_state_gc.py` | `uv run pytest tests/test_state_gc.py -q -k mark_cas` 通过；reachable/unreachable/shared/cycle/corrupt/unknown schema fixture 分类稳定，损坏对象不进入删除集 | G01 | 待办 |
| G03 | 实现持久化 Git object marker | Git state store/GC marker、`tests/test_state_gc.py` | `uv run pytest tests/test_state_gc.py -q -k mark_git` 通过；current/历史/transaction 所需 commit/tree/blob 保留，unknown/shallow/missing object 阻断 sweep | G02 | 待办 |
| G04 | 生成绑定 generation/root digest 的 sweep plan | GC planner、`tests/test_state_gc.py` | `uv run pytest tests/test_state_gc.py -q -k sweep_plan` 通过；计划列对象类型/hash/size/reason，默认零删除；root 或 generation 变化使计划失效 | G03, A05 | 待办 |
| G05 | 实现 GC sweep journal 与对象删除 | GC executor、`tests/test_state_gc.py` | `uv run pytest tests/test_state_gc.py -q -k sweep_execute` 通过；持锁二次 mark 后按计划删除，失败 journal 可重试，reachable/pinned/in-flight 对象删除数为 0 | G04, S08 | 待办 |
| G06 | 接入 GC CLI plan/execute | CLI renderer、`tests/test_cli.py` | `uv run pytest tests/test_cli.py -q -k state_gc` 通过；默认 plan、显式 execute、确认短语和 stale-plan 拒绝稳定，unknown/corrupt 输出不泄漏对象内容 | G05, C02 | 待办 |
| V3D | Gate D：历史回滚与 GC 核心门禁 | historical rollback/GC/CLI tests | `uv run pytest tests/test_historical_rollback.py tests/test_state_gc.py tests/test_cli.py -q -k 'historical_rollback or state_gc'` 通过；默认预览零 mutation，故障注入可恢复 | R06, G06 | 待办 |

## G. Gate E：高级 TUI、文档与发布

| ID | 任务 | 涉及范围 | 完成定义（DoD） | 依赖 | 状态 |
|---|---|---|---|---|---|
| E01 | 接入非最新回滚 TUI 预览 | TUI history/rollback screen、`tests/tui/test_historical_rollback.py` | `uv run pytest tests/tui/test_historical_rollback.py -q -k preview` 通过；展示目标/保留 deployment、冲突路径与派生 state，冲突时执行按钮不可达 | V3D, V3C | 待办 |
| E02 | 接入非最新回滚 TUI 执行 | TUI rollback controller、`tests/tui/test_historical_rollback.py` | `uv run pytest tests/tui/test_historical_rollback.py -q -k execute` 通过；必须确认短语，键盘/鼠标提交同一 token，结果与 R05 服务一致 | E01 | 待办 |
| E03 | 接入 GC TUI 预览 | TUI state/GC screen、`tests/tui/test_gc.py` | `uv run pytest tests/tui/test_gc.py -q -k preview` 通过；按类别显示数量/大小/保留原因/unknown，打开和滚动计划零删除 | V3D, V3C | 待办 |
| E04 | 接入 GC TUI 执行与恢复 | TUI GC controller、`tests/tui/test_gc.py` | `uv run pytest tests/tui/test_gc.py -q -k execute` 通过；确认短语、stale-plan 拒绝、journal 进度/失败恢复可见，重复点击不重复删除 | E03 | 待办 |
| E05 | 编写 TUI 使用与安全文档 | README、`docs/` TUI 指南、help snapshots | 文档示例覆盖安装 extra、键盘/鼠标、dev/prod、确认、中断/recover、无鼠标 fallback、历史回滚/GC；`uv run pytest tests/test_cli.py -q -k help` 通过 | E02, E04 | 待办 |
| E06 | 增加 TUI 隔离安装 smoke | package fixture、`tests/integration/test_tui_package.py` | `uv build --clear` 后在 `<项目根>/tmp` 创建基础/TUI 两种隔离环境；基础 CLI 可用且 tui 给提示，extra 环境完成 headless startup/help/quit | E05, P02 | 待办 |
| E07 | 执行 v0.3 全量自动发布门禁 | 全项目、package metadata、`uv.lock` | `uv run pytest -q`、`uvx ruff check src tests`、`uvx ty check src`、`uv build --clear` 与 E06 smoke 全部通过；fake transport 无真实写入 | E06, V3B, V3C, V3D | 待办 |
| U01M | 主流本地终端键盘/鼠标人工增强验证 | 用户提供的 Linux/macOS 终端、local-only fixture | **由用户代验**：验证键盘全流程、单击、滚轮、resize、文本选择和无鼠标 fallback；只使用 local-only/fake transport，记录终端名/版本与结果，不记录敏感值；未执行不阻塞 E07 | E07 | 待办 |
| U02M | SSH/tmux 鼠标与断线人工增强验证 | 用户提供的非生产 SSH/tmux 会话、local-only fixture | **由用户代验**：验证 mouse mode 开/关、滚轮、resize、断线后的 transaction 提示；不得连接部署服务器或执行 mutation；未执行不阻塞 E07 | E07 | 待办 |

## 发布门禁

- [x] 每条 TODO 都有可执行或可观察的 DoD。
- [x] T00 覆盖 Python/Git/uv、v0.2 前置状态与全量基线；TUI 依赖在 P01 独立冻结。
- [x] 已声明北极星依据、v0.2 前置与只读规划范围。
- [x] 已包含验证纪律、受阻留言、禁止绕过、熔断、状态同步和用户代验约定。
- [x] 应用服务、CLI 兼容、只读 TUI、mutation TUI、高级 core 和高级 TUI 分 Gate 汇合。
- [x] 键盘/鼠标等价、焦点、滚轮、尺寸矩阵均有 headless 自动门禁。
- [x] prod/历史回滚/GC/recover 的确认短语与 stale-plan 拒绝有独立任务。
- [x] 非最新回滚和 GC 先完成 core/CLI，再接 TUI，依赖方向无反转。
- [x] 自动 fixture 与真实终端人工增强分离；人工项不读取真实秘密、不写真实服务器。
- [x] 文末包含变更记录。

## 变更记录

| 日期 | 变更内容 | 原因 |
|---|---|---|
| 2026-07-12 | 首版：拆分共享应用服务、只读 TUI、部署/最新回滚、鼠标门禁、历史回滚、GC 与发布任务 | 用户希望 v0.3 向支持键盘与鼠标的 TUI 方向迭代，同时承接 v0.2 后移能力 |
