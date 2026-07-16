# git-deploy v1.2.0 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`6add56d66916af5658836f175c759b9c73bba15e`
> 发布提交：`d7f71adcffb58f23a160c6e72e2964c5ce24db68`
> 版本：`1.2.0`
> 审计日期：`2026-07-16`
> 结论：**不通过稳定版审计，建议发布 v1.2.1 Hotfix**

---

## 1. 执行摘要

v1.2.0 的产品和架构方向基本正确。

Thin Workspace 没有重新引入 v0.3 的平台化状态模型，而是保持：

```text
每仓独立 deploy.toml
每仓独立 Build
每仓独立 Git 历史
每仓独立 State
每仓独立 Target Lock
        ↓
Workspace 只负责顺序、统一 Target、Prepare All、一次确认和顺序部署
```

本轮确认已经正确落地的能力：

- 自动识别单仓或 Workspace；
- 配置歧义时要求显式选择；
- Workspace 只保存仓库名称、路径和顺序；
- 所有仓库先完成 Build、Plan 和上传字节冻结；
- Prepare 失败时零远端连接；
- Combined Plan；
- 一次确认；
- 顺序部署；
- 每仓成功后独立提交 State；
- 中途失败后重跑自然收敛；
- Native OpenSSH Connection Pool；
- Git Common Dir State；
- Per-repository Target Lock；
- Source/Output 完整所有权冲突；
- Output 根目录缺失 Fail Closed；
- Git executable mode；
- FTP executable mode Fail Closed。

PR #8 声明：

```text
Python 3.11：97 passed
Python 3.12：97 passed
Ruff：通过
ty：通过
uv lock --check：通过
wheel/sdist：构建通过
隔离安装与 Smoke Test：通过
```

GitHub Actions 的 PR Head 运行也显示 Python 3.11 和 3.12 的全部步骤成功。

但本轮静态审计确认存在三个发布阻断问题：

1. Native OpenSSH 在用户确认后仍通过可变 Alias 连接，可能把文件部署到未审阅的新主机；
2. Native SFTP 将任意路径探测失败视为“文件不存在”，可能跳过删除后仍提交成功 State；
3. Workspace 不检查不同仓库的物理远端根目录是否相同或嵌套，两个仓库可能相互覆盖和删除文件。

另外，Workspace 的共享 ControlMaster 在连接失效后无法真正重建，默认 15 秒 Timeout 也被错误用于完整文件传输。

因此：

> v1.2.0 的 Thin Workspace 架构可以保留，但不应直接视为稳定完成。应先发布 v1.2.1 修复连接边界、删除语义和跨仓远端所有权。

---

# 2. 审计范围

本轮重点检查：

- v1.2.0 相对 v1.1.0 的增量；
- Workspace 配置；
- Workspace 自动发现；
- Prepare / Freeze / Execute 两阶段；
- 多仓 Target 校验；
- Target Lock；
- Git Common Dir State；
- Combined Plan；
- Confirm Once；
- 顺序部署；
- 独立 State Commit；
- Partial Failure / Rerun；
- Native OpenSSH Connection Pool；
- OpenSSH Alias；
- ControlMaster；
- SFTP Batch；
- FTP / Paramiko SFTP；
- CI、测试和发布版本一致性。

主要文件：

```text
src/git_deploy/cli.py
src/git_deploy/workspace.py
src/git_deploy/prepared.py
src/git_deploy/deployer.py
src/git_deploy/config.py
src/git_deploy/planner.py
src/git_deploy/manifest.py
src/git_deploy/lock.py
src/git_deploy/transports/openssh_sftp.py
src/git_deploy/transports/sftp.py
src/git_deploy/transports/ftp.py
tests/test_workspace.py
tests/test_openssh_sftp.py
```

---

# 3. 审计方式与限制

本轮通过 GitHub Connector 读取：

- 最新 Commit；
- PR #8；
- PR #7；
- 最新 Main 源码；
- v1.2.0 Tag；
- 测试源码；
- GitHub Actions Workflow Run 和 Job Steps；
- Pull Request Review Threads。

本地尝试：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

执行环境仍无法解析 `github.com`，因此无法独立运行：

