# git-deploy v1-lite Scope

v1-lite 的长期产品边界是“构建、找差异、上传”。

必须保持：

- 默认 `git-deploy` 完成 Build → Plan → Upload/Delete → State；
- Git 管源码历史，轻量 output manifest 管未入 Git 的构建产物；
- SFTP/FTP 逐文件幂等操作与重试；
- 未知远端内容永不删除；
- 所有操作成功后才原子提交 `.git/git-deploy/<target>.json`；
- 失败恢复方式是重新执行同一条命令。
- Thin Workspace 只编排独立仓库的顺序与统一 Target；每仓保留自己的配置、Git、State 和 Lock。

明确不做：

- CI/CD、审批、RBAC、Web/TUI；
- Expected State、Generation、CAS、Transaction、Deployment Manifest；
- History、Verify、Recover、自动 Rollback；
- Workspace 全局 State、跨仓事务、依赖图、Target Map 或默认并行；
- Docker/1Password Build Provider、构建缓存或沙箱；
- FTPS、远端 Hook、Health URL、Owner/Group 管理。

旧 v0.3 实现保存在 `legacy/v0.3` 分支和 v0.3.x tags，新主线不提供运行时兼容层。
