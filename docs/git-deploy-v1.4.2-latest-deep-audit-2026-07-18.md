# git-deploy v1.4.2 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`faeaefba293bf397197a26e6ee4f3e8c58767821`
> 功能提交：`8a34b7ef56003f892e147681f2d803370cb65df1`
> PR：`#13 release v1.4.2: harden hybrid runtime safety`
> 版本：`v1.4.2`
> 审计日期：`2026-07-18`
> 审计结论：**有条件通过**
> Paramiko Hybrid：**通过**
> Native OpenSSH Hybrid：**存在一个 P1 No-overwrite 语义缺口**
> 建议版本：`v1.4.3` 小型安全修复

> v1.4.3 整改状态（2026-07-18）：第 11 节 TODO 已全部实现并具备单元、真实 Native OpenSSH、WSL2/Ubuntu 24.04 验证；详见 `docs/release-notes-v1.4.3.md`。

---

# 1. 执行摘要

v1.4.2 对 v1.4.1 深度审计中提出的剩余问题进行了集中修复，且没有扩张产品范围。

已经确认完成：

1. 所有 Root File 和 Mirror Directory 都先完整上传到 Stage；
2. 所有 Stage 上传完成后执行第二次 Remote Freshness Gate；
3. Stage 上传重试和 SSH 重连后仍会经过第二次 Gate；
4. 每个在线路径在 Backup 前重新核对 Remote Plan 中冻结的 Expected Type；
5. 计划时 Missing 的路径使用 No-overwrite Rename 发布；
6. 新 Ownership Manifest 写入前再次核对旧 Ownership 原始 Hash；
7. Stale Stage 只清理 `.git-deploy` 内部的 Stage、Backup 和 Recovery；
8. Stale 内部清理失败时保留可显式恢复的 Recovery；
9. `--recover` 已拆分为独立 Recovery-only Prepare；
10. Recovery 不再运行 Build、不读取既有 State 内容、不扫描 Local Hybrid、不冻结当前部署文件；
11. Workspace Recovery 只显示、确认和执行实际存在 Recovery 的项目；
12. Schema-1 已提交 Ownership 且 Commands Pending 时 Fail Closed；
13. Recovery Plan 不再混入当前 Source/Output 上传和删除；
14. Cleanup 提示已指向 `--remote-plan` 和 `--recover`；
15. `allow_recovery` 兼容参数已经从主调用链移除。

以上修复覆盖了 v1.4.1 报告提出的全部事项。

但是，本轮发现一个 Native OpenSSH 特有的实现偏差：

> `OpenSSHSFTPTransport.rename_path()` 先 `lstat(destination)`，随后通过批处理发送普通 `sftp rename source destination`。OpenSSH 客户端在服务端支持 `posix-rename@openssh.com` 时，默认使用 POSIX Rename；服务端直接调用 `rename(oldpath, newpath)`，会覆盖在两次操作之间突然出现的目标。

因此下面的最后一刻竞态仍可能发生：

```text
lstat(final) = Missing
        ↓
外部写入者创建 final
        ↓
sftp rename stage final
        ↓
POSIX Rename 覆盖 final
```

这意味着：

- Paramiko 路径的 No-overwrite 语义成立；
- Native OpenSSH 路径的 `lstat + rename` 不是原子 No-overwrite；
- v1.4.2 对“目标在最终 Rename 前最后一刻出现”的保护没有在用户主要使用的 Native OpenSSH 后端上完全成立。

该问题需要外部写入者命中非常窄的时间窗口，因此定级为 **P1**，而不是普通单发布器场景下的 P0。

建议发布 v1.4.3，仅进行一个很小的修复：

```text
Native OpenSSH rename_path:
    rename source destination
        ↓
    rename -l source destination
```

OpenSSH `sftp` 的 `-l` 会强制使用传统 `SSH2_FXP_RENAME`，避免自动选择可覆盖目标的 `posix-rename@openssh.com`。

---

# 2. 审计范围

## 2.1 发布完整性

