# git-deploy v1.2.2 最新代码审计与 SFTP 远程命令设计

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`020d59f855b9d62ff26435fbbce6b696a829afc3`
> 版本：`v1.2.2`
> 审计日期：`2026-07-17`
> 审计结论：**通过，可进入稳定日常使用；建议后续以 v1.3.0 增加受控的 SFTP/SSH after-deploy 命令**

---

## 1. 执行摘要

v1.2.2 是一次范围清晰的收口版本，没有扩大 Thin Workspace，也没有重新引入复杂的发布状态机。

本次主要修复：

1. FTP 同一父目录下的大批量删除只执行一次完整 `NLST`；
2. FTP 删除能够区分父目录存在、父目录缺失和探测错误；
3. FTP 空目录返回 `550` 时能够通过 `CWD` 继续确认；
4. FTP 明确权限错误仍然 Fail Closed；
5. 上传、删除和创建目录后能够维护连接级目录缓存；
6. 连接重建和关闭后清空 FTP 缓存；
7. Workspace `build` 恢复为纯本地操作；
8. Workspace Build 不再解析 SSH Alias、要求 `ssh/sftp` 或检查远端目录所有权；
9. OpenSSH ControlMaster 在认证阶段被 Ctrl-C 中断时清理本地私有 Socket 目录；
10. 对跨仓物理目标全局锁和远端临时文件残留给出了明确 ADR 与文档边界。

本轮没有发现 P0 或影响主链正确性的 P1。

综合评价：

```text
单仓 SFTP / Native OpenSSH        通过
WSL + 1Password SSH Agent         通过
Thin Workspace                    通过
FTP 常规上传和删除                 通过
FTP 大批量 Hash Asset 删除         通过
Workspace Build-only              通过
跨设备并发部署                      不保证，属于明确非目标
```

v1.2.2 已经适合成为当前稳定基线。

---

# 2. 审计方式与限制

本轮通过 GitHub Connector 获取和检查：

- 最新 Commit；
- v1.2.1 → v1.2.2 Commit Diff；
- 当前 Main 源码；
- `v1.2.2` Tag；
- `pyproject.toml`；
- FTP Transport；
- Workspace；
- Native OpenSSH Transport；
- 单元测试；
- Release Notes；
- ADR。

本地尝试执行：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

当前执行环境仍无法解析 `github.com`，所以无法独立运行：

```bash
uv lock --check
uv run pytest -q
uv run ruff check src tests
uv run ty check src
uv build --clear
```

此外，v1.2.2 是直接位于 `main` 上的单个 Release Commit，GitHub Connector 没有返回与该 Commit 关联的 PR Workflow Run 或 Combined Status。

因此本报告的动态测试结论来自仓库中的测试实现和 Release Notes，不能冒充为本轮独立复跑结果。

---

# 3. 版本与发布一致性

## 3.1 Main

最新 Commit：

```text
020d59f855b9d62ff26435fbbce6b696a829afc3
release v1.2.2: close latest audit findings
```

它基于：

```text
9af7be31cba487ad46d50cb143447df43f415bc1
v1.2.1
```

只有一个增量 Commit。

## 3.2 Package Version

当前 Main：

```toml
[project]
name = "git-deploy"
version = "1.2.2"
```

`v1.2.2` Tag 中的 `pyproject.toml` Blob 与 Main 相同。

未发现：

- Main 与 Tag 代码不一致；
- Package Version 未更新；
- README 安装链接版本落后；
- Release Notes 版本不一致。

---

# 4. v1.2.1 剩余问题修复验证

## 4.1 FTP 大批量删除缓存

### 原问题

v1.2.1 每删除一个文件都执行：

```python
ftp.nlst(parent)
```

同一目录删除 120 个文件时，会完整扫描 120 次。

### 当前实现

FTPTransport 新增：

```python
self._directory_entries: dict[str, set[str]]
```

第一次探测目录：

```text
NLST parent
    ↓
保存完整 child name set
```

后续相同 Parent 的 Delete：

```text
读取缓存
    ↓
不再调用 NLST
```

成功 Delete 后：

```text
cache.discard(name)
```

成功 Upload 后：

```text
cache.add(name)
```

连接建立或关闭时：

```text
cache.clear()
```

### 测试

测试创建 120 个文件并逐个删除，断言：

```text
NLST calls == ["/root/assets"]
Deleted files == 120
```

### 评价

**通过。**

