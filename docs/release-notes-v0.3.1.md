# git-deploy v0.3.1

v0.3.1 修复 v0.3.0 主线审计中的三项发布阻断问题（P0），并补齐相关回归测试。

对照审计：`docs/git-deploy-v0.3.0-latest-main-audit-2026-07-14.md`。

## 安全与正确性修复

### P0-01：锁内 stale-plan 防护

stateful deploy 在取得 `TargetLock` 后、任何远端读取/写入、journal/CAS/manifest
写入之前，重新读取 current，并校验计划冻结的 before-boundary：

- `expected_before_state_id`
- `expected_generation`
- `expected_before_tree_id` / `source_tree_id`
- `expected_before_applied_transition_ids`

不一致时抛出稳定的 `stale_plan` 错误；远端写、transaction、manifest 均为 0。

静态 no-op 路径同样在锁内做新鲜性检查，避免并发推进 generation 后仍报告
“No changes”。

### P0-02：stateful latest rollback 的 after-state drift 与 `--force`

默认在锁内、journal 与第一笔远端 mutation 之前，按 manifest snapshot 校验远端
after 状态（exists / sha256 / mode）。发现第三方内容时拒绝，零 mutation。

- CLI `rollback --force` 贯通到 `StateRollbackService.rollback_latest(force=…)`
- `force=True` 允许继续，并把真实第三方远端 bytes 持久化为 recovery backup
- 中途失败时恢复到 rollback 前的第三方内容，而不是 manifest after bytes

### P0-03：FTPS 默认验证服务器证书与主机名

`FtpTransport` 在 FTPS 模式默认使用：

```python
ssl.create_default_context(...)  # CERT_REQUIRED + check_hostname
```

仅当显式 `tls_verify=false` 时允许兼容旧服务器，并输出明显安全警告。
支持 `tls_ca_file` / `tls_cert_file` / `tls_key_file`；`ftps_tls_trust_digest`
汇总信任边界配置且不包含私钥或密码。

## 测试与门禁

- 两执行者非重叠路径 stale-plan 竞态
- rollback after 匹配 / 缺失 / hash / mode drift / force / force 中途恢复
- FTPS verified context、insecure opt-out 警告、未受信证书连接失败
- 全量 `uv run pytest` 通过（本发布环境 359+）

## 兼容性

- Python 3.11+；配置格式与 v0.3.0 兼容
- FTPS 默认从“不验证证书”变为“必须验证”；旧自签服务器需显式
  `tls_verify=false` 或提供 `tls_ca_file`
- stateful rollback 默认更严格；存在人工热修复时需 `--force`

## 本版本明确不包含

Textual TUI、非最新 deployment 回滚、自动 GC、PyPI 发布，以及审计中的完整
P1 批次（rollback manifest 布局、stateful post_commands/health、SFTP rename
fallback 收窄、history/verify all、doctor 契约收口）——可在后续小版本跟进。