- Main 最新 Commit；
- PR #13；
- v1.4.2 Tag；
- Main/Tag Package Blob；
- Main/Tag Deployer Blob；
- GitHub Actions；
- Python 3.11；
- Python 3.12；
- Ruff；
- ty；
- Wheel/sdist；
- Isolated Wheel Smoke；
- PR Review Threads。

## 2.2 v1.4.1 剩余问题

- Stage 后第二次 Freshness Gate；
- Retry/Reconnect 后 Freshness；
- Expected-Type-aware Backup；
- Planned-Missing No-overwrite；
- Ownership Precommit Hash Gate；
- Recovery-only Prepare；
- Corrupt State Recovery；
- Broken Build Recovery；
- Missing Local Hybrid Recovery；
- Schema-1 Pending Command；
- Recovery-only Render；
- Workspace Recovery Selection。

## 2.3 失败状态机

- Initial Stale；
- Secondary Stale；
- Secondary Stale Cleanup Failure；
- Last-moment Destination Appearance；
- Expected Type Drift；
- Ownership Precommit Drift；
- Stage Failure；
- Swap Failure；
- Ownership Write Failure；
- Command Failure；
- State Failure；
- Cleanup Failure；
- Ctrl-C；
- Schema-1 Recovery；
- Workspace Partial Failure。

## 2.4 Transport

- Paramiko Rename；
- Native OpenSSH Rename；
- SFTP Standard Rename；
- OpenSSH POSIX Rename Extension；
- No-overwrite Contract；
- Recovery Restore；
- Stage Cleanup。

---

# 3. 审计方式与限制

本轮通过 GitHub Connector 读取：

- Commit；
- PR Metadata 和 Diff；
- Main/Tag 文件；
- GitHub Actions；
- PR Review Threads；
- Planner；
- Deployer；
- Hybrid Recovery；
- Prepared Recovery；
- CLI；
- Workspace；
- Native OpenSSH Transport；
- Paramiko Transport；
- 单元测试；
- Docker Integration Tests；
- Release Notes 和 ADR。

同时查阅 OpenSSH Portable 官方源码以确认：

- `sftp rename` 如何选择协议；
- `posix-rename@openssh.com` 的服务端实现；
- `rename -l` 与传统 Rename 的关系。

尝试独立 Clone：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

当前执行环境仍无法解析：

```text
github.com
```

错误：

```text
Could not resolve host: github.com
```

因此本轮无法独立执行：

```bash
uv lock --check
uv run --isolated --python 3.11 --all-groups pytest -q
uv run --isolated --python 3.12 --all-groups pytest -q
uv run ruff check src tests examples
uv run ty check src
uv build --clear
```

动态验证依据为成功的 GitHub Actions 和仓库中的真实集成测试；剩余问题来自当前 Main 与 OpenSSH 官方实现的交叉审计。

---

# 4. 版本、Tag 与 CI

## 4.1 Main

```text
faeaefba293bf397197a26e6ee4f3e8c58767821
Merge pull request #13 from howjc/agent/v1.4.2-runtime-freshness
```

功能提交：

```text
8a34b7ef56003f892e147681f2d803370cb65df1
release v1.4.2: harden hybrid runtime safety
```

## 4.2 PR

```text
PR #13
release v1.4.2: harden hybrid runtime safety
merged: true
```

PR 记录：

```text
231 tests
Ruff
ty
Wheel/sdist
Python 3.12 isolated tests
Real Paramiko
Real Native OpenSSH
```

## 4.3 Package Version

Main：

```toml
version = "1.4.2"
```

Tag：

```toml
version = "1.4.2"
```

两者 `pyproject.toml` Blob 相同：

```text
462978771ec739c72ca9c9ed0cb6794149b25c28
```

Main 与 Tag 的 `deployer.py` Blob 相同：

```text
733b255c497d687b354194ac04d3a233bd84c090
```

## 4.4 CI

GitHub Actions：

```text
status: completed
conclusion: success
```

Python 3.11 和 3.12 均通过：

