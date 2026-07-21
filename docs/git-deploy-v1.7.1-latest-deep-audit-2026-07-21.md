# git-deploy v1.7.1 最新代码深度审计报告

> 仓库：`howjc/git-deploy`  
> 分支：`main`  
> 最新提交：`fc447a15d50a228db11a39b34b9dcae1bfdab8aa`  
> 功能修复提交：`d783cdfef018efcc4d626522d345e40b500cb28c`  
> 上一审计基线：`v1.7.0 / 9b00e93fbe8b8ab219b22d606359ec8f765a04de`  
> 当前 Package Version：`1.7.1`  
> 审计日期：`2026-07-21`  
> 代码审计结论：**通过**  
> Release Gate：**未闭合——Tag 与 CI 证据缺失**  
> 综合结论：**有条件通过**

---

# 1. 执行摘要

v1.7.1 针对 v1.7.0 审计提出的四项核心整改均已完成：

1. Unknown Target Filter 在任何远端连接前拒绝；
2. Project 非 Git 不再退化创建伪 `.git`；
3. Workspace 单仓 Config/Git 失败转换成独立 FAIL 行；
4. Factory/Connect 异常纳入单 Target 失败隔离；
5. Transport Close 被保护；
6. Target Lock 在正常 Factory/Connect/Probe 失败路径中保证释放；
7. READY 项不再直接相信 Preflight Snapshot；
8. READY 在执行阶段获得 Target Lock；
9. READY 重新连接当前 FTP Server；
10. READY 重新计算 Banner Hash；
11. READY 重新 Inspect Capability Profile；
12. READY 重新读取 Pending；
13. `--force` 与 REPROBE 不绕过 Pending；
14. Invalid Profile 的 Plan 明确说明 Pending 在执行阶段检查；
15. Workspace 后续仓库在前一个仓库失败时继续；
16. 没有修改 Planner、Deployer、Ownership、Pending、Recovery 或 State Schema。

本轮结果：

```text
P0：无
P1：无
P2：2 项
P3：1 项
```

代码层面可以作为当前稳定基线。

剩余两个 P2：

1. Target Lock 自身的底层 I/O 异常仍可能逃逸并中止 Batch；
2. Bootstrap Plan/Summary 输出不是 Fail-open，输出异常可能在远端初始化成功后改变命令表象。

二者不影响 FTP Hybrid 内容安全、Ownership、Pending 或 State，不建议为此扩大版本范围。

发布完整性方面：

- `pyproject.toml` 与 `__version__` 均为 `1.7.1`；
- 当前 GitHub Connector 无法解析 `v1.7.1` 或 `1.7.1` Tag；
- Release Commit 没有关联 Workflow Run；
- Combined Status 为空；
- 当前审计环境仍无法解析 `github.com`，无法独立 Clone 和执行测试。

因此：

```text
Source / Architecture / Safety：
    通过

Bootstrap Success Contract：
    通过

Release CI / Tag：
    未闭合

综合：
    有条件通过
```

---

# 2. 版本与变更范围

## 2.1 最新 Main

```text
fc447a15d50a228db11a39b34b9dcae1bfdab8aa
release v1.7.1: close Bootstrap filter, git, and freshness gaps
```

## 2.2 功能修复提交

```text
d783cdfef018efcc4d626522d345e40b500cb28c
fix(bootstrap): tighten filter, git, lock isolation, and READY freshness
```

## 2.3 与 v1.7.0 的差异

共 2 个提交，主要修改：

```text
src/git_deploy/bootstrap.py
tests/test_bootstrap.py
README.md
docs/release-notes-v1.7.1.md
pyproject.toml
src/git_deploy/__init__.py
uv.lock
```

没有修改：

```text
src/git_deploy/deployer.py
src/git_deploy/planner.py
src/git_deploy/transports/ftp.py
src/git_deploy/hybrid.py
FTP Hybrid Executor
SFTP Hybrid Executor
Ownership Schema
Pending Schema
Recovery Schema
State Schema
```

因此本轮风险集中在 Bootstrap 编排，不涉及业务部署协议。

---

# 3. Package 与发布状态

## 3.1 Package