```bash
uv lock --check
uv run pytest -q
uv run ruff check src tests
uv run ty check src
uv build --clear
```

因此测试结论分成：

```text
GitHub CI 证明
静态代码审计
```

本报告不会把 PR 描述中的测试声明冒充为本地独立复跑结果。

---

# 4. 版本与 CI 一致性

## 4.1 Main 和版本

当前 Main：

```text
6add56d66916af5658836f175c759b9c73bba15e
Merge pull request #8
release v1.2.0: add thin workspace
```

`pyproject.toml`：

```toml
version = "1.2.0"
```

`src/git_deploy/__init__.py` 同步为 `1.2.0`。

## 4.2 Tag 与 Main 源码

`v1.2.0` Tag 中的：

```text
src/git_deploy/workspace.py
```

Blob SHA 与当前 Main 一致：

```text
47453fd8cd9dada48fc2a23e8518876bac7fa00a
```

未发现 Tag 源码落后于 Main 的问题。

## 4.3 CI

PR Head `d7f71adc...` 对应 CI Run：

```text
status     completed
conclusion success
```

Python 3.11 和 3.12 Job 均通过：

- Interpreter Matrix；
- Lock Check；
- Dependency Install；
- Pytest；
- Ruff；
- ty；
- Build；
- Wheel Isolated Install；
- Version / Help Smoke。

Main Merge Commit 没有被 Connector 返回 PR-triggered Workflow Run，这属于 Connector 查询范围限制；可用证明来自合并前的 PR Head。

---

# 5. 已正确实现的架构边界

## 5.1 Thin Workspace 保持足够薄

Workspace Config 只允许：

```toml
default_target = "dev"

[[repositories]]
name = "api"
path = "api"
```

它没有重新加入：

- Shared Build；
- Shared State；
- Target Map；
- Dependency Graph；
- Parallel Jobs；
- Global Transaction；
- Rollback；
- Configuration Inheritance。

这一点符合产品边界。

## 5.2 每仓保持独立

每个 Repository 仍独立拥有：

- `deploy.toml`；
- Build；
- Git HEAD；
- Output Manifest；
- Target State；
- Target Lock；
- Frozen Upload Bytes。

Workspace 不合并 Git 历史或 State。

## 5.3 Prepare All Before Connect

`prepare_workspace()`：

1. 检查所有仓库是否存在统一 Target；
2. 逐仓调用 `prepare_project()`；
3. 任何 Prepare 失败时关闭此前 PreparedDeployment；
4. Prepare 阶段不建立远端连接。

`prepare_project()`：

```text
Load Config
Validate Git
Acquire Lock
Migrate State
Dirty Check
Load State
Resolve Target
Build
Post-Build Dirty Check
Create Plan
Freeze Upload Bytes
```

这是合理的两阶段模型。

## 5.4 字节冻结边界正确

源码：

```text
从计划 HEAD 使用 git cat-file 导出
```

产物：

```text
复制到 Temporary Directory
重新计算 SHA256
与 Plan Manifest 比较
```

用户确认后工作区或 Output 再发生变化，不会替换已审阅的上传字节。

## 5.5 每仓 State 独立提交

Workspace 顺序执行每一个 PreparedDeployment。

只有某仓自己的远端操作全部成功后，才保存该仓 State。

因此：

```text
A 成功
B 失败
C 未执行
```

重跑后：

```text
A No-op
B 继续
C 随后执行
```

不需要 Global State 或 Recover。

## 5.6 v1.0.1 审计问题已修复

已确认：

- 配置 Output 根目录缺失时直接失败；
- Source 与 Output 完整所有权冲突；
- 相同或嵌套 Output Mapping 拒绝；
- Git `100755` 进入 UploadOperation；
- SFTP 上传后 chmod；
- FTP 遇到 executable source 在连接前拒绝。

这些修复方向正确。

---

# 6. P0 发布阻断问题

## P0-01：OpenSSH Alias 在确认后仍可漂移到另一台主机

### 代码路径

Prepare 阶段：

```text
prepare_project()
    ↓
resolve_target_for_plan()
    ↓
ssh -G alias
    ↓
冻结 host / username / port
    ↓
生成 target fingerprint
```

