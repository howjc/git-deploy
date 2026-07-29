# Release Notes v1.7.3

## 变更

### Bootstrap 真实 stdout 管道契约（v1.7.2 审计 P1）

- **强制 Flush**：Plan / Summary 经 TextIO `write` + `flush` 发出，块缓冲管道上的 BrokenPipe 在远端变更前可见。
- **Plan Fail-closed**：Plan 写入或 flush 失败时以 `ConfigError` 拒绝确认与远端写（Remote Mutation = 0）。
- **Summary Fail-open**：Summary flush 失败时保留已计算的 0/1 退出码，不出现解释器 Shutdown 的 120。
- **Broken Pipe 静音**：输出失败后将 stdout 重定向到 `/dev/null`，避免进程退出二次 flush 改写退出码。
- **真实子进程测试**：覆盖 `head -c 0` 管道、summary 提前关闭、以及 write 成功但 flush 失败的缓冲流模型。

### TargetLock 部分获取清理（v1.7.2 审计 P2）

- flock 成功后 metadata / fsync 失败时显式 `LOCK_UN` + close，后续可立即重新获取锁。

## 迁移

从 v1.7.2 升级即可，无配置或 Capability Profile schema 变更。管道自动化可依赖退出码；`bootstrap --yes | early-close-consumer` 不再在成功后误报 120。