```toml
[project]
version = "1.7.1"
```

## 3.2 Runtime Version

```python
__version__ = "1.7.1"
```

## 3.3 Tag

尝试读取：

```text
v1.7.1
1.7.1
```

均返回：

```text
No commit found for the ref
```

当前只能确认 Main 已更新为 1.7.1，不能确认正式 Tag 已创建。

## 3.4 CI

Release Commit：

```text
fc447a15...
```

返回：

```text
workflow_runs: []
combined statuses: []
```

不能声称：

```text
v1.7.1 CI Verified
```

## 3.5 独立复跑

尝试：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

结果：

```text
Could not resolve host: github.com
```

因此本轮无法独立执行：

```bash
uv lock --check
uv run --isolated --python 3.11 --all-groups pytest -q
uv run --isolated --python 3.12 --all-groups pytest -q
uv run ruff check src tests
uv run ty check src
uv build --clear
```

---

# 4. v1.7.0 P1：Unknown Target Filter

## 4.1 新验证模型

Project：

```python
collect_known_target_names(config)
```

Workspace：

```python
collect_workspace_known_target_names(workspace)
```

统一：

```python
validate_bootstrap_target_filter(
    requested_filters,
    known_targets,
)
```

## 4.2 行为

```bash
git-deploy bootstrap prdo --yes
```

现在返回：

```text
ConfigError:
unknown bootstrap target filter(s): prdo
```

混合合法与非法：

```bash
git-deploy bootstrap prod typo --yes
```

同样整体拒绝，不会只执行 `prod`。

## 4.3 连接顺序

Filter Validation 发生在：

```text
Candidate Preflight
FTP Transport Construction
FTP Connect
```

之前。

因此：

```text
Unknown Filter
    → Remote Connect = 0
    → Remote Mutation = 0
```

## 4.4 Workspace 语义

Target 名称只要存在于任意一个可加载 Repository Config，即视为 Known。

不存在于整个 Workspace：

```text
Fail Before Connect
```

## 4.5 测试

覆盖：

- 空 Filter；
- 合法 Filter；
- 单个拼写错误；
- 合法 + 非法混合；
- Project Zero Connect；
- Workspace Unknown Filter。

## 4.6 结论

> **上一轮 P1 已关闭。**

---

# 5. v1.7.0 P2：Git Repository Gate

## 5.1 删除伪 `.git` Fallback

v1.7.0：

```python
try:
    git_dir = repository.common_dir()
except PlanError:
    git_dir = project_root / ".git"
```

v1.7.1：

```python
repository.validate()
git_dir = repository.common_dir()
```

失败时：

```text
git_error = ...
state_base = .git-deploy-invalid-worktree
action = FAIL_PRECHECK
```

该占位路径不会被创建或用于锁。

## 5.2 Project 非 Git

行为：

```text
FAIL_PRECHECK
Remote Connect = 0
Remote Probe = 0
.git 不创建
Exit != 0
```

## 5.3 Workspace 非 Git

一个 Repository 非 Git：

```text
broken/prod
    → FAIL_PRECHECK

frontend/prod
    → 正常 PROBE / READY
```

最终：

```text
至少一个失败
    → Exit 1

成功仓库
    → Profile 保留
```

## 5.4 Workspace Config 错误

单仓 Config 无法加载时生成：

```text
repository/<config>
FAIL_PRECHECK
```

不会中止后续 Repository 枚举。

## 5.5 测试

覆盖：

- Project Non-Git；
- 不创建 `.git`；
- 不创建 Invalid Placeholder；
- Zero Connect；
- Workspace One Non-Git；
- Healthy Repository Continues；
- Overall Non-zero。

## 5.6 结论

> **上一轮 P2 已关闭。**

---

# 6. v1.7.0 P2：Factory / Lock 异常隔离

## 6.1 Preflight

Transport Factory 已进入：

```python
try:
    built = factory(resolved)
    ...
except Exception:
    FAIL_PRECHECK
finally:
    transport.close()
```

Factory 构造失败不再终止整个 Preflight。

## 6.2 Execute

执行阶段：

