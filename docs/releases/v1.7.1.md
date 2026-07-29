# Release Notes v1.7.1

## 变更

### Bootstrap 成功语义收口

- **Unknown Target Filter**：拼写错误或混合合法+非法 Target 名在任何远端连接前以 `ConfigError` 失败，并列出全部未知名称；空 filter 与合法 filter 行为不变。
- **Git Repository Gate**：删除伪 `.git` fallback；Project 非 Git 不得 connect/probe；Workspace 中坏仓变成 FAIL 行，其他仓继续，整体有失败则非 0 退出。
- **异常隔离**：加锁后 factory/connect 异常转为单条目 FAIL 并释放锁，后续 Target 继续。
- **READY 最终校验**：执行阶段对 READY 加锁并重新 Inspect Profile 与 Pending；确认窗口后 Profile 消失、Banner 漂移或 Pending 出现时 FAIL，而非静默 READY；`--force`/REPROBE 仍不绕过 Pending。

### 文档

- Create Root 成功但后续 Probe 失败时，已创建的 Remote Root 会保留；重跑 Bootstrap 可继续 Probe（不递归删除远端 Root）。

## 迁移

从 v1.7.0 升级即可，无配置/Profile schema 变更。自动化脚本若依赖错误 Target 名静默 SKIP 成功，升级后会失败并需修正 filter。
