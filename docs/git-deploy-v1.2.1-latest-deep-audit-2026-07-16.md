# git-deploy v1.2.1 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`9af7be31cba487ad46d50cb143447df43f415bc1`
> 修复提交：`6da8c1b6a69a53204e3ab3a9fddba50a0d21372e`
> 版本：`1.2.1`
> 审计日期：`2026-07-16`
> 总体结论：**有条件通过**

---

## 1. 执行摘要

v1.2.1 已经正确关闭 v1.2.0 审计报告中的核心阻断问题：

1. OpenSSH Alias 在真实连接前重新解析；
2. 实际 SSH/SFTP 命令固定已审阅的 HostName、User 和 Port；
3. Native SFTP 路径探测改为 `EXISTS / MISSING / ERROR` 三态；
4. 只有明确 Missing 才跳过删除；
5. Pooled ControlMaster 增加健康检查、驱逐和重建；
6. Connect Timeout 与认证等待、文件传输生命周期分离；
7. Workspace 在 Build 和 Lock 前解析全部 Target；
8. 同一 Endpoint 上相同或父子嵌套 Root 被拒绝；
9. Combined Plan 展示 Endpoint、Root、模式、Commit 边界和冻结字节；
10. Doctor 在 Target 解析失败时停止远端流程；
11. Repository Name、Target Name 和 `--create-root` 边界收紧；
12. FTP 删除不再依赖英文 `550` 错误文本直接判断 Missing。

本轮没有发现新的 P0 级问题。

针对用户的主要日常场景：

```text
WSL
  ↓
Native OpenSSH
  ↓
1Password SSH Agent
  ↓
SFTP
  ↓
Thin Workspace
```

当前代码已达到可以进行真实长期使用验收的状态。

但 PR #9 合并后仍有两个未解决 Review Thread：

- FTP 多文件删除会对同一个目录重复执行完整 `NLST`；
- `git-deploy build` 的 Workspace 模式错误依赖完整远端/OpenSSH Preflight。

此外，本轮静态审计补充发现：

- FTP 删除在父目录已经不存在或部分服务器对空目录返回 `550` 时，幂等删除可能失败；
- 不同仓库在不同 Workspace/单仓命令中指向同一个物理 Root 时，仍没有跨仓物理目标锁；
- Native OpenSSH 连接中断后可能残留随机 `.tmp` 文件；
- Native OpenSSH 大文件上传只有开始/完成进度，没有中间字节进度。

这些问题不再影响核心 State 正确性，但建议发布 `v1.2.2` 进行轻量收口。

---

# 2. 审计范围

本轮重点复核上一轮报告中的所有问题：

## OpenSSH

- Alias 连接前复核；
- Host/User/Port Pinning；
- ProxyJump / ProxyCommand 保留；
- ControlMaster；
- Pool Health Check；
- Dead Master Eviction；
- Retry Reconnect；
- Connect Timeout；
- Authentication Wait；
- Transfer Timeout；
- SFTP Probe；
- Delete；
- Rename/Backup Swap；
- WSL/1Password 边界。

## Workspace

- Target 全量预检；
- Remote Root Ownership；
- Lock 全量获取；
- Prepare All；
- Combined Plan；
- Confirm Once；
- Sequential Deploy；
- Independent State；
- Partial Failure；
- Rerun Convergence；
- Shared Connection Pool；
- Workspace Build；
- Workspace Doctor。

## FTP/SFTP

- Executable Mode；
- Delete Missing；
- Delete Permission；
- Connect Retry；
- Upload Retry；
- Idempotence；
- Bulk Delete；
- Root Check。

## 发布

- Main Commit；
- v1.2.1 Tag；
- Package Version；
- CI；
- Test Matrix；
- Wheel Build；
- Isolated Install。

---

# 3. 审计方式与限制

通过 GitHub Connector 读取：

- 最新提交；
- PR #9；
- PR Review Threads；
- v1.2.1 Tag；
- Main 源码；
- 测试源码；
- GitHub Actions Run；
- Python 3.11/3.12 Job Steps；
- v1.2.0 → v1.2.1 文件增量。

本地再次尝试：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

当前执行环境仍然无法解析 `github.com`，因此无法独立运行：

```bash
uv lock --check
uv run pytest -q
uv run ruff check src tests
uv run ty check src
uv build --clear
```

本报告的动态验证依据是 GitHub Actions；源码结论来自独立静态审计。

---

# 4. 版本与 CI