- Interpreter Check；
- Lockfile Check；
- Dependency Install；
- Tests；
- Ruff；
- ty；
- Build Package；
- Isolated Wheel Install；
- CLI Smoke。

PR 无未解决 Review Thread。

---

# 5. 上一轮问题关闭情况

---

## 5.1 P1：Stage 上传期间的二次 TOCTOU

### v1.4.1 问题

v1.4.1 只在执行开始时做一次 Freshness Gate。

随后还会经历：

- 普通 Source/Output 操作；
- 创建 Recovery；
- 上传完整 Stage；
- 网络 Retry；
- SSH Reconnect。

如果目标在 Stage 期间出现，仍可能被接管。

### v1.4.2 实现

执行顺序改为：

```text
Initial Freshness
    ↓
普通 Source/Output
    ↓
Recovery PREPARED
    ↓
Root File → Stage
Mirror Directory → Stage
    ↓
Recovery STAGED
    ↓
Secondary Freshness
    ↓
Recovery SWAPPING
```

第二次 Gate 重新核对：

- Ownership Raw Hash；
- Recovery Record；
- Current/Historical Path Types。

### Retry/Reconnect

Stage 上传发生 Retry 和 Reconnect 后：

```text
上传完成
    ↓
仍然执行 Secondary Freshness
```

### Stale Cleanup

第二次 Gate 失败时：

```text
在线 Final Path 不修改
只清理：
    Stage Root
    Empty Backup Root
    Recovery Record
```

清理失败时：

```text
保留 STAGED Recovery
提示 --remote-plan / --recover
```

### 测试

覆盖：

- Path 在 Stage 期间出现；
- Stage Retry 后 Path 出现；
- Reconnect 后 Path 出现；
- Stale Cleanup 成功；
- Stale Cleanup 失败；
- Recovery 后外部 Path 保留；
- Real Paramiko Stage Race。

### 结论

**通用逻辑关闭。**

Native 最后一个 Rename 原子性例外见 P1-01。

---

## 5.2 Expected-Type-aware Backup

### 实现

`_backup_current()` 现在接收：

```python
expected_type: RemotePathType
```

并执行：

```text
Current Type != Expected Type
    → StaleRemotePlanError

Expected Missing
    → 不 Backup

Expected File/Directory
    → 移动到 Backup
```

### old_existing_names

不再在执行阶段重新推断：

```text
当前是否存在
```

而是从用户审阅的：

```text
expected_path_types
```

生成。

这防止 Stage 期间出现的新路径被错误写入：

```text
old_existing_names
```

### 测试

覆盖：

- File → Directory；
- Directory → File；
- SWAPPING 后、Backup 前类型变化；
- 外部替换内容保持。

### 结论

**关闭。**

---

## 5.3 Root File 统一 Stage

v1.4.1 的 Root File 在 SWAPPING 阶段直接上传到 Final。

v1.4.2 改为：

```text
Root File
    → Stage/<file>

Mirror Directory
    → Stage/<directory>
```

然后统一：

```text
Stage 完整
    → Secondary Freshness
    → Swap
```

收益：

- Root File 也受第二次 Gate；
- 上传过程不会直接修改在线 Root File；
- 最后发布使用同一 Recovery 边界；
- Retry 只修改内部 Stage。

### 结论

**关闭。**

---

## 5.4 Planned-Missing No-overwrite

### 设计

如果 Remote Plan 记录：

```text
Expected Type = Missing
```

则：

```text
不允许 Backup 当前突然出现的 Path
Stage Rename 必须不覆盖目标
```

### Paramiko

Paramiko 使用标准 SFTP Rename。

如果 Final 最后一刻出现：

```text
Rename 失败
Stage 仍存在
Final 外部内容保留
Recovery 后 Stage 清理
```

### 内存测试

覆盖：

```text
Secondary Gate 通过
    ↓
Rename 前创建 Final
    ↓
No-overwrite Failure
    ↓
External Final 保留
```

### Native OpenSSH

存在实现偏差，详见 P1-01。

---

## 5.5 Ownership Commit 前新鲜度