`resolve_target_for_plan()` 保留：

```python
ssh_host_alias
```

并将：

```python
ssh_resolved = True
host = resolved.host
username = resolved.username
port = resolved.port
```

写入计划 Target。

但是 OpenSSH Master 真正连接时使用：

```text
ssh ... -MNf <alias>
```

连接命令没有传入冻结的：

```text
HostName
User
Port
```

也没有在连接前重新运行 `ssh -G` 并比较。

### 风险窗口

Workspace Prepare 可能持续较长时间：

```text
Resolve Alias
Build API
Build Web
Build Admin
Freeze Bytes
显示 Combined Plan
等待用户确认
Connect
```

在此期间：

- 用户编辑 `~/.ssh/config`；
- 配置同步工具重写 SSH Config；
- VPN/环境脚本切换 Alias；
- Alias 的 HostName、User、Port 或 Proxy 被修改。

### 结果

用户审阅的是：

```text
deploy@host-A:22
```

实际执行：

```text
ssh project-prod
```

可能连接：

```text
deploy@host-B:22
```

部署成功后 State 仍记录：

```text
host-A fingerprint
```

这是最严重的错误类型：

> 文件可能被发送到用户没有确认的另一台服务器。

下一次运行虽然可能检测到 Fingerprint 变化，但错误部署已经发生。

### 修复要求

连接必须同时保留 Alias 的配置上下文，并固定审阅后的 Endpoint。

建议 OpenSSH 命令增加：

```text
-o HostName=<frozen-host>
-o User=<frozen-user>
-p <frozen-port>
```

仍然保留：

```text
alias
-F ssh_config_file
```

这样：

- Alias 继续提供 IdentityFile、ProxyJump、ProxyCommand、Match；
- Host/User/Port 使用审阅冻结值。

同时在连接前增加：

```text
ssh -G alias
```

重新解析并比较：

```text
host
username
port
```

发生变化时：

```text
stale target: SSH alias changed after plan; re-run required
```

建议同时做到：

```text
Recheck + Pin
```

而不是只做其中一个。

### 必须新增测试

- Prepare 后修改 Alias HostName；
- Prepare 后修改 Alias User；
- Prepare 后修改 Alias Port；
- 确认后连接前修改 Alias；
- 连接命令包含冻结 HostName/User/Port；
- Alias 漂移时零 Remote Mutation；
- State 不提交。

---

## P0-02：Native SFTP 将所有探测失败视为“不存在”

### 当前实现

Native SFTP 删除：

```python
if not self._exists(target):
    return

run_batch(("rm target",))
```

`_exists()`：

```python
result = sftp_batch(("ls path",), check=False)
return result.returncode == 0
```

也就是说：

```text
returncode == 0
    → exists

任何非 0
    → missing
```

### 非 0 不只代表 Missing

还可能是：

- Permission Denied；
- ControlMaster 已断开；
- Network Failure；
- SFTP Server Error；
- Authentication Session Expired；
- Timeout；
- Path Parent 无权限；
- SSH Proxy 故障；
- 临时服务器错误。

### 灾难性后果

计划要求删除：

```text
public/dist/old.js
```

执行时：

```text
ls old.js
    ↓
Permission denied
    ↓
_exists() == False
    ↓
delete() 直接返回成功
    ↓
State 保存
```

本地 State 已推进，远端旧文件仍存在。

下一次部署：

- Source Last Commit 已推进；
- Output Manifest 已移除该文件；
- 工具不会再次计划删除。

这不是普通重试问题，而是：

> 工具确认删除成功并永久丢失删除意图。

### 修复要求

远端路径探测必须返回三态：

```python
EXISTS
MISSING
ERROR
```

只有明确的：

```text
No such file
Couldn't stat remote file: No such file or directory
```

才能归类为 `MISSING`。

所有其他非 0：

```text
raise DeployError
```

建议：

- 给 SFTP Process 强制 `LC_ALL=C`；
- 精确匹配 OpenSSH Missing 文本；
- 不匹配则错误；
- 不要将全部 Return Code 1 解释成 Missing。

