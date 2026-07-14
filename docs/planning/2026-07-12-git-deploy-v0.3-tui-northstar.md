# git-deploy v0.3 TUI 与高级状态运维北极星

> 状态：预案，待 v0.2 Gate A/B/C 与 GA 门禁完成后实施。
>
> 上游依据：
> - `docs/planning/2026-07-10-git-deploy-build-artifacts-northstar.md`
> - `docs/planning/2026-07-10-git-deploy-v0.2-state-build-todo.md`
> - `docs/audit/2026-07-12-git-deploy-v0.2-plan-audit.md`

## 1. 目标

v0.3 在不削弱现有自动化 CLI、安全门禁和非交互输出的前提下，增加一个可选 TUI，作为 dev/prod 多环境部署、计划审阅、状态检查、历史查看和恢复操作的交互入口。TUI 同时支持完整键盘操作与终端鼠标点击、滚轮，鼠标只是输入方式之一，不形成独立业务路径。

v0.3 还承接 v0.2 明确后移的两项高级状态能力：

1. 对较旧非最新 deployment 进行可证明安全的派生回滚；
2. 对 state/CAS/build cache/Git objects 做引用可达性 GC。

这两项能力先在共享应用服务与 CLI 中冻结计划、确认、事务和恢复契约，TUI 只负责展示并调用同一契约。

## 2. 用户收益

| 场景 | v0.3 收益 |
|---|---|
| dev/prod 快速切换 | 常驻显示 remote、项目、physical target ID 和风险等级，减少重复输入与误选环境 |
| revision 选择 | 在同一屏幕输入 commit/range 并审阅文件级增删改、artifact 和警告 |
| 部署观察 | 把既有结构化进度事件呈现为阶段、文件、字节与 transaction 状态，不再依赖滚动日志猜测 |
| 日常运维 | 聚合 history、verify、state、未完成 transaction 与最新回滚入口 |
| 鼠标操作 | 点击选择、按钮和滚轮浏览适合低频操作者，同时保留键盘高效路径 |
| 高风险维护 | 历史回滚与 GC 先展示影响范围、不可逆边界和恢复信息，再允许执行 |

## 3. 优先级与发布切片

| 优先级 | Gate | 范围 | 退出条件 |
|---|---|---|---|
| P0 | Gate A：共享应用服务 | request/result/event、确认策略、取消语义；CLI 改为调用服务层 | CLI 行为、退出码、dry-run 和非 TTY 输出保持兼容，TUI 不直接访问 transport/state |
| P1 | Gate B：只读 TUI | remote/project 选择、plan、history、verify、state/transaction 摘要 | 键盘与鼠标 headless 测试通过；任何只读界面零远端写、零本地状态写 |
| P1 | Gate C：常用变更流程 | deploy、最新回滚、实时进度、失败/中断恢复提示、生产确认 | 所有变更复用 v0.2 transaction/lock；点击不能绕过确认；中断不伪装为回滚成功 |
| P1 | Gate D：高级状态能力 | 非最新回滚、引用可达性 GC 的 core/CLI 契约 | fake transport/fixture 覆盖重叠阻断、mark-and-sweep、崩溃恢复和幂等重试 |
| P2 | Gate E：高级能力 TUI 与兼容收口 | 历史回滚、GC 预览/执行、终端尺寸、SSH/tmux、文档与打包 | 自动门禁全部通过；真实终端兼容由用户代验，不反向阻塞已验证的自动主线 |

Gate A-C 构成 TUI 的最小可发布切片。Gate D-E 不得反向改变 Gate A-C 已冻结的服务、确认和事件契约。

## 4. 固定范围

### 4.1 首版 TUI 包含

