# git-deploy v0.3.3

v0.3.3 是 v0.3.2 之后的稳定收口版本，对照
`docs/git-deploy-latest-main-deep-audit-v0.3.2-2026-07-15.md`
逐项处理 P1 可靠性问题，**不新增功能**。

实现完成后用三个独立视角（逐行扫描、被删除行为审计、跨文件调用链追踪）
对本版全部 diff 做了一轮交叉代码审查，发现并修复了三处实现过程中引入的
真实回归：rollback 生命周期失败路径的 CAS/journal 收尾不再被异常处理覆盖
（P1-05 段说明）、SFTP posix_rename fallback 的错误分类启发式两头都不可靠
（P1-06 段说明）、以及 FTP/FTPS 的 `replace_file_stream` 存在与 SFTP 修复前
相同的破坏性 delete-then-rename 顺序（P1-06 段说明）。三处均已修复并补充
回归测试。

## 审计结论核验

审计文档的 P0 发现（"首次 artifact baseline 会使当前 reviewed plan 自己变
stale"）在 `d4cd6989`（v0.3.2 精确提交）上**未复现**：本版新增的 CLI 集成
回归测试
`test_first_artifact_baseline_deploy_failure_does_not_report_self_stale_plan`
证明 `_execute_reviewed_domain_deploy` 中已有的 `replace(source_plan, ...)`
重冻结逻辑已经在 baseline 提交后正确重新绑定 before-boundary，一次 `deploy`
命令即可完成首次 baseline + 部署，不产生工具自身触发的 `stale_plan`；baseline
之后的失败也不会误报为 stale_plan，且 baseline 作为独立 durable transition
在失败重试后仍然保留。已有测试
`test_host_build_deploy_bootstraps_artifacts_and_rolls_back_unified_state`
此前已覆盖成功路径，本版补齐失败路径。

## 修复内容

### P1-09：统一 StalePlanError 类型

`StalePlanError` 从裸 `ValueError` 改为 `errors.py` 中的
`GitDeployError → PolicyError → StalePlanError` 层级（`exit_code=2`），
`application/plan_token.py` 改为从 `..errors` 重新导出，保持所有既有
`from .plan_token import StalePlanError` 导入路径不变。`application/errors.py`
的 `application_error_from_exception` 新增显式分支，映射为
`operation.stale-plan` / `POLICY` 而非落入 `internal.unexpected`。
`state_guards.py`、`state_rollback.py`、`cli.py` 中原先用字符串前缀
`"stale_plan: ..."` 规避类型系统的 `PolicyError` 调用点，改为直接抛出
`StalePlanError`。

修复前，`application/plan_token.py` 里由 token/plan 不匹配触发的
`StalePlanError`（`ValueError` 子类）不会被 CLI `run()` 的
`except ApplicationError` / `except GitDeployError` 捕获，会作为未捕获异常
崩溃退出而不是给出结构化错误和稳定退出码。

### P1-01：Git 对象落盘顺序调整到 freshness gate 之后

`cli.py::_execute_reviewed_domain_deploy` 中 `_materialize_reviewed_object_env`
（写入 durable Git 对象）与 `_require_reviewed_plan_fresh_under_lock`
（锁内 freshness 校验）的调用顺序对调：freshness 先行。Stale plan 现在在
本工具自己写入任何 durable Git 对象之前就被拒绝。

### P1-02：pre-connect 竞态窗口保证表述

评估后未采用"持锁跨越远端连接"的重构方案（该方案需要把 SSH Agent 认证等
网络等待纳入本地文件锁持有期间，个人工具场景下持锁挂起的风险高于收益）。
改为在 README 安全模型和 `_require_reviewed_plan_fresh_under_lock` 文档中
明确表述保证边界：stale plan 保证零远端 mutation；多数已知竞态在连接前拒绝；
不保证所有竞态都零 connect。

### P1-06：SFTP/FTP posix_rename fallback 安全收窄

`SftpTransport.replace_file_stream` 不再对 `posix_rename` 抛出的任意
`OSError` 无条件执行「删除线上 target 再 rename」。新增 `_publish_temporary`：
`posix_rename` 失败（不管错误原因是扩展不支持、权限、网络还是服务端错误）
一律走非破坏性的两步 rename：target → 备份名 → temp → target，最终 rename
失败时自动把备份改回原名，从不直接删除线上 target。（首版实现曾尝试只在
错误文本明确指向"扩展不支持"时才进入 fallback，独立代码审查发现这个文本
启发式两头都不可靠——一次误判会把后续每次写入都永久缓存为"降级"，而通用
错误文本又会导致该服务器每次写入都硬失败退出；由于 fallback 本身已经是
无损可恢复的，不再需要靠猜测错误原因来决定是否进入，已改为上述更简单也
更安全的版本。）`FtpTransport.replace_file_stream`（含 FTPS）此前也是先
`delete(remote_path)` 再 rename 的破坏性顺序，同一次审查中发现并按相同的
备份改名协议修复。