删除流程：

```text
EXISTS
    → rm
MISSING
    → idempotent success
ERROR
    → fail, state unchanged
```

### 相关影响

相同 `_exists()` 还用于：

- Root Exists；
- Directory Create；
- Publish Fallback。

这些位置虽然多数最终会因为后续 mkdir/rename 失败而停止，但错误报告会不准确。

### 必须新增测试

- Missing File；
- Permission Denied；
- Dead ControlMaster；
- Network Failure；
- Timeout；
- Parent Permission Denied；
- Delete Probe Error 后 State 不提交；
- Rerun 仍保留 Delete Operation。

---

## P0-03：Workspace 不拒绝相同或嵌套的物理远端根目录

### 当前校验

Workspace Config 拒绝：

- 重复 Repository Name；
- 重复 Local Repository Path；
- Workspace 外路径；
- 缺失 `deploy.toml`。

但没有比较每个仓库 Target 的：

```text
protocol
host
username
port
remote_root
```

### 风险场景一：相同 Root

```text
api:
  project-prod → /srv/application

web:
  project-prod → /srv/application
```

两个仓库各自认为自己独立拥有：

```text
/srv/application/app.py
/srv/application/public/*
```

可能：

- 后部署仓库覆盖前一个仓库；
- 某仓 Delete 删除另一个仓文件；
- 两份独立 State 都显示成功。

### 风险场景二：嵌套 Root

```text
api:
  /srv/application

web:
  /srv/application/public
```

API 的 Source 或 Output 可能覆盖：

```text
public/*
```

Web 又认为该目录由自己独立拥有。

### 为什么现有 Lock 无法保护

Target Lock 位于：

```text
<repository-common-dir>/git-deploy/<target>.lock
```

两个不同仓库即使指向同一台服务器同一目录，也会使用两个完全独立的 Lock。

Workspace 本身顺序执行只防止同一命令并发，不防止：

- 两仓顺序互相覆盖；
- 两个 Workspace Process；
- 单仓命令和 Workspace 同时执行；
- 两个不同 Workspace 管理同一 Root。

### 修复要求

Workspace 在任何 Build 前完整解析所有 Target。

按 Physical Endpoint 分组：

```text
protocol
effective host
effective username
effective port
```

同组内比较 `remote_root`：

```text
root A == root B
root A is parent of root B
root B is parent of root A
```

任何相同或嵌套必须 Fail Closed。

示例错误：

```text
workspace repositories 'api' and 'web' manage overlapping remote roots:
deploy@host:22:/srv/application
deploy@host:22:/srv/application/public
```

v1.2.1 不建议增加 `allow_shared_root`。

保持原则：

> 一个物理远端路径只能由一个 Repository State 所有。

### 可选增强

在本机增加物理 Target Lock：

```text
~/.local/state/git-deploy/targets/<fingerprint>.lock
```

用于防止不同仓库/Workspace Process 同时管理完全相同的物理目标。

但第一优先级仍是 Workspace Prepare 时拒绝重叠 Root。

### 必须新增测试

- 同 Endpoint、相同 Root；
- 同 Endpoint、父子 Root；
- 相同 Alias、不同 Root Sibling；
- 不同 Endpoint、相同 Root 文本；
- FTP 相同 Root；
- Alias 解析后相同 Endpoint；
- 冲突在第一个 Build 前失败；
- 冲突时零 Lock Leak；
- 冲突时零 Remote Connect。

---

# 7. P1 高优先级问题

## P1-01：失效的 Pooled ControlMaster 无法在重试时被替换

### 当前重试流程

单文件失败后：

```python
transport.close()
transport.connect()
```

对于普通独占 OpenSSH Transport：

```text
close()
    → master.close()
```

可以重建连接。

对于 Workspace Pool：

```text
OpenSSHSFTPTransport.close()
    → self.master = None
    → 不关闭 pooled master
```

重新连接：

```text
pool.acquire(target)
```

Pool 只检查 Key：

```python
existing = masters.get(key)
if existing is not None:
    return existing
```

没有：

```text
ssh -O check
invalidate
evict
force reconnect
```

### 后果

如果失败原因是：