- 配置路径、named remote、项目和 physical target 摘要；
- local-only revision plan 与 source/artifact 文件差异；
- history、verify、state inspect、未完成 transaction 摘要；
- deploy 与最新成功 deployment 回滚；
- 阶段、计数、字节、当前路径和最终结果等实时进度；
- 非最新回滚与 GC 的影响预览，在 Gate D 完成后才开放执行；
- 键盘快捷键、Tab/Shift+Tab 焦点移动、Enter/Space 激活、Esc 返回；
- 鼠标单击、滚轮和可见滚动条。

### 4.2 明确非目标

- 不提供 TOML 可视化编辑器；
- 不读取、显示、复制或持久化 1Password secret/reference 值；
- 不在 TUI 内自动登录远端、Docker 或 1Password；
- 不替代脚本/CI 使用的 CLI，不改变非 TTY 稳定输出契约；
- 不把双击、悬停、右键菜单或拖拽设为完成任务的唯一方式；
- 不承诺所有终端模拟器都提供鼠标协议；检测不到鼠标时键盘路径必须完整可用；
- 不把关闭 TUI、Esc 或 Ctrl+C 描述为远端回滚。

## 5. 架构边界

```text
argparse CLI ─┐
              ├─> Application Services ─> Domain/Planner/State/Executor/Transport
Textual TUI ──┘             │
                            └─> Result + Progress/Warning/Transaction Events
```

### 5.1 应用服务层

- 服务接受不可变 request，返回结构化 result；不得在服务内 `print`、读取按键或依赖 Textual widget。
- CLI 和 TUI 使用相同的 remote/project 解析、plan、deploy、history、verify、rollback、state 与 GC 服务。
- 服务显式声明副作用等级：`local_read`、`remote_read`、`local_mutation`、`remote_mutation`。
- 所有变更请求携带确认凭据、预期 target identity、预期 generation 和 transaction policy；UI 不能自行拼接绕过字段。
- 领域错误保持稳定 code/category/context，由 CLI 渲染为 stderr/exit code，由 TUI 渲染为通知或结果屏。

### 5.2 事件与异步适配

- 延续 `ProgressEvent` 的结构化思想，补充 operation、target、warning、transaction stage 和 terminal result 事件。
- 同步领域服务不得阻塞 Textual event loop；通过受控 worker 执行，并把事件投递回 UI。
- 同一 physical target 同时只允许一个变更 worker；只读动作也必须尊重未完成 transaction 的 v0.2 门禁。
- UI 离开屏幕不取消后台 mutation；必须进入明确的“请求取消/等待协调/需要恢复”状态。

### 5.3 可选依赖

- 基础安装继续只提供 CLI；TUI 框架放入 `tui` optional dependency。
- 入口采用 `git-deploy tui`；缺少 extra 时返回明确安装提示，不输出 Python traceback。
- TUI 依赖与样式不得被导入到普通 CLI 冷启动路径。

## 6. 交互与安全契约

### 6.1 环境上下文

- 顶部状态区始终显示 remote alias、project、endpoint 摘要、remote root、physical target ID 短码和风险等级。
- prod 风险不能只依赖红色表达，必须同时显示文本标签；风险等级来自显式配置/策略，不通过 alias 字符串猜测。
- 切换 remote/project 后立即清空旧 plan、确认内容和可执行按钮，防止把 dev 预览执行到 prod。

### 6.2 计划与确认

- 所有 mutation 先生成不可变 operation plan，显示 source/artifact 增删改、hooks、health、build runner、secret 变量名、generation 与警告。
- 普通环境至少经过 review 屏和一次明确激活。
- 非最新回滚、GC delete 和 transaction recovery 必须输入界面显示的确认短语；日常 deploy（包括 prod、`--force` 和 secret build）只需普通确认，CLI 可用 `--yes` 跳过交互。
- plan 生成后 target identity、generation 或 managed policy 变化时，执行必须拒绝并要求重新计划。
- `--force`、secret build、Docker network、历史回滚和 GC 都作为单独风险项展示，不得藏在滚动日志中。

### 6.3 键盘与鼠标