复杂度由接近：

```text
O(D × N)
```

收敛到：

```text
O(N + D)
```

其中：

- `N`：首次目录列表大小；
- `D`：待删除文件数量。

---

## 4.2 FTP 缺失父目录和空目录

### 当前实现

FTP Directory Probe 定义：

```text
EXISTS
MISSING
ERROR
```

`NLST` 失败后：

1. 明确 Permission/Access 错误：`ERROR`；
2. 尝试 `CWD absolute`；
3. `CWD` 成功：目录存在但为空；
4. `CWD` 失败：检查最近可列出的父目录；
5. 父目录中不存在该名称：`MISSING`；
6. 父目录中存在该名称但无法进入：`ERROR`。

### 结果

父目录已经被人工删除：

```text
Delete = idempotent success
```

空目录的 `NLST` 返回 `550 No files found`：

```text
CWD success
    ↓
EXISTS with empty entries
    ↓
target file absent
    ↓
Delete success
```

Permission Denied：

```text
DeployError
State unchanged
```

### 评价

**通过。**

它兼顾了：

- 失败重跑收敛；
- 避免把权限错误当成 Missing；
- 不依赖单个 FTP Server 的固定英文 “No such file”。

---

## 4.3 FTP Cache 一致性

当前实现正确处理：

### Upload

成功完成 `STOR` 后：

```text
已缓存 Parent
    → 添加新文件名

Parent 尚未缓存
    → 不伪造不完整缓存
```

### Delete

成功 Delete 后：

```text
从 Parent Cache 删除文件名
```

### Mkdir

成功创建目录后：

```text
已缓存的 Parent 添加目录名
新目录缓存为空集合
```

### Retry / Reconnect

`invalidate_connection()` 最终调用 `close()`：

```text
清空所有目录缓存
重新连接后重新探测
```

### 评价

**通过。**

缓存只在一个 FTP Connection 生命周期内有效，边界合理。

---

## 4.4 Workspace Build 纯本地边界

### 原问题

v1.2.1：

```text
git-deploy build
    ↓
Resolve SSH Alias
Check ssh/sftp
Check Remote Root Ownership
```

导致本地 Build 依赖远端部署环境。

### 当前实现

`run_workspace_build()`：

1. 逐仓 `load_config()`；
2. 用户没有传 Target：不选择 Target；
3. 用户显式传 Target：只验证每仓都存在该名称；
4. 加载全部配置成功后；
5. 按 Workspace 顺序运行 Build。

不会执行：

```text
resolve_target_for_plan
ssh -G
OpenSSH Tool Discovery
Git Validate
Remote Root Ownership
Remote Connect
```

### 测试

测试显式阻止：

- `resolve_target_for_plan`；
- `_validate_native_tools`；
- `_validate_remote_ownership`；
- `GitRepository.validate`。

即使：

- 没有 Workspace Default Target；
- Alias 无效；
- 远端 Root 父子重叠；
- 没有 SSH Tool；

两个仓库仍能完成本地 Build。

### 评价

**通过。**

这重新符合产品职责：

```text
build = local
deploy = local + remote
doctor = diagnostics
```

---

## 4.5 OpenSSH Ctrl-C 清理

ControlMaster 建立前会创建：

```text
0700 private directory
control.sock path
```

当前代码对：

```python
except BaseException:
```

也执行：

```python
shutil.rmtree(directory, ignore_errors=True)
```

因此 KeyboardInterrupt 不再绕过本地目录清理。

### 评价

**通过。**

远端已经 Fork 成功但本地进程恰好被中断的极端竞态仍由 OpenSSH 自身负责，不值得为个人 CLI 增加后台进程管理系统。

---

# 5. 保持正确的旧安全边界

本轮未破坏以下能力：

## Git 与 Build

- Build Failure 时零 Remote Connect；
- Source 固定为 Committed HEAD；
- Dirty Worktree 不会替换 Source Bytes；
- Output 在 Connect 前冻结和 Hash 复核；
- Rename = Delete + Add；
- Output Root Missing = Fail Closed；
- Source/Output Ownership Collision = Fail Closed。

## State

- State 保存在 Git Common Dir；
- 多 Worktree 共享 State；
- 每个 Target 独立 State；
- 远端操作全部完成后才 Save；
- 失败后 State 保持旧值；
- 重新执行自然收敛。

## Workspace

