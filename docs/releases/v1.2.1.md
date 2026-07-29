# git-deploy v1.2.1

v1.2.1 是针对 v1.2.0 深度审计结果的安全 Hotfix，修复 Native OpenSSH 连接边界、删除状态语义和 Thin Workspace 跨仓远端所有权。

## 发布阻断修复

- Prepare 后、真实连接前重新解析 SSH Alias；HostName、User 或 Port 漂移时在建立 ControlMaster 前失败，State 保持不变。
- OpenSSH 命令保留 Alias 的 Identity/Proxy/Match 上下文，同时固定计划中已审阅的 HostName、User 和 Port。
- Native SFTP 路径探测改为 Exists/Missing/Error 三态；只接受带目标远端路径的 C-locale 缺失诊断，Permission、Network、Timeout 和失效 Control Socket 全部失败并保留删除意图。
- Workspace 在首个 Build/Lock/Connect 前解析全部物理 Target，拒绝同一 Endpoint 上相同或父子嵌套的 Remote Root。

## 连接与审阅可靠性

- Connection Pool 复用前检查 Master 健康度；失败连接可精确驱逐，Operation Retry 会建立新 Master。
- Target `timeout` 仅作为 OpenSSH `ConnectTimeout`；认证等待与完整 SFTP Batch 不再受默认 15 秒 Python 进程超时影响。
- Combined Plan 显示 Alias/Frozen Endpoint、Remote Root、Full/Incremental、Commit Boundary 和 Frozen Bytes。
- Doctor Target 解析失败后跳过远端；Workspace Doctor 在全部 Target 预检成功前不会连接或执行 `--create-root`。

## 边界收紧

- Workspace Repository Name 限制为最长 64 位的 `[A-Za-z0-9._-]+`。
- 平铺 CLI 保留字 `build`、`doctor`、`init` 不再允许作为 Target Name。
- Deploy 路径显式拒绝 Doctor 专用的 `--create-root`。
- FTP 删除先列出父目录确认文件存在，不再依赖英文 550 文本判断 Missing。
- Native SFTP 大文件在阻塞 Batch 开始时立即显示当前文件进度，结束后报告 100%。

## 验证

- Alias Host/User/Port 漂移、零 Master/Mutation/State；
- Missing/Permission/Dead Master/Network/Timeout 三态分类和删除意图保留；
- Pooled Master 健康检查、驱逐、实际 Operation Retry 第二次建连；
- 同 Root、双向父子 Root、FTP、解析后同 Endpoint 冲突及 Sibling/Different Endpoint 放行；
- Combined Plan、磁盘容量、Repository/Target Name、Doctor/CLI 参数边界；
- 容器化真实 OpenSSH Alias、严格 Host Key、非默认端口、ProxyJump、ControlMaster、首次部署和 No-op；
- 本机 FTP、Python 3.11/3.12、Ruff、ty、wheel/sdist 和隔离安装。