所有路径 Swap 完成后、写新 Ownership 前再次执行：

```text
validate_remote_freshness(
    check_path_types=False
)
```

此时不再检查旧路径类型，因为它们已经被部署主动改变。

仍然检查：

- Recovery Record；
- Old Ownership Raw Hash。

如果其他发布器修改旧 Ownership：

```text
不覆盖外部 Ownership
保留 Recovery
```

测试覆盖：

```text
Stage Publish 后修改 Ownership
    → StaleRemotePlanError
    → 外部 Ownership 保留
```

### 结论

**关闭。**

---

## 5.6 独立 Recovery-only Prepare

### v1.4.1 问题

`--recover` 仍调用普通 `prepare_project()`，依赖：

- Build；
- Local State 解析；
- Dirty Worktree；
- Local Hybrid；
- Freeze；
- Temp Disk。

### v1.4.2 实现

新增：

```python
PreparedRecovery
prepare_recovery()
create_recovery_plan()
execute_prepared_recovery()
```

Recovery-only Prepare 只做：

```text
Load Config
Resolve Target
Get Git Common Dir
Acquire Target Lock
Connect
Read Ownership
Read Recovery
Inspect Recovery
Freeze Command Contract
```

不做：

```text
Build
State Load
Source Diff
Output Scan
Local Hybrid Scan
Freeze Upload Bytes
Temp Disk Check
Clean Worktree Check
```

### 测试

测试同时破坏：

- Build Command；
- `.deploy/`；
- Worktree Clean；
- Existing State JSON；
- Deployment Freeze Helpers。

随后：

```bash
git-deploy --recover --yes
```

仍然成功恢复。

### 结论

**关闭。**

---

## 5.7 Workspace Recovery-only

Workspace 新增：

```python
prepare_workspace_recovery()
render_workspace_recovery_plan()
execute_workspace_recovery()
```

特点：

- 不执行 Build；
- 只保留实际有 Recovery 的项目；
- Recovery 前先重新验证全部选中项目；
- 后一项目 Stale 时前一项目不先写入；
- Combined View 只显示 Recovery；
- 统计实际 Pending Commands。

### 结论

**关闭。**

---

## 5.8 Schema-1 Pending Commands

### v1.4.1 问题

Schema 1 没有：

```text
command_hash
```

但已提交 Ownership 且 Commands Pending 时，可能执行当前配置命令。

### v1.4.2 实现

`inspect_recovery()`：

```text
schema == 1
and commands_pending
    → DeployError
      legacy command contract unknown
```

允许：

```text
Schema-1 Pre-commit Restore
```

拒绝：

```text
Schema-1 Ownership Committed
Commands Pending
```

Doctor 同步显示：

```text
Legacy Command Contract Unknown
```

### 结论

**关闭。**

---

## 5.9 Recovery-only Render

Pending Recovery 存在时：

```text
当前 plan.operations = ()
```

显示：

```text
Mode: RECOVERY
Recovery Phase
Recovery Action
Pending Commands
State Pending
Cleanup Pending
```

不再显示：

- 当前 Source Upload；
- 当前 Output Delete；
- 当前 Hybrid Mirror；
- 不会在 Recovery 中执行的普通操作。

### 结论

**关闭。**

---

# 6. P1-01：Native OpenSSH 的 No-overwrite Rename 并不成立

## 6.1 严重性

```text
级别：P1
影响后端：Native OpenSSH
影响场景：Planned-Missing Path 在最终 Rename 窗口出现
用户相关性：高，当前主要环境是 WSL + Native OpenSSH + 1Password Agent
```

## 6.2 git-deploy 当前实现

`OpenSSHSFTPTransport.rename_path()`：

```python
if self.lstat(destination) is not MISSING:
    raise

sftp batch:
    rename source destination
```

其函数注释声明：

```text
without overwriting a destination
```

但实现是：

```text
Check
    ↓
Rename
```

二者不是单一原子操作。

## 6.3 OpenSSH 客户端行为

