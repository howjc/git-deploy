# git-deploy v0.3.0

v0.3.0 把重点从“大型交互界面”转回个人和小团队的日常脚本体验，同时保留
v0.2.1 的 physical-target state、事务恢复、构建产物和多环境能力。

## 主要变化

- `plan PROJECT`、`deploy PROJECT` 默认从各项目可信 current 选择缺失提交直到当时
  的 `HEAD`；执行和 history 使用冻结后的完整 commit hash。
- 新增 `doctor PROJECT|all`。默认只读本地配置、Git、current/CAS、manifest/backup
  和 transaction；`--check-remote` 才执行只读远端连接/目录检查。
- `rollback PROJECT` 默认等价于 `--latest`；v0.3.0 仍只自动回滚最新成功记录。
- 重复 implicit/explicit deploy 成为明确的 `No changes`/exit 0，不连接远端、不写
  transaction、manifest 或 current。
- history 会报告损坏记录；rollback 在远端 I/O 前验证 backup hash；manual recovery
  输出 hash-only evidence 和安全检查命令。
- 新增隔离 OpenSSH 容器门禁，真实验证 SFTP 原子上传、增删改、mode/owner/group、
  drift、latest rollback 和权限失败；同时保留 FTP/FTPS fake contract 矩阵。

## 兼容性和升级

- Python 仍要求 3.11+，配置格式与 v0.2.1 兼容。
- 显式 `--revisions COMMIT` / `FROM..TO` 仍受支持；包含 `HEAD` 的 selector 会在 plan
  时冻结为完整 SHA。
- 原 `rollback PROJECT --latest` 仍可使用。
- 升级前备份 `deploy.toml` 和 state，安装 v0.3.0 后先运行 `git-deploy doctor`，再在
  非关键 dev target 做 plan、deploy、verify、latest rollback 演练。
- 没有可信 current 的 target 不会猜测基线；按错误提示执行 revision 或 empty
  bootstrap。

## 本版本明确不包含

Textual TUI、非最新 deployment 派生回滚、自动 state GC、Web UI/RBAC 和数据库
migration 自动回滚继续冻结。它们不会作为隐藏依赖影响 v0.3.0 的日常 CLI。