- 每仓独立 Config、Git、State 和 Lock；
- 全部 Target/Root 预检发生在 Build 前；
- 所有 Lock 在第一个 Build 前获取；
- Prepare All；
- Combined Plan；
- Confirm Once；
- Sequential Deploy；
- Partial Failure 后 Rerun Convergence。

## Native OpenSSH

- 系统 POSIX `ssh/sftp`；
- WSL Agent 环境继承；
- 1Password SSH Agent；
- Alias 重解析；
- Host/User/Port Pinning；
- ControlMaster；
- Connection Pool；
- Dead Master Eviction；
- Retry Reconnect；
- ProxyJump/ProxyCommand；
- ConnectTimeout 与 Transfer 生命周期分离。

---

# 6. 剩余低风险问题

## P2-01：Missing Directory Probe 没有缓存

当前缓存只保存：

```text
存在的目录 → entries set
```

没有保存：

```text
确认 Missing 的目录
```

如果 State 中有大量文件属于同一个已经被人工删除的父目录：

```text
old-dir/a.js
old-dir/b.js
old-dir/c.js
...
```

每个 Delete 仍可能重复执行：

```text
NLST old-dir
CWD old-dir
```

虽然已经避免了 `O(D × N)` 的大目录完整扫描，但 Missing Parent 情况仍是：

```text
O(D) remote probes
```

### 建议

后续可增加：

```python
_missing_directories: set[str]
```

同一连接内确认 Missing 后缓存。

成功创建该目录时移除 Missing 标记。

### 优先级

低。

---

## P2-02：FTP root_exists 仍把所有 550 视为 Missing

`root_exists()` 当前逻辑：

```python
except error_perm:
    if response startswith 550:
        return False
```

Doctor 可能把：

```text
550 Permission denied
```

显示为：

```text
root missing
```

部署路径的 `ensure_root()` 会进一步通过 CWD 检查并 Fail Closed，因此不会静默推进 State。

### 影响

主要是 Doctor 诊断准确性。

### 建议

让 `root_exists()` 复用 `FTPDirectoryProbe` 三态。

### 优先级

低。

---

## P2-03：极少数 FTP Server 的隐藏目录语义

当前缺失父目录判断会参考最近可列出的 Ancestor。

如果某 FTP Server：

- 允许列 Parent；
- 但故意隐藏一个实际存在、无权访问的 Child；
- 同时返回无法分类的通用 550；

工具可能把 Child 解释为 Missing。

这是 FTP 协议和 Server 行为差异带来的极端边界。

### 控制条件

产品已经明确要求：

```text
一个部署账号
一个发布器
正确的 Remote Root 权限
```

对于正常部署账号影响较低。

### 建议

不需要立即增加复杂 FTP Capability Negotiation。

出现真实兼容案例后，再增加：

```text
MLST
SIZE
NLST
CWD
```

按服务器能力排序的 Probe Strategy。

---

## P2-04：v1.2.2 Commit 的 CI 可验证性不足

Release Notes 声明执行了：

- Python 3.11/3.12；
- Ruff；
- ty；
- Wheel/sdist；
- Isolated Install。

但该版本是直接 Main Commit，Connector 没有返回对应 PR Workflow Run 或 Combined Status。

### 建议

后续即使是个人项目，也保持：

```text
Branch
    ↓
PR
    ↓
CI
    ↓
Merge
    ↓
Tag
```

或者至少等待 Main Push CI 成功后再创建 Tag。

---

# 7. v1.2.2 最终评价

| 项目 | 结论 |
|---|---|
| 产品复杂度 | 通过 |
| 单命令日常部署 | 通过 |
| FTP Upload | 通过 |
| FTP Bulk Delete | 通过 |
| FTP Missing Parent | 通过 |
| FTP Empty Parent | 通过 |
| FTP Permission Fail Closed | 通过 |
| Workspace Build-only | 通过 |
| Native OpenSSH | 通过 |
| WSL + 1Password | 通过 |
| Thin Workspace | 通过 |
| State Correctness | 通过 |
| CI 独立证据 | 本轮不可确认 |
| 跨设备并发互斥 | 明确非目标 |

结论：

> **v1.2.2 通过代码审计，可以作为稳定日常使用版本。**

不建议继续围绕 FTP 和 Workspace 增加更多抽象。

---

# 8. SFTP 远程命令需求判断

## 8.1 这个需求是合理的

典型部署流程不仅是上传文件，还可能需要：

