# application 公共契约（v0.3）

本文冻结 `git_deploy.application` 作为 CLI 与领域实现之间的 UI 无关边界。内部模块、私有 helper 和 executor 可以重构；以下公开语义在 v0.3 内不得静默漂移。

## 请求

所有请求都是 immutable、keyword-only dataclass，并携带 `remote`、`project`、最大副作用等级、期望 target identity/fingerprint/generation。

| 请求 | 专属字段 | 合法副作用 |
|---|---|---|
| `PlanRequest` | `revisions`、`check_remote`、`force` | local read；check 时 remote read |
| `DeployRequest` | `revisions`、`dry_run`、`check_remote`、`force` | deploy 为 remote mutation；dry-run 为 local/remote read |
| `HistoryRequest` | `limit`、`offset`、`deployment_id` | local read |
| `VerifyRequest` | `deployment_id`、`latest`、`remote_check` | local/remote read |
| `RollbackRequest` | `deployment_id`、`latest`、`dry_run`、`check_remote`、`force` | rollback 为 remote mutation；preview 为 local/remote read |
| `StateRequest` | `action`、`execute`、`check_remote`、`revision`、`empty` | 由 action 明确限制 |
| `GCRequest` | `execute`、`plan_token` | plan 为 local read；execute 为 local mutation |

稳定枚举为 `OperationKind`、`SideEffectLevel`、`StateAction`。v0.3 可以新增独立 operation/request，但不得删除既有字段、改变既有字段含义、把 read 请求升级为 mutation，或让 mutation 伪装为 read。

## 结果

核心终态结果为 `ApplicationResult`：`operation`、`remote`、`project`、`side_effect`、`status`、`summary`、`fields`、`warnings`。`ResultField` 只允许 scalar 或 flat scalar tuple，不允许 renderer/widget 对象。

已公开的服务结果包括：

- plan：`RevisionPlanResult`、`RevisionSelectionOrigin`、`PlannedChange`、`ArtifactMappingPlan`、`BuildPlanSummary`；`selection_origin` 与 `resolved_revisions` 使 implicit plan 绑定不可变 commit；
- history：`HistoryResult`、`HistoryEntry`、`HistoryLineage`；
- verify：`VerifyResult`、`VerifyPathResult`、`VerifyMode`；
- state：`StateInspectResult`、`StateVerifyResult`、`StateVerifyMode`、`OpenTransactionSummary`；
- rollback：`LatestRollbackPlan`、`RollbackPathPlan`；
- worker：`WorkerHandle`。

允许在新结果中新增有默认值、不会改变现有调用语义的字段。禁止删除或重命名 CLI 已读取的字段、改变状态枚举值，或把 secret/reference/renderer 对象放入结果。

## 错误

`ApplicationError` 固定提供 `code`、`category`、`message`、`context` 和 `to_dict()`；`ErrorCategory`、`ErrorContextItem` 与 `application_error_from_exception()` 是公开契约。error code 必须是稳定的 lowercase dotted identifier，context 必须递归脱敏。

允许新增更具体的稳定错误 code/category 映射；禁止输出凭据、token、1Password reference/value，禁止用 traceback 或任意对象 repr 取代结构化错误。

## 事件

所有事件携带 `operation`、`remote`、`project`、单调递增 `sequence`：

- `OperationStartedEvent(side_effect)`；
- `TargetResolvedEvent(target_id, physical_fingerprint, generation)`；
- `OperationWarningEvent(code, message)`；
- `OperationProgressEvent(progress)`；
- `TransactionStageEvent(transaction_id, stage)`；
- `TerminalResultEvent(result)`。

`OperationEventKind` 与 `TransactionStage` 的既有值不可改名或复用。允许新增向后兼容事件类型，但 CLI 不得依赖 UI 专属字段。

## 服务边界

公开服务包括 `ApplicationConfigService`、`RevisionPlanService`、`DeployService`、`HistoryService`、`VerifyService`、`StateInspectService`、`LatestRollbackService` 和 `ApplicationWorker`。服务负责 request 校验、identity/generation/token 复核与结构化结果；CLI 只负责参数映射和渲染。

v0.3 不新增 TUI 专属 request/result/event 字段。任何公共契约变更都必须先更新本文件和 `tests/test_application_contract.py` 的结构签名，并证明现有 CLI 兼容。
