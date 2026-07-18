# git-deploy v1.4.3

v1.4.3 是 Hybrid v1.4 系列的小型安全收口版本，修复 Native OpenSSH 后端在 Planned-Missing 最后一刻竞态中可能覆盖外部目标的问题。

## Native OpenSSH No-overwrite

- `OpenSSHSFTPTransport.rename_path()` 强制使用 OpenSSH 的传统 SFTP Rename，不再自动协商具有覆盖语义的 POSIX Rename 扩展。
- 保留 Rename 前的 Destination Preflight；即使外部目标在 Preflight 后、远端原子 Rename 前出现，Rename 也会失败。
- Stage 保持可恢复，外部 Final、旧 Ownership 与 Local State 均不推进；显式 `--recover` 只清理 Stage/Recovery，不删除外部 Final。
- Backup、Stage Publish 与 Recovery Restore 都经由同一个 No-overwrite Transport 契约。

## 验证

- 单元测试捕获并校验 Native SFTP Batch、路径空格与引号转义以及 No-overwrite 命令契约。
- 真实 Native OpenSSH 集成测试在 Destination `lstat` 后、Batch Rename 前创建外部文件，验证失败、恢复和状态不变量。
- 真实测试在 WSL2、Ubuntu 24.04 与系统 OpenSSH 客户端上执行；CI 的容器化 OpenSSH Fixture 同时覆盖远端协议行为。

## 兼容性与边界

- 配置、Local State、Ownership 与 Recovery Schema 均无变化。
- Paramiko 后端行为不变。
- 本版本不引入远端租约或多发布器协调；部署期间仍不得由其他发布器修改已存在的 Hybrid 受管路径。
