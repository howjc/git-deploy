# git-deploy v0.3.2

v0.3.2 修复 v0.3.1 之后深度审计中的 application → domain exact-plan 边界漏洞
（P0），并将 README 安装指引切到当前安全版本。

对照审计：`docs/git-deploy-latest-main-deep-audit-2026-07-15.md`。

## 相对 v0.3.1 的范围

| 提交 / 内容 | 是否包含在 v0.3.2 |
|---|---|
| v0.3.1 release（`c6ca546`）领域层 stale-plan / rollback drift / FTPS | 是（继承） |
| `b2d34f4` static no-op 必须走锁内 freshness（release 后补修） | **是** |
| Application deploy 确认后不再 `_run_plan_or_deploy` 重规划（P0-01） | **是（本版）** |
| Latest rollback exact `expected_deployment_id`（P0-02） | **是（本版）** |
| README 不再推荐 v0.3.0 已知阻断 wheel（P0-03） | **是（本版）** |

说明：已发布的 **v0.3.1 tag 对应 `c6ca546`，不包含** `b2d34f4` 及本版
exact-plan 修复。本版 **不** 用新内容覆盖重发 `0.3.1`；统一以 `0.3.2` 发布。

## 安全与正确性修复

### P0-01：Application deploy exact-plan（禁止确认后重规划）

此前 application 层用 token 绑定 plan A，执行时却递归调用
`_run_plan_or_deploy()` 再 `plan_selectors` 生成 plan B；领域锁内只验证 B。

v0.3.2：

- `RevisionPlanResult` 冻结 `before_state_id`、before/after applied transitions、
  exact `domain_files`，并写入 plan digest
- 执行路径通过 `to_frozen_source_diff_plan` / 基于**冻结 before-boundary** 的
  等价 durable 物化，调用 `StateDeploymentExecutor.deploy`
- **禁止** application execute 再进入 `_run_plan_or_deploy` 重规划
- 并发推进若发生在 execute 无锁 precheck 之后、领域执行之前 → 稳定
  `stale_plan`，零远端连接/写入，零 transaction/manifest

### P0-02：Latest rollback exact-deployment

此前 preview 绑定 deployment A，执行时 `_run_rollback` / `rollback_latest()`
无参重选 latest，结果却可能仍报告 A。

v0.3.2：

- `StateRollbackService.rollback_latest(expected_deployment_id=…, expected_generation=…)`
- 锁内加载 **exact** reviewed deployment，校验仍为 latest successful 且 generation 匹配
- application 路径 `_execute_reviewed_latest_rollback` 不再调用 `_run_rollback` 重选
- 成功路径 `result.deployment_id ==` 实际 `rollback_of`
- preview A 后成功部署 B 成为 latest → A 的 token 执行必须 `stale_plan`，不回滚 B

### P0-03：发布身份与 README

- 包版本 **0.3.2**（`pyproject.toml` 与 `__version__` 一致）
- README 推荐安装 URL 指向 v0.3.2，明确不要再装 v0.3.0
- 本 release notes 写明 v0.3.1 与 `b2d34f4` / 本版边界

## 测试与门禁

- §8.1：execute precheck 后竞态 → stale；禁止 nested `_run_plan_or_deploy`
- §8.2：preview A + concurrent B → A token stale；domain
  `expected_deployment_id` 成功路径 ID 一致
- 全量 `uv run pytest`：365 passed（本发布环境）

## 兼容性

- Python 3.11+；配置与 v0.3.1 兼容
- 直接调用 `rollback_latest()` 不传 expected_* 时仍按 latest 选择（测试 / 旧域路径）
- application CLI latest rollback / deploy 路径语义更严格（exact plan）

## 本版本明确不包含

审计 Patch 2–4 全量（二次读 drift 重校验、rollback backup 布局、rollback
post_commands/health、SFTP rename 收窄、完整 FTPS TLS fixture 矩阵、history/verify
all、doctor schema、StalePlanError 类型统一、release CI）、Textual TUI、非最新
deployment 回滚、自动 GC、PyPI 发布。