OpenSSH `sftp` 的 Rename 命令：

```text
rename source destination
```

在没有 `-l` 时，将 `lflag = false` 传入：

```c
sftp_rename(conn, path1, path2, lflag)
```

`sftp_rename()` 在服务端支持：

```text
posix-rename@openssh.com
```

且没有强制 Legacy 时，会发送：

```text
SSH2_FXP_EXTENDED(posix-rename@openssh.com)
```

而不是传统：

```text
SSH2_FXP_RENAME
```

## 6.4 OpenSSH 服务端行为

OpenSSH 服务端的 POSIX Rename Handler 直接执行：

```c
rename(oldpath, newpath)
```

POSIX Rename 在目标已存在时会替换目标。

因此：

```text
lstat(destination) = Missing
        ↓
外部写入者创建 destination
        ↓
OpenSSH POSIX Rename
        ↓
destination 被覆盖
```

## 6.5 为什么现有测试没有发现

### Memory Transport

内存测试的 `rename_path()`：

```text
Destination Exists
    → Raise
```

它模拟的是 No-overwrite Transport，而不是 OpenSSH POSIX Rename。

### Real Paramiko

真实 Paramiko 集成测试覆盖了：

- Initial Freshness；
- Stage Race。

Paramiko 标准 Rename 能按预期失败。

### Real Native

真实 Native 集成测试覆盖：

- Initial Confirmation-window Stale；
- 正常 Stage/Swap；
- Ownership 读取。

没有覆盖：

```text
Native lstat 返回 Missing
    ↓
在 Batch Rename 前创建 Final
    ↓
执行真实 sftp rename
```

## 6.6 可复现逻辑

```text
1. Remote Plan:
       late.txt = Missing

2. Initial Freshness:
       late.txt = Missing

3. Stage Upload:
       Stage/late.txt 完成

4. Secondary Freshness:
       late.txt = Missing

5. _backup_current:
       lstat(late.txt) = Missing
       return

6. 外部写入者创建:
       late.txt = important

7. Native rename_path:
       内部再次 lstat 可能仍在创建前完成
       sftp rename Stage/late.txt late.txt

8. OpenSSH 使用 posix-rename:
       important 被覆盖
```

时间窗口很窄，但它正是 v1.4.2 声称通过 No-overwrite 关闭的最后一刻窗口。

## 6.7 最小修复

修改：

```python
f"rename {source} {destination}"
```

为：

```python
f"rename -l {source} {destination}"
```

OpenSSH `-l` 会强制传统 Rename，不使用 POSIX Rename Extension。

对 git-deploy 的 `rename_path()` 来说，所有 Destination 本来都要求 Missing，因此不需要 POSIX 覆盖语义。

适用：

- Final → Backup；
- Stage → Final；
- Recovery Backup → Final；
- Internal Path Rename。

## 6.8 建议 API 收口

可选将 Transport 方法重命名为：

```python
rename_path_no_replace()
```

或在 Base Contract 中明确：

```text
Destination must be absent at the remote atomic rename point.
```

但不是 v1.4.3 的必要条件。

## 6.9 必须新增测试

### Unit

- [x] Native Batch Payload 必须包含 `rename -l`；
- [x] 普通 `rename ` 不得出现在 `rename_path()`；
- [x] Backup Rename 使用 `-l`；
- [x] Recovery Restore 使用 `-l`。

### Real Native Integration

注入流程：

```text
transport.rename_path()
    lstat destination → Missing
    ↓
测试 Hook 创建 destination
    ↓
执行真实 sftp rename -l
```

验收：

- [x] Rename 失败；
- [x] Stage 保留；
- [x] External Final 保留；
- [x] StaleRemotePlanError；
- [x] `--recover` 不删除 External Final；
- [x] Recovery 清理 Stage；
- [x] Ownership 不推进；
- [x] Local State 不推进。

---

# 7. P2：普通文件操作仍可能先于 Secondary Stale 发生

执行顺序仍为：

```text
Initial Freshness
    ↓
普通 Source/Incremental Operations
    ↓
Hybrid Stage
    ↓
Secondary Freshness
```

