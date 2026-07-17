# git-deploy v1.3.0

v1.3.0 为 SFTP Target 增加受控的 `after_deploy`，让一次部署在同步文件后完成必要的 Reload、Restart 或健康检查，同时保持 v1-lite 的顺序执行、延迟 State 和失败重跑边界。

## 受控远程命令

- 每个 SFTP Target 可配置最多 16 条、单条最多 4096 字符的单行 `after_deploy` 命令和正数 `command_timeout`。
- FTP、空命令、NUL/换行、超量或超长命令在本地配置加载阶段直接拒绝。
- Single Plan、Workspace Combined Plan 与交互确认均显示命令；dry-run 零远端执行。
- 命令在文件操作之后、State 提交之前顺序执行；失败停止后续命令、保留旧 State，且不会自动重试。
- No-op 不连接、不执行命令，避免无变化部署重启服务。

## SSH 后端

- Native OpenSSH 通过现有 ControlMaster 执行 `ssh -T`，继续固定已审阅的 Host/User/Port，继承 ProxyJump/ProxyCommand 与 1Password Agent 环境，不建立第二次认证。
- Paramiko Direct Host 复用同一个 SSHClient Exec Channel，不申请 PTY、关闭 stdin，实时转发 stdout/stderr 并读取 Exit Status。
- 两个后端默认在 `remote_root` 下执行，支持整个命令的明确超时。

## Workspace 与 State

- 每仓严格按 Files → Commands → State 顺序完成后才进入下一仓。
- A 成功、B 命令失败时 A State 已提交、B State 保持旧值、C 不执行；重跑后 A No-op，B/C 自然收敛。
- Workspace 内相同 Native Endpoint 的文件与命令继续共享一个命令级 Connection Pool。

## FTP 收尾

- 同一连接缓存已确认缺失的父目录，多个旧文件属于同一已删除目录时只探测一次。
- `root_exists()` 复用 FTP 三态目录探测，Permission 不再被 Doctor 误报为 Missing。

## 安全与验证

- 文档要求非交互、尽量幂等的命令；sudo 使用 `sudo -n` 与最小 `NOPASSWD` allowlist。
- 不增加 before/on-failure hooks、自动命令重试、自动回滚、交互 Shell、Secret 插值或 Workspace 全局命令。
- 自动验证覆盖 Fake Transport 编排、本机 FTP、隔离容器 OpenSSH 的 Paramiko/Native Exec、Python 3.11/3.12、Ruff、ty、wheel/sdist 与隔离安装。