- 每个可点击控件有焦点态和键盘等价操作；关键动作在页脚显示快捷键。
- 列表单击只选择，不直接执行 mutation；执行按钮和确认输入区与列表分离。
- 支持滚轮/触控板转换的滚动事件；内容不能因无法滚动而不可达。
- 不依赖 hover 才能看到关键状态，不依赖双击才能打开详情。
- 自动测试分别用键盘和鼠标完成同一 action，并断言生成的 application request 完全一致。

### 6.4 中断与恢复

- 计划/只读 worker 可安全取消；取消结果不得写 state。
- mutation 在进入 `remote_mutating` 前可以取消；进入后只接受协调取消，由 executor 根据 transaction journal 完成恢复、提交或标记 `manual_recovery_required`。
- TUI 退出时若存在 mutation，必须阻止静默退出并显示 transaction ID、阶段和恢复命令。
- 重启 TUI 后首先检查未完成 transaction；存在时禁用新 mutation，只开放 inspect/recover。

## 7. 非最新回滚契约

- 只允许对状态谱系中仍可定位、对象完整且 policy/target identity 匹配的 deployment 生成回滚计划。
- 先计算该 deployment 引入的 source/artifact transition 与后续状态的路径重叠；不能证明安全时默认阻断。
- 回滚结果是从 current 派生的新 snapshot/generation，不移动历史指针，不删除后续 deployment 证据。
- 远端 mutation、verify、hooks/health 与 state 推进继续处于同一 transaction。
- TUI 必须显示被撤销 deployment、保留的后续 deployment、冲突路径和派生 after state；不得提供“忽略冲突继续”的鼠标快捷入口。

## 8. 引用可达性 GC 契约

- root 至少包括 current、全部未完成 transaction、保留期内/可回滚 deployment、rollback backup、有效 build cache 与显式 pin。
- mark 与 sweep 分离；默认命令和 TUI 入口只生成计划，不删除对象。
- sweep plan 绑定 state generation、root set digest 与对象 hash；任一变化必须使旧计划失效。
- 删除前复算可达性并持有 target/store 级锁；失败记录 journal，可幂等重试。
- 首版不以单纯 age/数量作为可达性判断，不删除未知 schema、损坏对象或正在使用的 worktree/spool。
- TUI 展示对象类别、数量、大小、保留原因与删除计划；实际删除使用确认短语，鼠标点击本身不构成授权。

## 9. 测试策略

### 9.1 自动门禁

- 服务层 contract tests：同一 request 在 CLI/TUI 适配器下产生相同 plan/result/error；
- CLI 兼容：help、退出码、stdout/stderr、dry-run、非 TTY progress 回归；
- Textual headless：使用 `run_test()`/Pilot 模拟按键、点击、滚轮、resize，断言 action/request/焦点与屏幕状态；
- fake transport：自动验证 deploy、verify、rollback、取消、崩溃恢复，禁止真实远端写；
- GC fixture：构造 reachable/unreachable/corrupt/pinned/in-flight 对象图，断言 mark/sweep 和失败恢复；
- secret sentinel：widget、notification、snapshot、日志、异常和剪贴板路径均不得出现 reference/token/value；
- 尺寸矩阵：至少覆盖 60×20 降级、80×24 基线、120×30 和 160×40；关键确认控件必须可达。

Textual 官方提供鼠标事件、滚轮事件和 headless Pilot 点击/按键测试能力，可作为首选框架依据：

- <https://textual.textualize.io/guide/input/>
- <https://textual.textualize.io/guide/testing/>

### 9.2 人工增强

- Linux/macOS 主流终端中的键盘、鼠标、触控板滚动和文本选择；
- SSH 与 tmux 中的鼠标协议、resize、断线和重连提示；
- 真实终端测试仅使用 local-only plan 或 fake/in-memory transport，不读取生产密钥、不写真实服务器；
- 人工增强未执行不阻塞自动主线，但发布说明必须列出已验证与未验证环境。

## 10. 里程碑完成定义