## 4.1 Main

```text
9af7be31cba487ad46d50cb143447df43f415bc1
Merge pull request #9 from howjc/agent/v1.2.1-deep-audit
```

修复提交：

```text
6da8c1b6a69a53204e3ab3a9fddba50a0d21372e
release v1.2.1: close deep audit findings
```

## 4.2 版本

Main：

```toml
version = "1.2.1"
```

Tag `v1.2.1`：

```toml
version = "1.2.1"
```

`pyproject.toml` Main 与 Tag Blob 一致。

关键文件：

```text
src/git_deploy/transports/openssh_sftp.py
src/git_deploy/workspace.py
```

Main 与 `v1.2.1` Tag Blob 一致。

## 4.3 CI

PR Head GitHub Actions：

```text
status: completed
conclusion: success
```

Python 3.11、3.12 均通过：

- Python Interpreter Check；
- `uv lock --check`；
- Dependency Install；
- `pytest`；
- Ruff；
- ty；
- Wheel/sdist Build；
- Isolated Wheel Install；
- Version/Help Smoke。

PR 记录：

```text
134 passed on Python 3.11
134 passed on Python 3.12
```

---

# 5. 上一轮 P0 修复验证

## P0-01：OpenSSH Alias 漂移

### 修复状态

**通过。**

当前连接前执行：

```text
resolve_current_ssh_alias()
```

并比较：

```text
approved host/user/port
current host/user/port
```

发生变化时：

```text
stale target: SSH alias changed after plan; re-run required
```

真实 ControlMaster、Control Command 和 SFTP Batch 都传入：

```text
HostName=<frozen host>
User=<frozen user>
Port=<frozen port>
ConnectTimeout=<configured seconds>
```

因此：

```text
Alias 保留 IdentityFile/ProxyJump/ProxyCommand
Endpoint 使用已审阅冻结值
```

### 测试状态

已覆盖：

- Host 漂移；
- User 漂移；
- Port 漂移；
- Drift 时不执行 `-MNf`；
- Workspace Prepare 后 Alias 漂移；
- Drift 时零 SFTP；
- Drift 时 State 不提交；
- 实际命令包含 Frozen Endpoint。

### 评价

这一实现同时做到：

```text
Re-resolve
+
Compare
+
Pin
```

比仅重新检查或仅 Pin 更可靠。

---

## P0-02：Native SFTP 删除探测

### 修复状态

**通过。**

当前定义：

```text
PathProbeResult.EXISTS
PathProbeResult.MISSING
PathProbeResult.ERROR
```

`run_batch()` 固定：

```text
LC_ALL=C
```

探测规则：

```text
returncode = 0
    → EXISTS

明确 No such file / not found
    → MISSING

其他非 0
    → ERROR
```

只有 `MISSING` 才允许：

```python
delete() return
```

权限、网络、Timeout、Dead Control Socket 等错误会抛出 `DeployError`，State 不会保存。

### 测试状态

已覆盖：

- Permission Denied；
- Connection Closed；
- Connection Timed Out；
- Network Unreachable；
- Stat Permission Denied；
- Control Socket Missing；
- 明确 No Such File；
- 明确 Not Found。

### 评价

上一轮最危险的：

```text
删除未执行
State 却推进
```

已经关闭。

---

## P0-03：Workspace 远端 Root 所有权

### 修复状态

**通过。**

Workspace Preflight 现在在任何 Build 或 Lock 前：

1. 加载所有 Config；
2. 选择统一 Target；
3. 解析 Native OpenSSH Alias；
4. 冻结物理 Endpoint；
5. 校验 Native OpenSSH Tools；
6. 比较所有远端 Root；
7. 之后才获取 Lock 和运行 Build。

Endpoint Key：

```text
protocol
host
username
port
```

同 Endpoint 下拒绝：

```text
root A == root B
root A parent of root B
root B parent of root A
```

### 测试状态

已覆盖：

- SFTP 相同 Root；
- SFTP 父子 Root；
- FTP 父子 Root；
- Sibling Root；
- 不同 Endpoint 相同路径文本；
- 不同 Alias 解析到同 Endpoint；
- 冲突发生在 Build 前；
- 冲突后 Lock 可重新获取。

### 评价

Thin Workspace 内部的跨仓远端文件所有权边界已经可靠。

---

# 6. 上一轮 P1 修复验证

## P1-01：Dead Pooled ControlMaster

### 修复状态

**通过。**

Pool：