### P1-08：FTPS 证书路径 config-relative 解析与结构化错误

`config.py` 新增 `_resolve_ftps_tls_paths`，让 `tls_ca_file`/`tls_cert_file`/
`tls_key_file`（以及 `build_ftps_ssl_context` 同时兼容的未文档化别名
`ca_file`/`cert_file`/`key_file`，仅在 `protocol = "ftps"` 时生效，避免与
SFTP 自己的 `key_file` 语义冲突）遵守与其他配置路径一致的"相对于
deploy.toml 所在目录"规则，并在 `protocol = "ftps"` 时于配置加载阶段校验
文件存在且可读。`transport.py::build_ftps_ssl_context` 包裹
`ssl.SSLContext` 构造，把 PEM 损坏、cert/key 不匹配等原本会抛出的
`ssl.SSLError`/`OSError` 转成 `ConfigurationError`。

### P1-05：Rollback 补齐 post_commands 和 health_urls 生命周期

`state_executor.py` 抽出共享的 `run_post_write_lifecycle` 和
`restore_backup_entries` 模块函数（`StateDeploymentExecutor` 原有方法
改为薄封装）。`StateRollbackService.rollback_latest` 在全部快照写入并
read-back 校验通过后、推进到 `remote_verified`/CAS 之前，运行与部署完全
相同的 `post_commands`/`health_urls` 生命周期；失败时用本次 rollback 自己
捕获的「rollback 前真实 bytes」自动恢复，generation 不推进，双重恢复失败
则落 `manual_recovery_required` 交给 `state recover`。（独立代码审查发现
首版实现把 `remote_verified` → CAS → `state_committed` → `recovered` 的
推进序列移到了保护性 try/except 之外：若全部写入和生命周期都已成功，只有
本地 journal/CAS 记账失败，会变成未捕获异常直接崩溃而不是给出可操作的
`state recover` 提示。已补上对应 try/except 并新增回归测试。）

### P1-04：doctor/history 不再误报 rollback 恢复目录

`deployments/rb-<id>/` 是 `StateRollbackService` 捕获的 rollback 前置备份
目录，按设计没有 `manifest.json`。`doctor_checks.py::manifests()` 和
`history_service.py::_corrupt_manifest_paths` 现在识别 `rb-` 前缀并跳过，
不再当作缺失/损坏的 deployment manifest 报告；doctor 新增
`rollback_event_counts` 上下文字段单独统计。

### P1-03：Rollback 的 drift 校验与 backup 捕获合并为单次读取

`state_rollback.py` 原来的 `_require_remote_after_state`（读一次判断 drift）
与 backup 捕获循环（再读一次）合并为 `_observe_remote_before_mutation`：
每个远端路径在 mutation 前只读一次，返回的 bytes 同时用于 after-state
校验和 backup 写入，消除两次独立读取之间的 TOCTOU 窗口。（Deploy 路径的
`evaluate_drift`/`prepare` 保留原有两次读取设计——合并需要更大幅度重构且
会把当前逐文件流式处理改为一次性持有全部文件字节，对大文件部署有内存回归
风险，评估后本版未处理，留待后续版本按 spool 方案单独设计。）

### P1-07：真实 FTPS 协议测试覆盖

新增开发依赖 `pyftpdlib` + `pyOpenSSL`，实现真实的显式 FTPS 测试服务器
（`_RealFtpsServer`，控制通道和数据通道都强制 TLS）。新增：受信 CA 完整
STOR/RETR/DELE roundtrip、真实过期证书握手拒绝、真实 hostname 不匹配握手
拒绝、`tls_verify=false` 逃生舱确实能连接自签名证书服务端。未覆盖客户端
证书 / mTLS（超出本版时间范围）。

### GitHub Actions CI

新增 `.github/workflows/ci.yml`：Python 3.11 / 3.12 矩阵，
`uv lock --check` → `pytest` → `ruff` → `ty` → `uv build` → 隔离 wheel
安装冒烟测试。

## 测试与门禁

```bash
uv lock --check
uv run pytest -q       # 384 passed（Python 3.11、3.12 均验证）
uvx ruff check src tests
uvx ty check src
uv build --clear
```

## 兼容性

- Python 3.11+；配置与 v0.3.2 兼容，无需迁移。
- `StateRollbackService.rollback_latest()` 新增可选 `fail_at` 测试注入参数，
  不影响现有调用方。
- 新增开发依赖 `pyftpdlib`、`pyopenssl`、显式声明 `cryptography`（仅
  `dependency-groups.dev`，不影响发布 wheel 的运行时依赖）。

## 本版本明确不包含

Deploy 路径 drift+backup 单次读取合并（P1-03 的另一半，见上文说明）、
客户端证书 / mTLS FTPS 测试、`history all` / `verify all --latest` 修复、
doctor 对 `auto_rolled_back` 状态识别、doctor remote check 递归扫描问题、
application transaction event 完整接线（P1-10）、Textual TUI、非最新
deployment 回滚、自动 GC、PyPI 发布。