1. 基础 CLI 在未安装 TUI extra 时保持现有功能、启动和错误输出，`git-deploy tui` 给出可操作安装提示。
2. CLI 与 TUI 不直接复制 plan/deploy/rollback/GC 领域逻辑，所有操作通过共享服务层。
3. local-only plan、history、state inspect 在 TUI 中不会连接远端或写任何 state/cache/worktree。
4. remote verify 明确标为远端只读，fake transport 写调用为 0。
5. remote/project 切换会清除旧 plan 和确认，physical target 信息始终可见。
6. 键盘和鼠标对每个主要 action 生成相同 request；没有 mouse-only action。
7. 非最新回滚、GC delete 和 transaction recovery 必须通过确认短语；日常 deploy 可用普通确认或 CLI `--yes`。
8. mutation worker 不阻塞 UI；重复点击、切屏和并发提交不会创建重复 transaction。
9. Ctrl+C、关闭 TUI 和断言失败不会把未知远端状态显示为成功或已回滚。
10. 未完成 transaction 存在时，TUI 禁止新 deploy/rollback/GC 并提供 inspect/recover 导航。
11. 非最新回滚对重叠、对象缺失、identity/policy/generation 变化默认阻断；安全案例生成新派生 state。
12. GC dry-run 默认零删除；sweep 绑定 generation/root digest，过期计划无法执行。
13. state/CAS/build cache/Git objects 的 reachable、pinned、in-flight、corrupt fixture 均有自动测试。
14. TUI 所有输出、通知、snapshot 和异常不包含 1Password reference、token 或 secret value。
15. 60×20 至 160×40 的 headless 尺寸矩阵可完成关键只读和确认流程。
16. `uv run pytest -q`、lint、type check、build 与隔离安装 smoke 全部通过。

## 11. 风险与控制

| 风险 | 控制 |
|---|---|
| UI 与 CLI 行为分叉 | 先抽应用服务；适配器 parity contract test |
| async UI 包装同步远端操作后卡死 | worker + 结构化事件；禁止在 widget handler 直接调用 transport |
| 鼠标误触生产 | 列表点击只选择；review + 普通确认；执行时复核 generation/target |
| 用户误解关闭窗口等于取消/回滚 | transaction 阶段常驻显示；协调取消与恢复结果显式化 |
| TUI 依赖扩大基础安装面 | optional dependency + lazy import |
| snapshot 泄漏 secret | sentinel 全通道扫描；不把 secret/reference 放进 view model |
| GC 破坏回滚/恢复 | root 冻结、mark/sweep、二次复算、锁、journal、默认 dry-run |
| 高级回滚破坏后续变更 | 路径/transition 重叠检测，不能证明安全即拒绝 |

## 12. 关键决策

| 日期 | 决策 | 原因 |
|---|---|---|
| 2026-07-12 | TUI 是可选适配器，CLI 和领域服务仍为稳定核心 | 保持 CI/脚本兼容，避免两套部署逻辑 |
| 2026-07-12 | 采用键盘优先、鼠标等价而非鼠标优先 | 兼容 SSH/tmux/无鼠标终端并降低误触风险 |
| 2026-07-12 | Gate A-C 先交付日常 TUI，历史回滚与 GC 后接 | 先释放多环境选择、计划审阅和部署可观测性收益 |
| 2026-07-12 | 不可逆操作使用确认短语且执行时复核 plan | 鼠标点击不能成为破坏性变更的唯一授权 |
| 2026-07-14 | 日常 deploy 简化为普通确认，CLI `--yes` 可覆盖 prod、force、secret 风险 | 个人脚本和小团队内部使用优先保持自动化命令简洁；底层 identity、integrity、generation、transaction 门禁不变 |
| 2026-07-12 | 首选 Textual，最终版本在 T00/P01 通过 Python 3.11 与打包测试后冻结 | 其输入与 headless 测试能力符合鼠标、键盘和自动门禁要求 |