```python
existing.is_healthy()
```

失效时：

```python
pool.invalidate(existing)
candidate.connect()
```

Operation Retry：

```python
transport.invalidate_connection()
connect again
```

Pooled Transport 的 `invalidate_connection()` 会：

```text
从 Pool 精确移除 Master
关闭 Master
清除 Transport 引用
```

### 测试状态

已覆盖：

- Cached Master 健康检查；
- Dead Master Eviction；
- Pool 只保留 Replacement；
- Operation 第一轮 Connection Closed；
- Retry 建立第二个 `-MNf`；
- 第二轮 Upload 成功。

---

## P1-02：Timeout 分离

### 修复状态

**通过。**

当前：

```text
target.timeout
    → OpenSSH ConnectTimeout
```

不再作为：

```text
subprocess.run whole authentication timeout
subprocess.run whole SFTP batch timeout
```

因此：

- 1Password/Windows Hello 授权不再被 15 秒杀死；
- 大文件传输不再被 15 秒中断；
- Pool 不受第一仓库 Python Batch Timeout 影响。

### 测试状态

已验证：

- Master Command 包含 `ConnectTimeout`；
- SFTP Command 包含 Frozen Endpoint；
- `subprocess.run` 不带 `timeout=`；
- 不同 Repository Timeout 仍可复用 Endpoint Master。

### 剩余边界

没有 Operation Timeout 意味着异常服务器可能无限挂起。

对于交互式个人工具，这是比 15 秒误杀更合理的默认。

后续可增加可选：

```toml
operation_timeout = null
```

但不应恢复短默认值。

---

## P1-03：Combined Plan 可见性

### 修复状态

**通过。**

每仓展示：

```text
Alias → Host
User
Port
Remote Root
Protocol
FULL / INCREMENTAL
Previous Commit → HEAD
Operation List
Upload/Delete Summary
```

Workspace 总计展示：

```text
Total Uploads
Total Deletes
Frozen Bytes
```

不展示密码、Agent Socket 或凭据。

---

## P1-04：Doctor Fail Closed

### 修复状态

**通过。**

Single Project Doctor：

```text
Target Resolve Failure
    → 记录 target config failure
    → return local checks
    → no transport
```

Workspace Doctor：

1. 先加载和解析全部 Target；
2. 校验 Remote Ownership；
3. 任一失败时 `remote_checks=False`；
4. 所有 Repository 都不创建 Transport；
5. `--create-root` 也不会连接或写远端。

测试已覆盖后面 Repository Alias 失败阻止前面 Repository Create Root。

---

# 7. 新发现与未关闭问题

## P1-01：FTP 批量删除存在 O(D × N) 目录扫描

### 当前实现

每删除一个文件：

```python
entries = ftp.nlst(parent)
```

然后在目录完整列表中寻找文件名。

假设：

```text
D = 待删除文件数量
N = 目录文件数量
```

复杂度接近：

```text
O(D × N)
```

典型前端目录：

```text
public/assets/
```

可能包含大量 Hash Asset。

如果一次轮换数百个文件，每个 Delete 都重新完整列目录，会产生大量 FTP 往返和目录数据传输。

### 影响

- 删除阶段明显变慢；
- 慢速海外 FTP 更严重；
- 可能触发服务器 Timeout；
- 重试又重复扫描；
- 多仓 Workspace 中延迟累积。

### 修复建议

在 `FTPTransport` 中增加当前连接级缓存：

```python
_directory_entries: dict[str, set[str]]
```

第一次访问 Parent：

```text
NLST once
```

后续删除复用缓存。

成功 Delete 后：

```text
remove name from cache
```

Upload 后：

```text
add name to cache
```

Reconnect / Close 后：

```text
clear cache
```

### 优先级

P1 性能。

对少量删除影响小，但前端 Build Asset 清理可能明显受影响。

---

## P1-02：FTP 父目录缺失时删除不能自然收敛

### 当前实现

Delete 先：

```python
ftp.nlst(parent)
```

如果 Parent 本身已经不存在：

```text
550 No such directory
```

当前代码将其视为：

```text
FTP existence probe failed
```

而不是：

```text
Target 已经不存在
Delete 已完成
```

部分 FTP Server 对空目录执行 NLST 也可能返回 `550 No files found`。

### 影响

- 远端目录被人工删除后，计划中的文件删除全部失败；
- 部分中断/手工处理场景不能通过重跑自然收敛；
- 不会错误提交 State，但会阻塞部署。