- ControlMaster 断开；
- SSH Session 失效；
- VPN 中断；
- Proxy 连接断开；
- 服务器重启；

所有配置的：

```toml
retries = 3
```

都会复用同一个死 Master。

表现为：

```text
Retry 1 → dead master
Retry 2 → same dead master
Retry 3 → same dead master
```

只有重新启动整个 git-deploy 命令才会建立新 Master。

### 修复要求

`SSHConnectionPool` 增加：

```python
invalidate(master_or_key)
```

行为：

```text
pop master
close master
```

`acquire()` 返回已有 Master 前先执行：

```text
ssh -O check
```

检查失败：

```text
evict
create new master
```

Operation Retry 捕获连接类错误时：

```text
transport.invalidate_connection()
transport.connect()
```

不能只调用普通 `close()`。

### 必须新增测试

- 第一次 Batch 模拟 Dead Master；
- Pool 中旧 Master 被移除；
- Retry 建立第二个 `-MNf`；
- 第二次成功；
- Pool 最终只保留健康 Master；
- `close_all()` 正确关闭。

---

## P1-02：Target Timeout 被错误用于完整 SFTP 文件传输

### 当前行为

OpenSSH Master 建立：

```python
subprocess.run(... timeout=max(target.timeout, 1))
```

每个 SFTP Batch：

```python
subprocess.run(... timeout=max(target.timeout, 1))
```

默认 Target Timeout：

```text
15 秒
```

这意味着一个大文件上传如果超过 15 秒：

```text
Python 杀死 sftp process
    ↓
Operation Retry
    ↓
再次从头上传
```

常见影响：

- 大型 `vendor/` 文件；
- Source Map；
- 压缩包；
- 慢速跨境服务器；
- FTP/SFTP 带宽较低；
- VPN/ProxyJump；
- 多仓批量部署。

### 认证体验影响

用户使用 1Password / Windows Hello 时，如果确认超过 15 秒，Master 建立也可能被 Python Timeout 终止。

### 根本问题

同一个 `timeout` 被同时用于：

```text
Network Connect
Interactive Authentication
Whole File Transfer
Remote Batch Operation
```

这些不是同一类 Timeout。

### 修复建议

最简 v1.2.1：

- `target.timeout` 只作为连接超时；
- 传给 OpenSSH：

```text
-o ConnectTimeout=<seconds>
```

- `run_batch()` 不使用 Python Whole-Process Timeout；
- 或增加独立：

```toml
operation_timeout = null
```

默认无限制。

认证等待应允许 1Password 生物授权，不应被 15 秒硬杀。

### Pool 衍生问题

Connection Pool Key 不包含 Timeout。

Master 保存的是第一个 Repository 的 Target，因此相同 Endpoint 的后续仓库可能继承第一个仓库的 Timeout。

修复 Timeout 模型后应让：

```text
Connection Policy
Operation Policy
```

不依赖第一个仓库对象。

### 必须新增测试

- 模拟超过 15 秒的大文件；
- 不触发 Whole Batch Timeout；
- Connect Timeout 仍生效；
- 生物认证等待不被短 Timeout 杀死；
- 两个 Repository 不同 Timeout 共享 Endpoint。

---

## P1-03：Combined Plan 没有展示每仓物理目标

当前 Combined Plan 显示：

```text
Workspace Target: prod

[api]
  UPLOAD app.py

[web]
  UPLOAD dist/app.js
```

没有显示：

- Backend；
- SSH Alias；
- Resolved Host；
- User；
- Port；
- Remote Root；
- Previous Commit；
- Planned HEAD；
- Full / Incremental。

但一次确认会批准所有仓库。

### 风险

如果某个仓库的：

```toml
[targets.prod]
```

误指向测试机、旧服务器或错误目录，用户在 Combined Plan 中看不出来。

### 修复要求

每仓至少显示：

```text
[api]
  Target: project-prod → deploy@192.0.2.10:22:/srv/api
  Mode: INCREMENTAL
  Commit: old -> new
  2 uploads, 0 deletes
```

Native OpenSSH 可以显示：

```text
Alias + Frozen Endpoint
```