如果：

```text
普通 app.php 已上传
Hybrid Stage 期间发现 Stale
```

则：

- 普通文件已经更新；
- Hybrid 在线路径未开始 Swap；
- Ownership 不更新；
- Local State 不更新。

下一次部署会依据旧 State 重传普通文件并重新收敛。

这不是未知内容删除问题，也不违反项目“不提供全局事务”的范围。

但需明确：

> Secondary Freshness 保证 Hybrid Online Final Paths 零修改，不保证此前普通 Source/Incremental Operations 零修改。

如果未来需要跨普通文件与 Hybrid 的全局零写入，需要把所有普通文件也纳入 Stage/Transaction，明显超出 v1-lite 范围，不建议实现。

---

# 8. P2：没有远端租约，仍不支持真正多发布器

v1.4.2 已将 Planned-Missing 的竞态保护推进到最终 Rename。

但对于一个计划时已存在的路径：

```text
旧 Final → Backup
```

之后，如果另一个写入者重新创建 Final，再发生 Stage Publish：

- Publish 会失败；
- Recovery 为恢复旧版本，可能删除新创建的 Final；
- 恢复 Backup。

因此以下场景仍不受支持：

- 两台机器同时部署同一 Hybrid Root；
- CI 与本地同时部署；
- 面板/FTP 与 git-deploy 同时发布；
- 人工在 Swap 阶段修改已存在的受管 Path。

这属于无 Remote Lease 的明确边界。

当前本地 Target Lock 只保护：

```text
同一 Git Common Dir
```

无法保护：

```text
不同机器
不同 Clone
其他发布器
人工 SSH
```

建议保持文档约束，不立即引入远端租约。

---

# 9. P3：Workspace Recovery 仍依赖所有仓库 Preflight 和 Lock

`prepare_workspace_recovery()` 会：

1. Load/Validate 所有 Workspace Repository；
2. Resolve 所有 Target；
3. 验证 Remote Root 不重叠；
4. 获取所有 Repository Target Lock；
5. 再逐个发现 Recovery；
6. 最终只保留有 Recovery 的项目。

优点：

- Workspace Recovery 在执行前具有一致的全局视图；
- 防止其他本地部署进程同时启动。

缺点：

- 一个没有 Recovery 但 Lock 忙的 Repository，可能阻止另一个 Repository 的恢复；
- 一个无关 Repository Git 损坏，也会阻止整个 Workspace Recovery。

用户可以使用单项目：

```bash
git-deploy --config path/to/deploy.toml TARGET --recover
```

绕开该问题。

当前不建议为了这个低频场景增加复杂的 Workspace Lazy Preflight。

---

# 10. 已确认正确的核心能力

## 10.1 Config

- Single Hybrid；
- SFTP-only；
- `remote = "."`；
- Name；
- Project ID；
- Local Root 不得为 Project Root；
- `.git/.deploy/.git-deploy` Protect；
- Stable Path Component。

## 10.2 Local Aggregation

- Explicit Sources；
- Duplicate Detection；
- File/Directory Conflict；
- Symlink Reject；
- Atomic Local Replace；
- `.deploy/` Ignore。

## 10.3 Ownership

- Strict Schema；
- Project/Mapping/Remote Identity；
- Raw Byte Hash；
- State Loss Delete；
- Unknown Remote Ignore；
- Adoption；
- Historical Transfer Reject。

## 10.4 Runtime Safety

- Initial Freshness；
- Workspace All-project Initial Freshness；
- Root File Stage；
- Mirror Directory Stage；
- Secondary Freshness；
- Retry/Reconnect Freshness；
- Expected Type Backup；
- Ownership Precommit Hash；
- Stale Internal Cleanup；
- Recovery-safe Stage Preservation。

## 10.5 Recovery

- Independent Prepare；
- Independent Render；
- Independent Confirmation；
- Independent Execute；
- Commands Pending；
- State Pending；
- Cleanup Pending；
- Command Hash；
- Schema-1 Precommit Restore；
- Schema-1 Pending Command Fail Closed；
- Missing Backup Fail Closed；
- Doctor Diagnostics。