```text
重启或 Reload 服务
清理应用缓存
执行框架缓存命令
更新文件权限
检查服务状态
Reload Nginx / PHP-FPM
Restart Queue Worker
```

当前 README 明确把：

```text
缓存刷新
进程重启
```

排除在工具职责之外。

如果真实日常部署每次都需要额外执行：

```bash
ssh project-prod
sudo systemctl restart ...
```

那么“部署只需要一条命令”的北极星实际上还没有完整达到。

因此支持一个受控的部署后命令列表是合理的。

---

## 8.2 技术名称应是 SSH Command，不是 SFTP Command

SFTP 只负责文件操作。

远程命令实际通过：

```text
SSH Exec Channel
```

实现。

从用户配置视角，它可以属于：

```toml
protocol = "sftp"
```

但内部能力应命名为：

```text
Remote SSH Command
After-deploy Command
Post-sync Command
```

避免将 SFTP 和 Shell Execution 混为一谈。

---

# 9. 推荐产品范围

## 9.1 只增加 after_deploy

第一版只支持：

```text
文件同步成功
    ↓
after_deploy commands
    ↓
State Commit
```

不要同时增加：

- before_deploy；
- on_failure；
- rollback_commands；
- named remote tasks；
- remote shell；
- interactive console；
- workspace global hooks；
- lifecycle DSL。

这些能力会重新把工具变成发布流水线平台。

---

## 9.2 推荐配置

```toml
[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/www/wwwroot/application"

after_deploy = [
  "sudo -n systemctl restart application.service",
  "sudo -n systemctl is-active --quiet application.service"
]

command_timeout = 120
```

PHP 项目示例：

```toml
after_deploy = [
  "php think clear",
  "sudo -n systemctl reload php8.3-fpm"
]
```

Laravel 示例：

```toml
after_deploy = [
  "php artisan optimize:clear",
  "php artisan config:cache",
  "php artisan queue:restart"
]
```

---

# 10. 精确执行语义

## 10.1 执行顺序

```text
Build
    ↓
Plan
    ↓
Freeze
    ↓
Confirm
    ↓
Connect
    ↓
Upload/Delete
    ↓
after_deploy[0]
    ↓
after_deploy[1]
    ↓
State Save
```

## 10.2 Command Failure

任一命令返回非零：

```text
Deployment Failed
State Unchanged
后续命令不执行
```

远端文件可能已经全部更新，但旧 State 会使下次部署重新执行同一批文件操作和命令。

这符合当前：

```text
失败重跑自然收敛
```

模型。

## 10.3 No-op

没有 Upload/Delete：

```text
不执行 after_deploy
只推进必要的 Commit State
```

避免每次无变化都重启线上服务。

## 10.4 Retry

文件传输：

```text
可以自动 Retry
```

远程命令：

```text
不自动 Retry
```

因为任意命令未必幂等。

例如：

```text
数据库 Migration
发送消息
扣减计数
创建一次性资源
```

自动重复可能造成更严重后果。

用户重新执行整个部署时，命令会再次执行，因此文档应要求：

> `after_deploy` 命令应尽量幂等。

---

# 11. Native OpenSSH 实现

## 11.1 复用现有 Master

当前已经有：

```text
OpenSSHMaster
SSHConnectionPool
ControlPath
Pinned Endpoint
Alias Drift Check
```

可以增加：

```python
OpenSSHMaster.run_command(command, cwd, timeout)
```

执行：

```text
ssh
-F <config>
-o ControlPath=<socket>
-o HostName=<frozen host>
-o User=<frozen user>
-o Port=<frozen port>
-T
alias
<remote command>
```

这样：

- 不建立第二条 SSH 认证；
- 不重复触发 Windows Hello；
- 继续使用 1Password Agent；
- 继续使用 ProxyJump；
- 继续固定审阅的 Endpoint。

## 11.2 Working Directory

默认在：

```text
target.remote_root
```

执行命令。

逻辑：

```text
cd -- <quoted remote_root> && <configured command>
```

用户不需要在每条命令中重复写项目目录。

## 11.3 TTY

默认：

```text
no PTY
no stdin
```

不要允许：

```text
交互密码
交互 sudo
交互确认
```

推荐命令使用：

```bash
sudo -n ...
```

---

# 12. Paramiko SFTP 实现

Direct Host SFTP 当前使用 Paramiko SSHClient。

可以通过同一 Client：