### 修复建议

目录 Probe 也使用三态：

```text
PARENT_EXISTS
PARENT_MISSING
ERROR
```

父目录明确 Missing：

```text
Delete idempotent success
```

空目录：

```text
Target missing
Delete idempotent success
```

权限错误：

```text
DeployError
```

可优先使用：

```text
MLST target
SIZE target
```

再根据服务器能力回退到缓存的 NLST。

### 优先级

P1 兼容性/收敛性。

---

## P2-01：Workspace Build 被远端 Preflight 绑定

### 当前实现

```python
run_workspace_build()
    ↓
preflight_workspace(require_git=False)
```

虽然 `require_git=False`，但仍执行：

- Target 解析；
- `ssh -G`；
- POSIX `ssh` / `sftp` 探测；
- Remote Root Ownership 校验。

### 问题

`git-deploy build` 应该是纯本地命令。

当前可能因为以下原因无法构建：

- 当前机器未安装 SSH/SFTP；
- 远端 Alias 临时无效；
- SSH Config 尚未配置；
- 两仓部署 Root 冲突；
- 在 CI Build Machine 中没有部署环境。

这些问题与本地 npm/pnpm/Composer Build 无关。

### 修复建议

Workspace Build 只做：

1. Load 所有 Config；
2. 如果用户传了 Target，只验证 Target Name 存在；
3. 检查 Build Command；
4. 顺序 Build。

不执行：

```text
ssh -G
Native Tool Discovery
Remote Ownership
Git State
Remote Connect
```

甚至可以进一步简化：

```bash
git-deploy build
```

不需要 Target，因为当前 Build Config 不是 Target-specific。

### 优先级

P2 产品契约/认知负荷。

---

## P2-02：不同命令之间没有物理目标全局锁

Workspace 内已经拒绝重叠 Root，但以下两个独立命令仍可能并发：

```text
Terminal A:
  cd repo-a
  git-deploy prod

Terminal B:
  cd repo-b
  git-deploy prod
```

如果两个独立 Repository 错误配置到同一物理 Root：

- Repository Lock 分别位于不同 Git Common Dir；
- 两个 Lock 都会成功；
- 远端文件操作可能交错。

### 当前风险水平

用户是个人单控制器，并且正常使用统一 Workspace 时，风险较低。

### 可选修复

增加本机物理目标锁：

```text
$XDG_STATE_HOME/git-deploy/targets/<fingerprint>.lock
```

不需要：

- 远端分布式锁；
- Server Lock File；
- Global Transaction。

---

## P2-03：Native OpenSSH 中断可能残留远端临时文件

上传使用随机：

```text
<target>.git-deploy-<uuid>.tmp
```

如果连接在 Upload 后、Publish 前断开：

- Cleanup 也可能使用死 Master 失败；
- Retry 使用新的 UUID；
- 旧 `.tmp` 可能残留。

不会覆盖正式文件，也不会错误提交 State，但长期故障可能累积垃圾。

后续可：

- 在同路径上传前清理本进程已知旧 Temp；
- Doctor 提示 stale `.git-deploy-*.tmp`；
- 不建议扫描删除未知历史文件。

---

## P2-04：Native OpenSSH 进度不是实时字节进度

当前回调：

```text
0 / total
上传完成
total / total
```

大文件过程中没有中间更新。

建议后续至少显示：

```text
Uploading large.bin...
```

或 Spinner。

不建议为了进度重新读取私钥或放弃 Native OpenSSH。

---

## P2-05：远端 Root 冲突无法识别 DNS 等价主机

Ownership Key 使用：

```text
protocol
host string
username
port
```

以下两项如果解析到同一 IP：

```text
prod-a.example.com
prod-b.example.com
```

仍会被视为不同 Endpoint。

对 Native Alias，`ssh -G` 通常会解析到 HostName，因此多数 Alias 场景能够识别。

不建议强制 DNS 解析作为身份，因为：

- DNS 可能多 IP；
- DNS 结果随网络变化；
- ProxyJump 场景不等于直连 IP；
- 可能引入新的不稳定性。

应继续将“不要用多个不同 Host 名管理同一 Root”作为配置责任。

---

## P2-06：OpenSSH 认证等待可能无限挂起

移除 Python 15 秒 Timeout 是正确修复。

但如果：

- 用户长时间不处理生物认证；
- SSH 服务卡住；
- ProxyCommand 不返回；

命令可能一直等待。

