# git-deploy v1-lite Scope

v1-lite 的长期产品边界是“构建、找差异、上传”。

必须保持：

- 默认 `git-deploy` 完成 Build → Plan → Upload/Delete → State；
- Git 管源码历史，轻量 Local State 管未入 Git 的增量产物；单 Hybrid 的 Remote Ownership Manifest 只记录可删除的远端直接子项；
- SFTP/FTP 逐文件幂等操作与重试；
- 未知远端内容永不删除；
- 所有操作成功后才原子提交 `.git/git-deploy/<target>.json`；
- 失败恢复方式是重新执行同一条命令。
- Thin Workspace 只编排独立仓库的顺序与统一 Target；每仓保留自己的配置、Git、State 和 Lock。
- SFTP Hybrid 只支持一个本地聚合根：直接文件增量、直接目录完整 Mirror，未知远端内容永不处理；
- Recovery Record 只服务当前 Hybrid Stage/Swap 的恢复或清理，不形成历史和回滚系统。

明确不做：

- CI/CD、审批、RBAC、Web/TUI；
- Expected State、Generation、CAS、Transaction 或完整 Root Deployment Manifest；
- History、Verify、通用 Recover、自动 Rollback；
- Workspace 全局 State、跨仓事务、依赖图、Target Map 或默认并行；
- Docker/1Password Build Provider、构建缓存或沙箱；
- FTPS、FTP Hybrid、通用远端 Hook、Health URL、Owner/Group 管理；
- Root Mirror、完整远端扫描、多 Hybrid 同根协调和发布事务。

旧 v0.3 实现保存在 `legacy/v0.3` 分支和 v0.3.x tags，新主线不提供运行时兼容层。