## 10.6 Tests

- 231 tests；
- Python 3.11；
- Python 3.12；
- Paramiko Real Integration；
- Native Real Integration；
- Stage Race；
- Retry/Reconnect Race；
- Expected Type Race；
- Ownership Commit Race；
- Broken Build Recovery；
- Corrupt State Recovery；
- Missing Local Hybrid Recovery；
- Recovery Render；
- Workspace Recovery。

---

# 11. v1.4.3 原子 TODO

## P1：Native No-overwrite

### TODO-001：强制 Legacy Rename

- [x] `OpenSSHSFTPTransport.rename_path()` 使用 `rename -l`；
- [x] 更新 Docstring；
- [x] 保持 Destination Preflight；
- [x] 保持错误上下文。

### TODO-002：Unit Test

- [x] 捕获 SFTP Batch Payload；
- [x] 断言 Payload 以 `rename -l` 开始；
- [x] 断言 Source/Destination Quote 不回归；
- [x] 断言不存在普通 `rename source destination`。

### TODO-003：Real Native Last-moment Test

- [x] Remote Plan Missing；
- [x] Secondary Freshness Missing；
- [x] Rename Batch 前创建 External Final；
- [x] Legacy Rename 失败；
- [x] External Final 保留；
- [x] Stage 保留或进入 Recovery；
- [x] Ownership 保持旧值；
- [x] State 保持旧值；
- [x] Recovery 后 External Final 保留。

### TODO-004：OpenSSH Compatibility

- [x] CI Fixture 验证 `rename -l`；
- [x] Ubuntu 24.04 OpenSSH 验证；
- [x] WSL2 OpenSSH 验证；
- [x] README 不必暴露低层实现；
- [x] Release Note 说明 Native No-overwrite 收口。

---

# 12. 人工验收建议

## 12.1 Paramiko

```bash
git-deploy prod --remote-plan
git-deploy prod --yes
```

可以开始受控使用。

## 12.2 Native OpenSSH

在 v1.4.3 前：

```text
保持单发布器
部署执行期间不要人工/面板/FTP修改 Hybrid 受管直接路径
```

此时 v1.4.2 可正常使用。

不要依赖：

```text
Native 最终 Rename 对最后一刻出现目标的绝对 No-overwrite
```

## 12.3 Recovery

```bash
git-deploy prod --recover
```

已经可以在以下情况下独立工作：

- Build 失败；
- `.deploy/` 缺失；
- Dirty Worktree；
- Existing State 损坏；
- 当前 Local Hybrid 不可扫描。

## 12.4 Stage Race

可人工验证：

1. 准备一个较大 `assets/`；
2. 开始部署；
3. Stage 上传期间创建一个计划时 Missing 的 Root File；
4. 应在 Secondary Freshness 失败；
5. External File 保留；
6. Online Existing Paths 不进入 Swap。

---

# 13. 最终结论

v1.4.2 已经有效关闭 v1.4.1 报告中的全部架构和状态机问题：

```text
Secondary Freshness
Expected Type
Root File Stage
Ownership Precommit
Recovery-only Prepare
Schema-1 Fail Closed
Recovery-only View
```

整体实现已经接近稳定。

但 Native OpenSSH 后端仍使用：

```text
lstat + default sftp rename
```

默认 OpenSSH Rename 可能选择：

```text
posix-rename@openssh.com
```

因此无法提供原子 No-overwrite。

综合结论：

```text
Paramiko Hybrid：
    通过

Native OpenSSH Hybrid：
    有条件通过
    单发布器环境可用
    最后一刻并发目标保护尚未完全成立

整体 v1.4.2：
    有条件通过
```

建议不要再扩展新功能，直接发布一个极小的：

```text
v1.4.3
Native OpenSSH rename -l safety closeout
```

修复完成后，Hybrid v1.4 系列即可进入稳定维护阶段。