```text
Acquire Target Lock
    ↓
Factory
    ↓
Connect
    ↓
READY Verify / Pending / Probe
    ↓
Transport Close
    ↓
Lock Release
```

Factory、Connect、Probe 的异常转换成：

```python
BootstrapResult(
    success=False,
    error=...
)
```

## 6.3 Close

Transport Close 被额外保护：

```python
try:
    transport.close()
except Exception:
    pass
```

不会覆盖主要 Result。

## 6.4 Batch Continue

`execute_bootstrap()` 仍按顺序执行全部 Item。

针对测试：

```text
prod factory failure
staging success
```

结果：

```text
prod      FAIL
staging   READY
Exit      1
```

并验证 `prod` Lock 可以立即重新获取。

## 6.5 结论

> **上一轮主要异常隔离问题已关闭。**

---

# 7. v1.7.0 P2：READY 最终 Freshness

## 7.1 READY 不再直接返回

v1.7.0：

```python
if READY:
    return success
```

v1.7.1：

```text
Acquire Lock
Connect
_verify_ready_item()
```

## 7.2 Final Profile Inspect

执行时重新：

```text
Server Banner Hash
Capability Profile Read
Schema
Target Fingerprint
Banner Binding
Required Feature Set
```

只要不再 VALID：

```text
FAIL:
profile no longer valid after plan confirmation
```

不会自动变成 Probe。

这是正确的：

- 用户确认的是 READY/no mutation；
- 最终变成 PROBE 需要远端写；
- 不能在没有重新确认的情况下自动升级动作。

## 7.3 Final Pending Check

Profile 仍 VALID 后再次读取 Pending。

如果确认窗口中出现：

```text
PREPARED
FILES_PUBLISHED
PRUNED
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

则 FAIL。

## 7.4 `--force`

Plan：

```text
REPROBE
forced reprobe; pending checked at execution
```

执行：

```text
先 Pending
后 Probe
```

因此 `--force` 不是 Pending Bypass。

## 7.5 Invalid Profile

Old/Corrupt/Banner Drift 等 REPROBE Plan 同样标注：

```text
pending checked at execution
```

## 7.6 测试

覆盖：

- READY Profile Deleted After Plan；
- READY Pending Appears After Plan；
- Force REPROBE Pending；
- Probe Count = 0 Under Pending；
- Exit Non-zero。

## 7.7 结论

> **上一轮 P2 已关闭。**

---

# 8. 状态机与远端安全回归

v1.7.1 没有修改 Deploy Executor。

Bootstrap 仍不执行：

- Build；
- Freeze；
- Source Upload；
- Incremental Upload；
- Hybrid Business Upload；
- Adoption；
- Ownership；
- Pending Write；
- State Write；
- Prune；
- after_deploy。

Bootstrap 的远端写入仍限于：

```text
Configured Remote Root（仅 CREATE ROOT）
.git-deploy/ftp-probe/<uuid>
```

Capability Profile 仍在 Probe 全部成功后原子写入本地。

没有发现：

- Unknown Remote Root 删除；
- Mirror Orphan 删除；
- Ownership 越权；
- Pending 覆盖；
- State 提交；
- Recovery Phase 改写；
- after_deploy 执行。

---

# 9. P2：Target Lock 底层 I/O 异常仍可能中止 Batch

## 9.1 当前捕获

执行阶段只对：

```python
lock.acquire()
```

捕获：

```text
PlanError
```

主要覆盖：

```text
Lock 已被其他进程持有
```

## 9.2 未捕获路径

`TargetLock.acquire()` 还执行：

```text
mkdir
open
flock
truncate
json write
flush
fsync
```

这些可能抛出：

```text
PermissionError
OSError
ENOSPC
Read-only filesystem
I/O error
```

`TargetLock.release()` 执行：

```text
seek
truncate
flush
flock unlock
close
```

同样可能抛出 OSError。

## 9.3 Batch 影响

`execute_bootstrap()` 当前为直接 Tuple Comprehension。

如果 `execute_bootstrap_item()` 因 Lock I/O 抛出：

- 当前 Item 没有 Result；
- 后续 Target 不执行；
- Summary 不输出；
- Best-effort Contract 被破坏。

如果 Release 在 Profile 保存后抛出：

- Profile 已成功；
- 命令可能异常退出；
- 用户无法从 Summary 得知实际结果。

## 9.4 安全影响

Acquire 失败发生在远端连接前：

```text
Remote Mutation = 0
```

Release 失败时 OS Close 最终通常仍会释放 Advisory Lock，但命令表象不稳定。

不影响：

- Ownership；
- Pending；
- State；
- 业务文件。

因此定级：

```text
P2
```

## 9.5 建议

最小收口：

```python
def execute_bootstrap(items, ...):
    results = []
    for item in items:
        try:
            result = execute_bootstrap_item(...)
        except Exception as exc:
            result = BootstrapResult(item, False, None, str(exc))
        results.append(result)
    return tuple(results)