不要只显示 Alias，也不要只显示 Target Name。

### 必须新增测试

- Combined Plan 显示所有 Repository Endpoint；
- 显示 Remote Root；
- 显示 Commit Boundary；
- 显示 Full / Incremental；
- 不泄露密码或 Agent 信息。

---

## P1-04：Doctor 在 Target 解析失败后仍继续远端流程

Doctor：

```python
try:
    resolved_target = resolve_target_for_plan(...)
except:
    append target config failure

...
transport_factory(resolved_target)
transport.connect()
```

解析失败后 `resolved_target` 仍为原始 Target。

如果用户传：

```bash
git-deploy doctor prod --create-root
```

Doctor 可能在已经报告 Target Config Failure 后继续：

- 创建 Transport；
- 连接远端；
- 尝试创建 Root。

### 修复要求

Target Resolve 失败后：

```text
记录失败
跳过所有 Remote Checks
return local results
```

Doctor 不应在配置预检失败后继续远端操作。

Workspace Doctor 也应先解析全部 Repository Target，再连接任何 Endpoint，特别是 `--create-root` 模式。

---

## P1-05：不同仓库的 Per-Repository Lock 不等于物理目标锁

P0-03 修复 Workspace 内冲突后，仍存在不同命令之间的问题：

```text
Terminal A:
  cd api
  git-deploy prod

Terminal B:
  cd web
  git-deploy prod
```

若两个仓库错误地指向相同远端 Root：

- 两个 Repository Lock 都能成功；
- 远端操作可能交错；
- State 独立提交。

建议中期增加物理目标 Lock：

```text
XDG_STATE_HOME/git-deploy/targets/<target-fingerprint>.lock
```

保持实现简单：

- 本机 advisory lock；
- 不做远端分布式锁；
- 不做服务器 Lock File；
- 不做全局事务。

---

# 8. P2 改进项

## P2-01：Workspace 第一轮 Target 校验没有真正 Resolve Endpoint

`prepare_workspace()` 第一轮只执行：

```python
load_config(...).target(target)
```

它只确认 Target 名存在。

真正的：

```text
ssh -G
POSIX ssh/sftp discovery
target fingerprint
```

发生在逐仓 Prepare 中。

因此后面仓库 Alias 无效时，前面仓库可能已经完成昂贵 Build。

修复跨仓 Root 冲突时，应建立独立 Preflight：

```text
Load All Config
Resolve All Targets
Validate All Endpoint/Root
Validate All Lock Availability
Then Build
```

## P2-02：所有仓库冻结文件同时占用临时磁盘

Workspace 会在部署开始前冻结所有仓库全部 Upload Bytes。

如果多个仓库都有：

- `vendor/`；
- `dist/`；
- 大型 Build Artifact；

临时磁盘占用可能接近全部上传内容总和。

这是 Prepare All 安全模型的自然成本，不建议立刻改回边构建边部署。

可以增加：

- Combined Frozen Bytes 统计；
- 临时目录可配置；
- Prepare 前可用磁盘检查；
- 明确错误信息。

不要引入复杂 CAS。

## P2-03：Native OpenSSH 进度只在上传结束后回调

Native Backend 的 `sftp` 子进程使用 Quiet Batch。

上传完成后才：

```python
callback(size, size)
```

因此大文件上传期间没有实时进度。

可在后续版本使用：

- OpenSSH SFTP Progress Parsing；
- 或保留 Spinner / Current File；
- 不要为此改回 Python 读取私钥。

## P2-04：Workspace Repository Name 校验过宽

Repository Name 只要求非空。

它被用于：

- 日志；
- Temporary Directory Prefix；
- Combined Plan；
- 错误消息。

建议限制：

```text
[A-Za-z0-9._-]+
```

并限制长度，拒绝控制字符和换行。

## P2-05：CLI 保留字可以与 Target Name 冲突

平铺 CLI 将：

```text
build
doctor
init
```

解释为 Action。

如果 Target 名也叫：

```text
build
```

就无法正常部署。

建议配置层拒绝保留 Target Name：

```text
build
doctor
init
```

或未来使用明确但仍简洁的：

```text
git-deploy deploy build
```

