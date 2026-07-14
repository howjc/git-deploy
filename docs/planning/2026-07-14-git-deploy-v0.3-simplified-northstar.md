# git-deploy v0.3 简化稳定版北极星

> 状态：v0.3 唯一有效北极星
> 日期：2026-07-14
> 基线：v0.2.1

## 1. 目标用户与核心场景

`git-deploy` 面向个人开发者和小团队，用 Git revision 管理多个长期维护项目，并通过 SFTP、FTP 或 FTPS 替代容易出错的手工文件发布。

v0.3 的核心目标只有一个：在保留 v0.2.1 事务、状态、备份、校验和恢复能力的前提下，把日常发布收敛为容易理解的一条命令：

```bash
git-deploy deploy application --remote prod
```

典型流程是：提交代码，查看可信 current 到当前 HEAD 的计划，确认部署，完成备份、上传、验证和 state 提交；失败时自动恢复，并给出可执行的诊断或恢复建议。

## 2. 产品边界

CLI 是 v0.3 唯一稳定主入口，必须适合本地终端、SSH、脚本和 CI。application request/result/error/event/service 层作为 CLI 与领域实现之间的稳定边界保留，不再为未计划的 UI 继续抽象。

v0.3 保留并复用以下 v0.2.1 能力：

- Git commit、连续范围和非连续 selector 的精确变更计算；
- named remote、physical target identity、target lock；
- expected state、generation、CAS、持久化 Git tree；
- durable transaction、部署前备份、上传后校验、失败恢复；
- 最新成功 deployment 回滚和 remote drift 检测；
- SFTP、FTP、FTPS，SFTP ownership/mode；
- Host/Docker artifact build 和 1Password CLI 注入。

## 3. 明确不做

以下能力不进入 v0.3.0，也不应被自动拾取：

- Textual TUI 或 Web UI；
- 非最新 deployment 自动回滚；
- 自动 GC 或自动删除历史对象；
- 多用户、RBAC、审批、通用流水线 DSL；
- Kubernetes/容器编排发布；
- 数据库 migration 自动回滚；
- 自动 adopt 未知远端内容。

最新成功 deployment 回滚是 v0.3 唯一支持的自动回滚。数据库和其他外部系统副作用始终位于文件事务边界之外。

## 4. 成功标准

v0.3.0 发布时必须证明：

1. 有可信 current 时，`plan`/`deploy` 可省略 `--revisions` 并冻结 current → HEAD 的确定计划；无可信 current 时明确拒绝猜测。
2. `rollback PROJECT` 默认选择最新成功 deployment。
3. `doctor` 能只读诊断配置、Git、state、backup、transaction；显式请求时增加远端只读检查。
4. 同一 physical target 的跨进程 mutation 互斥有自动测试。
5. 上传、权限、hook、health、journal、state commit 故障都有远端/state/transaction 结果断言。
6. 本地容器中的真实 SFTP 完成上传、原子替换、权限、删除、漂移和回滚演练；FTP/FTPS 完成兼容 smoke。
7. 错误包含阶段和建议动作，同时不泄漏凭据、token、1Password reference 或 secret value。
8. README 首页能在五分钟内指导安装、bootstrap、日常 deploy、doctor 和最新回滚。
9. 显式 `--revisions` 调用保持兼容。
10. 全量测试、lint、type check、build 和隔离 wheel smoke 全部通过。

## 5. 风险与控制

| 风险 | 控制 |
|---|---|
| 隐式 selector 猜错远端基线 | 只允许可信 current → 已冻结 HEAD；无 current 时拒绝 |
| 简化确认削弱底层安全 | `--force` 仍只能放行明确 drift；identity、policy、integrity、generation、transaction 门禁不可绕过 |
| 失败后远端与 state 分叉 | durable journal、before backup、上传后校验、自动恢复与 recover 决策矩阵 |
| 真实协议行为与 fake 不一致 | 本地容器 SFTP/FTP/FTPS 集成测试，不接触生产配置 |
| 状态增长 | doctor/inspect 报告容量；v0.3 不自动删除 |
| hook/health 外部副作用不可逆 | 明确文件恢复边界，并在错误和恢复手册中说明 |

## 6. 关键决策

| 日期 | 决策 | 原因 |
|---|---|---|
| 2026-07-14 | CLI 是唯一稳定入口 | 适合个人工具、SSH、脚本和 CI，维护成本最低 |
| 2026-07-14 | application service 边界冻结 | 现有边界已覆盖 CLI 需求，不再为未来 UI 增加抽象 |
| 2026-07-14 | 日常默认选择可信 current → HEAD | 减少重复参数，同时不猜测未知远端状态 |
| 2026-07-14 | 只支持最新成功 deployment 自动回滚 | 覆盖主要恢复场景，避免复杂的历史派生回滚 |
| 2026-07-14 | 冻结 TUI、非最新回滚和自动 GC | 当前个人使用收益不足以覆盖长期维护和误操作风险 |
| 2026-07-14 | Mock/fixture/container 是自动发布门禁 | 自动证明编排和协议行为；真实生产联调保留为独立人工验收 |

## 7. 重新评估条件

只有出现可重复的真实使用证据时，才重新评估被冻结能力，例如：state 容量造成明确磁盘压力、CLI 被量化证明显著低效，或最新回滚长期无法覆盖高频事故。重新评估必须先新增 ADR 和原子任务，不得直接恢复旧 TUI/GC TODO。

## 8. 隐式 revision 选择契约

省略 `--revisions` 只允许表达“当前可信 state 到调用时 HEAD”，不得表达“猜测远端当前版本”。application plan service 负责解析和冻结，CLI parser 不读取 state。

| 场景 | 结果 | 安全说明 |
|---|---|---|
| current 存在、完整且 identity/policy 匹配 | 以 current source tree 为基线，把当前仓库 HEAD 冻结为完整 commit SHA 后规划 | token/digest 绑定冻结 SHA、generation、target identity 和 policy |
| current 已对应 HEAD 且 transition 已应用 | 返回静态 no-op、exit 0 | 不连接远端、不创建 transaction/manifest、不推进 state |
| current 不存在 | 拒绝隐式计划 | 提示按已知 Git revision bootstrap，或确认受管路径为空后 empty bootstrap；不得 adopt |
| current tree/commit/object 本地不可达 | 拒绝 | 提示 local state verify、恢复持久化 Git objects 或执行适当 `git fetch`；不得退回 Git 父提交猜测 |
| shallow clone 缺少 current → HEAD 所需 first-parent 历史 | 拒绝 | 提示获取所需历史，不自动执行网络操作 |
| detached HEAD | 允许 | detached HEAD 仍解析为完整 commit SHA，计划不依赖分支名 |
| 生成计划后 HEAD 移动 | 旧计划保持绑定原 SHA；执行若 request/token 不一致则 stale | 不用字面 `HEAD` 在执行时重新解释已审阅计划 |
| working tree 有未提交修改 | 忽略未提交 bytes 并输出 warning | 部署内容只来自 Git objects |
| 显式 `--revisions` | 完全覆盖隐式规则 | 保持 v0.2.1 selector 兼容；无 current 时只能生成 legacy source plan，不能绕过 artifact state 要求 |
| `all` | 每个项目用各自 repository/current/HEAD 独立解析 | 任一项目缺 state 时正式执行整体停止；不复用首个仓库 SHA，不宣称跨项目原子 |

隐式选择产生的结构化结果必须标记 `selection_origin=implicit_current_to_head`；显式调用标记 `selection_origin=explicit`。history 保存解析后的不可变 commit selector，而不是可移动的 `HEAD` 字面值。
