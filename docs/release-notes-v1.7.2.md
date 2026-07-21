# Release Notes v1.7.2

## 变更

### Bootstrap 批次与观测加固（v1.7.1 审计 P2）

- **Target Lock I/O 隔离**：`lock.acquire()` 的 `PermissionError` / `OSError`（mkdir、open、fsync 等）与持有冲突一样转为单条目 FAIL，不再中止整批；`lock.release()` 的 I/O 失败不再把已成功的 Probe 改写成进程崩溃。
- **外层 Batch Continue**：`execute_bootstrap_item` 的意外逃逸被收成 FAIL 行，后续 Target 继续执行（best-effort 批次契约）。
- **Plan 输出 Fail-closed**：Plan 打印失败（BrokenPipe / OSError / Unicode / ValueError）在确认与远端变更前以 `ConfigError` 拒绝执行。
- **Summary 输出 Fail-open**：Summary 打印失败不影响已计算的退出码；远端 Profile/Root 已成功时命令仍按真实结果退出。

## 迁移

从 v1.7.1 升级即可，无配置或 Capability Profile schema 变更。管道关闭场景下，自动化若曾依赖 Summary 打印异常作为失败信号，应改为依赖进程退出码。