```python
stdin, stdout, stderr = client.exec_command(
    wrapped_command,
    timeout=command_timeout,
    get_pty=False,
)
```

要求：

- 不开启 PTY；
- 关闭 stdin；
- 输出 stdout/stderr；
- 读取 Exit Status；
- 非零返回 DeployError。

FTP Target 配置 `after_deploy` 时应在 Config Load 阶段直接拒绝。

---

# 13. 最小接口设计

## 13.1 Config

TargetConfig 增加：

```python
after_deploy: tuple[str, ...] = ()
command_timeout: float | None = 120.0
```

校验：

- 只允许 SFTP；
- 每条命令必须非空；
- 拒绝 NUL 和换行控制字符；
- 数量可限制，例如最多 16 条；
- 单条长度可限制，例如 4096 字符。

## 13.2 Transport

避免重新设计大型 Command Framework。

可在 `Transport` 增加默认方法：

```python
def run_command(
    self,
    command: str,
    *,
    cwd: PurePosixPath,
    timeout: float | None,
) -> None:
    raise DeployError("remote commands are not supported")
```

实现：

- OpenSSHSFTPTransport；
- Paramiko SFTPTransport。

FTP 使用默认 Unsupported。

## 13.3 Deployer

在：

```python
state_store.save(...)
```

之前：

```python
for command in plan.target.after_deploy:
    transport.run_command(...)
```

注意当前 Deployer 在文件循环结束后立即 `transport.close()`。

需要调整为：

```text
Connect
File Operations
Commands
Close
State Save
```

而不是在命令前关闭 Transport。

---

# 14. Plan 与确认

Single Project Plan 应显示：

```text
AFTER  sudo -n systemctl restart application.service
AFTER  sudo -n systemctl is-active --quiet application.service
```

Workspace Combined Plan：

```text
[api]
  Target: ...
  UPLOAD ...
  AFTER sudo -n systemctl restart api.service
```

用户确认的必须是：

```text
文件变更
+
远程命令
```

不能在确认界面隐藏命令。

---

# 15. 命令输出

执行前：

```text
REMOTE [1/2] sudo -n systemctl restart application.service
```

stdout/stderr 应实时输出到当前终端。

成功：

```text
REMOTE OK [1/2]
```

失败：

```text
REMOTE FAILED [1/2] exit=1
```

不要将命令输出写入 State。

Verbose 模式可以显示：

- Endpoint；
- CWD；
- Timeout；
- Exit Code。

不要显示：

- SSH_AUTH_SOCK；
- 密钥；
- 密码；
- 1Password Item；
- 环境变量 Secret。

---

# 16. Sudo 安全建议

1Password 只负责 SSH 身份认证，不负责远端 sudo 密码。

建议服务器配置最小权限的 `NOPASSWD` Allowlist：

```sudoers
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl restart application.service
deploy ALL=(root) NOPASSWD: /usr/bin/systemctl is-active application.service
```

配置命令：

```toml
after_deploy = [
  "sudo -n /usr/bin/systemctl restart application.service",
  "sudo -n /usr/bin/systemctl is-active --quiet application.service"
]
```

不要配置：

```sudoers
deploy ALL=(ALL) NOPASSWD: ALL
```

---

# 17. Workspace 语义

after_deploy 属于每个 Repository Target。

顺序：

```text
Repository A Files
Repository A Commands
Repository A State

Repository B Files
Repository B Commands
Repository B State
```

如果 B Command 失败：

```text
A 已成功并提交 State
B State 不更新
C 不执行
```

重新运行：

```text
A No-op
B 重新 Sync + Command
C 随后执行
```

第一版不要增加 Workspace Global Command。

如果多个仓库最终需要只重启一次同一个服务，应先通过真实案例验证，再决定是否增加：

```text
workspace after_deploy
```

不要提前设计。

---

# 18. 不建议支持的能力

第一版明确不做：

- 任意 `git-deploy ssh`；
- 交互 Shell；
- Command Name Registry；
- Pre-deploy Commands；
- On-failure Commands；
- 自动 Command Retry；
- 自动 Rollback；
- 数据库 Migration 识别；
- Service Manager 抽象；
- Systemd 专用配置；
- Docker/Kubernetes Commands；
- Workspace Global Hooks；
- Secret Interpolation；
- 文件名/Commit Message 模板插值。

---

# 19. 推荐版本规划