当前产品追求平铺 CLI，拒绝保留名更简单。

## P2-06：`--create-root` 在 Deploy 路径没有显式拒绝

`--create-root` 是 Doctor 参数，但部署路径不会主动拒绝，容易被用户误认为部署行为选项。

应在 Project / Workspace Deploy 参数校验中拒绝。

## P2-07：FTP 删除仍依赖英文错误文本

FTP Missing 判断依赖：

```text
not found
no such file
does not exist
```

不同 FTP Server 的 550 文本可能不同。

后续可通过：

```text
SIZE / MLST
```

先判断存在性。

该问题不是 v1.2 新增，但仍未关闭。

---

# 9. 测试覆盖审计

## 9.1 已覆盖

Workspace Tests 已覆盖：

- 配置顺序；
- 未知字段；
- 单仓/Workspace 歧义；
- 显式 Workspace；
- Combined Confirm Once；
- Doctor 路由；
- 所有 Target 名在 Build 前校验；
- Prepare 失败释放此前 Lock；
- A 成功、B 失败、C 不执行；
- 重跑收敛；
- Frozen Bytes；
- 单一命令级 Pool；
- Pool 最终 Close。

OpenSSH Tests 已覆盖：

- Alias 自动选择 Native；
- Windows `ssh.exe` 拒绝；
- Batch Path Quote；
- Control Socket Length；
- Master 复用；
- 0700 Directory；
- chmod；
- Pool 跨 Remote Root 复用；
- Backup Swap。

## 9.2 缺失的关键回归

必须补：

```text
Alias changes after Prepare
Alias changes while waiting confirmation
Native delete permission denied
Native delete dead master
Native delete transient network failure
Pooled dead master eviction
Pooled retry creates a new master
Slow upload longer than 15 seconds
Workspace same physical remote root
Workspace nested physical remote root
Combined Plan endpoint visibility
Doctor target resolve failure with --create-root
Different repository timeout sharing one pool
```

## 9.3 当前测试为什么没有发现

现有 Pool 测试只验证：

```text
正常连接复用
最终 close_all
```

没有模拟：

```text
已缓存 Master 在第二次使用前死亡
```

Workspace 测试使用：

```text
/srv/api
/srv/web
/srv/admin
```

没有测试相同或嵌套 Root。

Native `_exists()` 测试没有覆盖非 Missing 错误。

---

# 10. 推荐修复版本：v1.2.1

## Patch A：冻结和校验 OpenSSH Endpoint

- 连接命令固定 HostName/User/Port；
- Alias 连接前 Re-resolve；
- 比较 Prepared Endpoint；
- Config 漂移时 Stale Target；
- State 不提交；
- 测试 Build/Confirmation Race。

## Patch B：Native Path Probe 三态化

- `EXISTS`；
- `MISSING`；
- `ERROR`；
- `LC_ALL=C`；
- 仅确认 Missing 时跳过 Delete；
- Permission/Network/Timeout 必须失败；
- State 保持旧值。

## Patch C：Workspace Remote Ownership

- Resolve All Targets Before Build；
- Endpoint Grouping；
- Equal/Nested Root Rejection；
- Combined Plan 显示 Endpoint；
- 对应测试。

## Patch D：Pooled Master Retry

- Pool Health Check；
- Pool Invalidate；
- Retry Evict Dead Master；
- Reconnect；
- 测试第二个 Master 建立。

## Patch E：Timeout Separation

- Connect Timeout；
- Authentication Interaction；
- Operation Timeout；
- Upload 默认不使用 15 秒 Whole Process Timeout；
- Pool 不继承第一仓 Operation Policy。

## Patch F：Doctor Fail Closed

- Resolve 失败后跳过 Remote；
- Workspace Doctor 全部 Resolve 后再 Connect；
- `--create-root` 只在全部 Preflight 成功后执行。

---

# 11. 推荐原子 TODO

## P0

