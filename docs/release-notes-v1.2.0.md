# git-deploy v1.2.0

v1.2.0 增加 Thin Workspace，在不合并配置、State 或 Git 历史的前提下，用一条命令安全编排多个独立仓库。

## Thin Workspace

- 新增最小 `deploy.workspace.toml`，只保存统一默认 Target、仓库名称、相对路径和部署顺序。
- 当前目录自动识别单仓或 Workspace；两种配置同时存在时要求显式使用 `--config` 或 `--workspace`。
- 每仓继续独立拥有 `deploy.toml`、Build、Output Mapping、Git Common Dir State 和 Target Lock。
- 支持 Workspace Deploy、Dry-run、Doctor、Build 和总计 Summary。

## 安全执行模型

- 在任何远端连接前完成所有仓库的 Target 预检、Build、Plan 和 Upload 字节冻结。
- 全部 Prepare 成功后渲染 Combined Plan，只确认一次，再按 Workspace 顺序逐仓部署。
- A 成功、B 失败时 C 不执行；每仓只在自身成功后提交 State，直接重跑即可让 A No-op 并继续 B/C。
- 无全局 State、全局事务、自动回滚、依赖图、Target Map 或默认并行。

## Native OpenSSH 复用

- Workspace 在整条命令内共享 `SSHConnectionPool`。
- 使用相同 Alias、有效 Host/User/Port、OpenSSH 配置和系统命令的仓库共用一条 ControlMaster；不同 `remote_root` 不会阻止复用。
- Pool 在命令结束或失败时统一清理，不引入后台服务或跨进程连接。

## 验证

- Workspace 配置边界、顺序、自动发现与歧义拒绝；
- 全仓 Target 预检先于 Build，全部 Prepare 先于 Remote Connect；
- Combined Plan、Confirm Once、顺序部署、冻结字节与共享 Pool；
- 部分失败后的独立 State 提交、后续仓库停止和重跑自然收敛；
- 单仓兼容、Python 3.11/3.12、Ruff、ty、wheel/sdist 和隔离安装。