```

同时：

```python
try:
    lock.acquire()
except Exception as exc:
    return BootstrapResult(...)
```

Release 使用：

```python
try:
    lock.release()
except Exception as exc:
    # Preserve failure/result information; do not abort later targets.
```

不捕获：

```text
KeyboardInterrupt
SystemExit
```

---

# 10. P2：Bootstrap 输出不是 Fail-open

## 10.1 当前顺序

```text
Print Plan
Confirm
Execute All Targets
Print Blank Line
Print Summary
Return Exit Code
```

## 10.2 问题

如果执行完成后：

```text
stdout closed
BrokenPipeError
UnicodeEncodeError
OSError
```

发生在 Summary：

- Capability Profile 可能已经保存；
- Remote Root 可能已经创建；
- Probe 已经完成；
- CLI 可能异常退出；
- 调用方认为失败或获得 Traceback。

CLI 顶层只捕获：

```text
GitDeployError
KeyboardInterrupt
```

不捕获一般输出 OSError。

## 10.3 与部署安全的关系

这不会改变远端内容或触发 Retry。

但会产生：

```text
操作成功
命令表象失败
```

属于观测/退出语义问题。

定级：

```text
P2
```

## 10.4 建议

为 Bootstrap 使用轻量 Safe Printer：

```python
class SafePrinter:
    disabled = False

    def print(...):
        if disabled:
            return
        try:
            print(...)
        except (BrokenPipeError, OSError, UnicodeError, ValueError):
            disabled = True
```

Plan Print 失败时是否继续：

- TTY Interactive：建议停止并拒绝确认，因为用户看不到 Plan；
- `--yes` Non-interactive：可以继续，但最终 Exit Code 不应被 Summary 输出改变。

更简单的首版：

```text
Plan 输出失败
    → Fail Before Mutation

Summary 输出失败
    → Preserve Computed Bootstrap Exit Code
```

---

# 11. P3：Workspace Target Discovery 双重加载

Workspace 当前：

1. 为 Filter Validation 加载全部 Repository Config；
2. 为 Candidate Enumeration 再加载一次。

影响：

- 少量重复 TOML I/O；
- 配置在两个阶段之间变化时，Known Target Snapshot 与 Candidate Snapshot 可能不一致；
- Broken Config 中的 Target 名不可用于 Known Target Validation。

安全影响很低。

可选优化：

```python
LoadedWorkspaceRepository:
    config | config_error