远程命令属于新能力，而不是 v1.2.x Bugfix。

建议：

```text
v1.2.2
稳定文件部署基线

v1.3.0-alpha.1
Native OpenSSH after_deploy

v1.3.0-beta.1
Paramiko SFTP after_deploy
Workspace/Failure Tests

v1.3.0
稳定发布
```

也可以在 Alpha 阶段只支持 Native OpenSSH，因为这是用户当前真实使用路径。

如果验证稳定，再补 Paramiko。

---

# 20. 原子 TODO

> 实施状态（2026-07-17）：以下项目已由 v1.3.0 实现并通过 Fake Transport、
> 本机 FTP 与隔离容器 OpenSSH 合约测试；真实 WSL + 1Password 验收仍按第 21 节作为可选人工增强。

## Config

- [x] TargetConfig 增加 after_deploy
- [x] TargetConfig 增加 command_timeout
- [x] FTP 配置命令时 Fail Closed
- [x] 空命令拒绝
- [x] 控制字符拒绝
- [x] 数量与长度限制
- [x] Config Tests

## Plan

- [x] Plan 冻结 after_deploy
- [x] Single Plan 显示命令
- [x] Workspace Plan 显示命令
- [x] Confirmation 包含命令
- [x] Dry-run 零 Remote Execution

## Native OpenSSH

- [x] OpenSSHMaster.run_command
- [x] 复用 ControlPath
- [x] Pinned Endpoint
- [x] Alias Drift 保持
- [x] No PTY
- [x] No Stdin
- [x] Remote Root CWD
- [x] Command Timeout
- [x] Streaming Output
- [x] Exit Code

## Paramiko

- [x] SSHClient.exec_command
- [x] No PTY
- [x] Close Stdin
- [x] Stream stdout/stderr
- [x] Exit Status
- [x] Timeout
- [x] Cleanup

## Deployer

- [x] File Ops 后执行 Commands
- [x] Commands 后 State Save
- [x] Command Failure State Unchanged
- [x] Command Failure Stop Remaining Commands
- [x] No-op 不执行 Commands
- [x] Command 不自动 Retry
- [x] Transport 在 Commands 后 Close

## Workspace

- [x] A Command Success → A State Save
- [x] B Command Failure → B State Old
- [x] C Not Executed
- [x] Rerun Convergence
- [x] Shared Master 无额外认证
- [x] Combined Plan Commands

## Security

- [x] sudo -n 文档
- [x] NOPASSWD Allowlist 文档
- [x] 禁止 Agent Forwarding
- [x] Config Trusted Boundary
- [x] 不输出 Secrets

---

# 21. 验收流程

## Native OpenSSH + 1Password

配置：

```toml
after_deploy = [
  "printf deploy-ok",
  "sudo -n systemctl restart application.service",
  "sudo -n systemctl is-active --quiet application.service"
]
```

执行：

```bash
git-deploy prod
```

确认：

- 只触发一次 Windows Hello；
- 文件上传与命令共享 Master；
- Command 输出可见；
- Service Restart 成功；
- State 最后提交。

## Command Failure

将第二条命令改成：

```bash
false
```

确认：

- 文件完成上传；
- Command 返回非零；
- State 保持旧值；
- 第三条命令不执行；
- 再次部署会重复 Sync 和 Commands。

## No-op

重复执行：

```bash
git-deploy prod
```

确认：

- No Changes；
- 不连接远端；
- 不触发 Windows Hello；
- 不执行 Restart。

## Workspace

A、B、C 三仓：

- A Command 成功；
- B Command 失败；
- C 未执行。

确认重新执行：

- A No-op；
- B 重试；
- B 成功后 C 执行。

---

# 22. 最终结论

## v1.2.2

> **通过。**

上一轮 FTP 和 Workspace Build 问题已正确修复，没有必要继续修改 v1.2.x 架构。

## 远程命令

> **值得支持，但必须实现为受控的 SFTP Target `after_deploy`，不能发展成通用远程运维框架。**

最合理的产品结果是：

```text
git-deploy prod
    ↓
Build
    ↓
Sync Files
    ↓
Restart / Reload / Verify
    ↓
Commit State
```

这能真正实现：

> 一次配置，一条命令完成日常构建与部署。

同时仍保持 v1-lite 的核心边界：

```text
没有 Pipeline DSL
没有自动 Rollback
没有通用 SSH Shell
没有复杂 Hooks
失败后重新执行
```