- [x] Native OpenSSH Connect Pin Frozen HostName
- [x] Native OpenSSH Connect Pin Frozen User
- [x] Native OpenSSH Connect Pin Frozen Port
- [x] Alias Recheck Before Connect
- [x] Alias Drift Error
- [x] Native Probe Result Enum
- [x] Missing Classification
- [x] Permission Error Classification
- [x] Dead Master Probe Classification
- [x] Delete State Commit Regression
- [x] Workspace Resolve All Targets
- [x] Workspace Endpoint Grouping
- [x] Same Root Rejection
- [x] Nested Root Rejection
- [x] Zero Build Before Root Conflict
- [x] Zero Remote Connect Before Root Conflict

## P1

- [x] Pool Master Health Check
- [x] Pool Invalidate
- [x] Transport Reset Connection
- [x] Retry Creates New Master
- [x] Separate Connect Timeout
- [x] Remove Whole Upload 15s Timeout
- [x] Authentication Wait Policy
- [x] Combined Plan Endpoint
- [x] Combined Plan Remote Root
- [x] Combined Plan Commit Boundary
- [x] Doctor Resolve Failure Short Circuit
- [x] Workspace Doctor Preflight All
- [x] Workspace Lock Availability Before Build

## P2

- [x] Frozen Bytes Total Summary
- [x] Temporary Disk Availability Check
- [x] Repository Name Validation
- [x] Reserved Target Name Validation
- [x] Reject --create-root Outside Doctor
- [x] Native Upload Progress
- [x] FTP Existence Probe

## 变更记录

- 2026-07-16：依据 P2-01 的完整 Preflight 顺序，补充并完成“全部 Repository Lock 在首个 Build 前可用”的原子任务。

---

# 12. 人工验收流程

## 12.1 单仓 Alias 漂移

1. 配置：

```toml
ssh_host_alias = "project-prod"
```

2. 执行 Dry-run 或进入确认界面；
3. 修改 `~/.ssh/config` 中 Alias HostName；
4. 确认部署；
5. 期望：

```text
stale target
zero remote mutation
state unchanged
```

## 12.2 1Password / Windows Hello

1. WSL：

```bash
ssh project-prod
```

确认会唤起 Windows 1Password。

2. 部署：

```bash
git-deploy prod
```

期望：

- 一次生物认证；
- 多文件复用 Master；
- 大文件不会因 15 秒被杀；
- State 正确提交。

## 12.3 Dead Master Retry

1. Workspace 两仓共享 Alias；
2. 第一仓建立 Master；
3. 人工终止 Master 或断开 VPN；
4. 第二仓上传；
5. 期望：

```text
检测 Master 已死亡
Pool Evict
重新认证/连接
Retry 成功
```

## 12.4 Delete Permission Error

1. 计划删除已拥有文件；
2. 将远端文件设置为无删除权限；
3. 执行部署；
4. 期望：

```text
Deploy Failed
State Unchanged
下一次仍计划 Delete
```

不能显示成功。

## 12.5 Workspace Root Collision

配置：

```text
api   → /srv/app
web   → /srv/app/public
```

执行：

```bash
git-deploy prod --dry-run
```

期望：

```text
在任何 Build 前失败
零 Remote Connect
明确列出冲突仓库和 Root
```

---

# 13. 最终评价

## 产品方向

通过。

Thin Workspace 仍保持：

```text
薄编排
独立仓库
独立 State
失败重跑
无全局事务
```

没有明显重新回到 v0.3 的复杂路线。

## 单仓普通 Paramiko SFTP

基本通过。

## FTP

条件通过，仍保留删除错误文本兼容问题。

## Native OpenSSH 单仓

不通过稳定审计：

- Alias 漂移；
- Delete Probe；
- Whole Batch Timeout。

## Native OpenSSH Workspace

不通过稳定审计：

- 继承单仓问题；
- Dead Pooled Master Retry；
- 跨仓 Remote Root Ownership；
- Combined Plan 目标信息不足。

## 最终结论

> v1.2.0 的架构可以继续保留，不需要再次破坏性重构；但当前实现存在可能连接错服务器、静默跳过删除并提交 State、以及多仓互相覆盖远端目录的阻断问题。建议立即发布 v1.2.1 Hotfix，仅修复正确性和安全边界，不增加新功能。

v1.2.1 完成后，产品可以进入真实多仓日常使用验收。