```

一次加载，同时用于：

- Known Target Union；
- Candidate Enumeration；
- FAIL Row。

定级：

```text
P3
```

不需要单独版本。

---

# 12. 测试覆盖评价

## 12.1 新增覆盖良好

- Project Unknown Filter；
- Mixed Valid + Unknown Filter；
- Zero Remote Connect；
- Workspace Unknown Filter；
- Project Non-Git；
- No Fake `.git`；
- Workspace Non-Git Continue；
- Overall Non-zero；
- Factory Exception；
- Later Target Continues；
- Lock Reacquire；
- READY Profile Deleted；
- READY Pending Appears；
- Force Pending；
- Zero Probe Under Pending。

## 12.2 仍缺

- Lock Parent `mkdir` PermissionError；
- Lock File `open` OSError；
- `fsync` ENOSPC；
- Lock Release OSError；
- Outer Batch Continue on unexpected `execute_bootstrap_item` exception；
- Plan Print BrokenPipe；
- Summary Print BrokenPipe；
- Workspace Config changes between Target Discovery and Enumeration；
- Actual CI Python 3.11 / 3.12；
- Official Tag verification。

---

# 13. Release Gate 收口

建议不要为两个 P2 立即发布 v1.7.2。

先完成发布基础设施：

1. 创建 `v1.7.1` Tag；
2. Tag 指向：

```text
fc447a15d50a228db11a39b34b9dcae1bfdab8aa
```

3. Python 3.11 CI；
4. Python 3.12 CI；
5. `uv lock --check`；
6. Full Pytest；
7. Ruff；
8. ty；
9. Build Wheel/Sdist；
10. Isolated Wheel Smoke；
11. `git-deploy --version`；
12. `git-deploy bootstrap --help`；
13. Project Bootstrap Smoke；
14. Workspace Bootstrap Smoke。

如果 Actions 仍不可用，至少在本地保存：

```text
Python version
Test count
Ruff result
ty result
Build artifact hashes
Wheel smoke result
```

但本地记录仍不能完全替代独立 CI。

---

# 14. 人工验收建议

## 14.1 Unknown Filter

```bash
git-deploy bootstrap prdo --yes
```

预期：

```text
Exit != 0
Remote Connect = 0
```

## 14.2 Mixed Filter

```bash
git-deploy bootstrap prod typo --yes
```

预期：

```text
整体拒绝
prod 不执行
```

## 14.3 Project Non-Git

在非 Git 目录运行：

```bash
git-deploy bootstrap --yes
```

预期：

```text
FAIL
不创建 .git
不连接 FTP
```

## 14.4 Workspace Bad Repo

一个 Repository 非 Git，一个正常：

```bash
git-deploy bootstrap --workspace deploy.workspace.toml --yes
```

预期：

```text
bad/prod     FAIL
good/prod    READY
Exit         1
```

## 14.5 READY Drift

Plan 后删除 Capability Profile，再执行：

```text
FAIL
不自动 Probe
```

## 14.6 Pending Drift

Plan 后制造 PREPARED Pending：

```text
FAIL
Probe Count = 0
```

## 14.7 Force Pending

```bash
git-deploy bootstrap --force --yes
```

存在 Pending：

```text
FAIL
Probe Count = 0
```

## 14.8 Idempotence

连续两次：

```bash
git-deploy bootstrap --yes
git-deploy bootstrap --yes
```

第二次：

```text
READY
Remote Probe = 0
```

---

# 15. 当前使用建议

v1.7.1 可以用于：

```bash
git-deploy bootstrap --yes
git-deploy bootstrap --force --yes
git-deploy bootstrap prod staging --yes
```

推荐流程：

```bash
git-deploy bootstrap --yes
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

当前应注意：

- Workspace 某个 Git Common Dir 不可写时，底层 Lock I/O 可能中止批次；
- 不建议把 Bootstrap 输出管道截断到 `head`；
- 创建 Root 后 Probe 失败，Root 会保留；
- Bootstrap 不等于首次 Adoption；
- 首次业务部署仍需 `--full` 审阅。

---

# 16. 最终结论

v1.7.1 已经正确关闭 v1.7.0 的核心问题：

```text
Unknown Filter Success Contract
Git Repository Gate
Workspace Failure Isolation
Factory/Connect Lock Isolation
READY Final Freshness
Force/Pending Safety
```

这些修复没有侵入 FTP Hybrid 部署状态机，没有改变 Ownership、Pending、State 或远端删除规则。

因此：

> **v1.7.1 代码审计通过。**

剩余问题集中在：

```text
Lock 底层 I/O 极端异常
CLI 输出异常
```

均为 P2，不阻断个人使用场景。

但正式发布证据仍不完整：

```text
v1.7.1 Tag 未解析
CI Run 缺失
独立测试无法复跑
```

综合结论：

> **v1.7.1 有条件通过：代码可以作为稳定基线；创建 Tag 并完成 CI 后可正式关闭本阶段。**

版本建议：

> **不需要立即发布 v1.7.2。将两个 P2 放入后续维护清单，优先恢复 Tag 与 CI，并开始真实 Project/Workspace FTP Hybrid Canary。**