当前可通过：

```text
Ctrl-C
```

终止。

后续可增加显式：

```toml
authentication_timeout = null
```

默认仍建议无限，避免误杀 1Password。

---

# 8. 测试覆盖评价

## 已新增并有效覆盖

### Alias

- Host Drift；
- User Drift；
- Port Drift；
- Pinning；
- Workspace Confirmation Window；
- Zero Mutation；
- State Unchanged。

### Probe

- Permission；
- Dead Master；
- Network；
- Timeout；
- Missing；
- Locale。

### Pool

- Healthy Reuse；
- Dead Eviction；
- Retry Reconnect；
- Second Master；
- Close All。

### Workspace

- Same Root；
- Parent/Child Root；
- FTP Collision；
- Different Alias Same Endpoint；
- Build Before Collision；
- All Locks Before Build；
- Combined Endpoint；
- Frozen Bytes；
- Doctor All Preflight；
- Repository Name。

### Timeout

- No Python Whole-process Timeout；
- ConnectTimeout Option；
- Different Repository Policy Sharing。

## 仍缺少

- FTP 100+ Deletes in One Parent；
- FTP Parent Missing；
- FTP Empty Parent Returning 550；
- Workspace Build Without `ssh`/`sftp`；
- Workspace Build with Invalid Alias；
- Different Independent Commands Same Physical Root；
- Remote Temp Residue after Dead Master。

---

# 9. 推荐 v1.2.2 小版本

不需要继续增加产品功能。

建议只完成：

## Patch A：FTP Delete Cache

- Parent Listing Cache；
- Cache after Upload；
- Cache after Delete；
- Clear on Reconnect；
- Bulk Delete Test；
- 复杂度从重复扫描降为一次 Parent Listing。

## Patch B：FTP Missing Parent

- Target/Parent Probe 三态；
- Missing Parent = Idempotent Success；
- Empty Parent Compatibility；
- Permission Error Fail Closed；
- Rerun Convergence Test。

## Patch C：Pure Workspace Build

- Build 不解析 SSH Alias；
- Build 不要求 Native Tools；
- Build 不检查 Remote Ownership；
- Build 不需要 Target 或只验证 Target Name；
- CI Build-only Test。

## Patch D：Minor Cleanup

- Ctrl-C Master Directory Cleanup；
- Native Temp Residue Documentation；
- Optional Physical Target Lock ADR。

---

# 10. 场景结论

| 场景 | 结论 |
|---|---|
| 单仓 Native OpenSSH + WSL + 1Password | **通过，可进入真实验收** |
| 多仓 Thin Workspace + 相同 Alias | **通过，可进入真实验收** |
| Alias 在确认窗口变化 | **已安全拒绝** |
| Native SFTP 删除 | **通过** |
| Dead Master Retry | **通过** |
| 大文件超过 15 秒 | **通过，不再误杀** |
| Paramiko SFTP | **通过** |
| FTP 少量上传/删除 | **条件通过** |
| FTP 大批量 Hash Asset 删除 | **建议先修 v1.2.2** |
| Workspace Build-only | **功能可用但边界过重** |
| 两个独立仓库命令管理同一 Root | **不保证互斥** |
| 跨设备同时部署 | **不保证互斥** |

---

# 11. 最终结论

v1.2.1 已经正确完成上一轮审计要求。

核心安全属性现在成立：

```text
用户审阅的 Endpoint
    =
真实连接的 Endpoint

明确 Missing
    =
允许跳过 Delete

Permission/Network/Dead Master
    =
部署失败，State 不更新

同 Workspace 远端 Root 重叠
    =
Build 前拒绝

Dead Pooled Master
    =
驱逐并重连

大文件 / 生物认证
    =
不再被 15 秒 Python Timeout 杀死
```

因此本轮结论由 v1.2.0 的“不通过”调整为：

> **v1.2.1 有条件通过。Native OpenSSH/SFTP 与 Thin Workspace 主链可以进入真实日常使用验收；剩余问题主要是 FTP 批量删除性能/兼容性和 Workspace Build 的纯本地边界，不需要再次重构架构。**

建议：

1. 开始在真实 WSL + 1Password + 多仓项目中持续使用；
2. 观察至少一周日常部署；
3. 以 v1.2.2 修复 FTP Delete 和 Workspace Build；
4. 暂停新增更复杂的 Workspace 功能；
5. 继续坚持“一条命令、每仓独立、失败重跑”的产品边界。
