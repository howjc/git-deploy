# git-deploy v1.4.1 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`48e87112cb6e2cc7e0504c1aaa9b05ca8bf2cfcc`
> PR：`#12 release v1.4.1: harden hybrid safety`
> PR Head：`fe4260096eab6ba25682154dbfdf8ddad12a426e`
> 版本：`v1.4.1`
> 审计日期：`2026-07-18`
> 审计结论：**有条件通过**
> 使用前提：**单发布器、部署执行期间不通过其他方式修改 Hybrid 当前/历史受管直接路径**

> v1.4.2 整改状态（2026-07-18）：本报告第 12 节建议已全部实现并具备对应自动测试；实现与兼容性说明见 `docs/release-notes-v1.4.2.md`。

---

## 1. 执行摘要

v1.4.1 是一次针对 v1.4.0 深度审计结果的安全收口版本。

上一轮发现的七项主要问题已经全部得到对应修复：

1. Remote Plan 与确认窗口之间的 Ownership/Path Type 漂移；
2. Recovery 在用户确认前自动修改远端；
3. Delete-only / Ownership-only 场景丢失待重试命令；
4. Hybrid Local Root 可以配置为项目根目录；
5. `.git/.deploy` 缺少强制保护；
6. 必要 Backup 缺失时 Recovery 静默推进；
7. 路径名不稳定与嵌套空目录丢失。

本轮确认：

- Remote Plan 冻结 Ownership 原始字节 Hash；
- 冻结当前与历史全部 Hybrid 直接路径类型；
- 正式执行前进行零写入 Freshness Gate；
- Workspace 在第一仓写入前验证全部选中仓库；
- Recovery 变成只读规划 + 显式 `--recover`；
- Recovery Schema 2 记录逐路径交换进度；
- Recovery 绑定 `after_deploy + command_timeout` 指纹；
- Command、State、Cleanup 阶段能够分别续接；
- 必要 Backup 缺失时 Fail Closed；
- `local = "."` 和 resolve 后项目根别名被拒绝；
- `.git/**`、`.deploy/**`、`.git-deploy/**` 受到双重保护；
- Native/Paramiko 统一路径组件边界；
- Mirror 能保留嵌套空目录；
- 真实 Paramiko 和 Native OpenSSH 集成测试覆盖 Stale Plan。

在项目明确的个人、单发布器定位下，v1.4.1 已经可以进入受控日常使用。

不过本轮发现一个仍值得在 v1.4.2 收口的 P1：

> Freshness Gate 只在执行开始时运行一次。之后可能先执行普通 Source/Incremental 操作、上传完整 Mirror Stage，最后才开始 Swap。如果同名未知路径在这个阶段内出现，当前 `_backup_current()` 仍会把它移动到 Backup，并在成功清理时永久删除。

这个问题要求“部署已经开始后仍有另一个写入者”才能触发，属于当前文档明确不支持的多发布器/并发外部修改范围，因此本报告不将其继续定为单发布器场景下的 P0。

但如果未来希望声明：

```text
即使部署执行期间发生外部修改，也绝不接管未知路径
```

则仍需增加执行中期的第二次 Freshness Gate 和逐路径写入前置条件。

---

# 2. 审计方式与限制

本轮通过 GitHub Connector 检查：

- 最新 Main Commit；
- PR #12；
- v1.4.0 → v1.4.1 Commit Diff；
- PR Review Threads；
- GitHub Actions；
- Python 3.11 / Python 3.12 Job；
- Main 与 `v1.4.1` Tag；
- Config；
- Planner；
- Deployer；
- Hybrid Recovery；
- CLI；
- Prepared Deployment；
- Workspace；
- Native OpenSSH；
- Paramiko；
- Doctor；
- 单元测试；
- Docker Integration Tests；
- Release Notes。

尝试独立 Clone：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

当前执行环境仍无法解析 `github.com`：

```text
Could not resolve host: github.com
```

因此无法在本轮独立复跑：

```bash
uv lock --check
uv run --isolated --python 3.11 --all-groups pytest -q
uv run --isolated --python 3.12 --all-groups pytest -q
uv run ruff check src tests examples
uv run ty check src
uv build --clear
```

动态验证依据为成功的 GitHub Actions 和仓库中的真实集成测试；代码结论来自当前 Main 的独立静态审计。

---

# 3. 版本、Tag 与 CI

## 3.1 Main

```text
48e87112cb6e2cc7e0504c1aaa9b05ca8bf2cfcc
Merge pull request #12 from howjc/agent/v1.4.1-hybrid-safety
```

功能提交：

```text
fe4260096eab6ba25682154dbfdf8ddad12a426e
release v1.4.1: harden hybrid safety
```

## 3.2 Package

Main：

```toml
[project]
name = "git-deploy"
version = "1.4.1"
```

Tag `v1.4.1`：

```toml
[project]
name = "git-deploy"
version = "1.4.1"
```

Main 与 Tag 的 `pyproject.toml` Blob 一致。

核心 `hybrid.py` Main/Tag Blob 也一致。

## 3.3 CI

PR Head：

```text
status: completed
conclusion: success
```

Python 3.11 与 Python 3.12 均通过：

- Interpreter Matrix；
- Lockfile Check；
- Dependency Install；
- Tests；
- Ruff；
- ty；
- Wheel/sdist Build；
- Isolated Wheel Install；
- CLI Version/Help Smoke。

PR 描述记录：

```text
220 tests
```

PR #12 没有未解决 Review Thread。

---

# 4. v1.4.0 审计问题关闭情况

---

## 4.1 P0：确认窗口 Adoption 绕过

### v1.4.0 问题

Remote Plan 认为路径 Missing，但用户确认前路径被外部创建，执行时会直接 Backup、替换和删除。

### v1.4.1 实现

HybridPlan 新增：

```python
expected_ownership_hash
expected_path_types
recovery_records
```

Remote Plan 冻结：

```text
Ownership Raw Bytes SHA256
Current ∪ Historical Direct Path Type
Recovery Records
```

执行前调用：

```python
validate_remote_freshness(...)
```

重新读取：

- Recovery Records；
- Ownership Raw Hash；
- 每个直接路径类型。

任一变化抛出：

```python
StaleRemotePlanError
```

### Workspace

Workspace 执行前先对所有选中仓库调用 Freshness Validation。

如果后面的仓库已 Stale：

```text
前面的仓库也不会开始写入
```

### 测试

覆盖：

- Missing → File；
- Missing → Directory；
- Owned File → Directory；
- Owned Directory → File；
- Ownership Manifest 改变；
- Source Operation 在 Stale 时零写入；
- Workspace 后一仓 Stale 时全部零写入；
- Real Paramiko；
- Real Native OpenSSH。

### 结论

**关闭。**

---

## 4.2 P1：Recovery 在确认前自动修改远端

### v1.4.0 问题

普通 Remote Plan 会自动调用 Recovery，用户即使取消，远端也已经被恢复或清理。

### v1.4.1 实现

`complete_remote_plan()`：

```text
只读取 Recovery
只渲染 Recovery
不执行 Recovery
```

新增：

```bash
git-deploy prod --recover
```

流程：

```text
Read-only Plan
    ↓
Render Recovery Action
    ↓
Confirm
    ↓
Freshness Gate
    ↓
Recovery Only
    ↓
Exit
```

普通部署发现 Recovery：

```text
拒绝继续
要求单独 --recover
```

Recovery 完成后必须重新运行：

```bash
git-deploy prod --remote-plan
git-deploy prod --yes
```

### 测试

覆盖：

- Remote Plan 显示 Recovery；
- Remote Plan 零写入；
- 用户拒绝 Recovery 确认时零写入；
- Recovery 完成后普通部署重新规划。

### 结论

**关闭。**

---

## 4.3 P1：Delete-only 命令失败后丢失待重试命令

### v1.4.0 问题

Ownership 已提交但命令失败；如果下一次没有文件工作，Plan 可能 No-op，命令不再执行。

### v1.4.1 实现

Recovery Schema 2 记录：

```text
OWNERSHIP_COMMITTED
COMMANDS_COMPLETE
STATE_COMPLETE
CLEANUP_COMPLETE
```

并绑定：

```text
after_deploy Commands
command_timeout
```

的 SHA256 指纹。

Recovery Outcome 明确区分：

```python
commands_pending
state_pending
cleanup_pending
```

恢复时：

```text
Commands Pending
    → 重新执行命令
    → 写 COMMANDS_COMPLETE

State Pending
    → 保存保守 State
    → 写 STATE_COMPLETE

Cleanup Pending
    → 清理 Stage/Backup/Recovery
```

### 测试

覆盖：

- 删除最后一个 Root File；
- 删除最后一个 Mirror Directory；
- Ownership-only；
- Command Failure；
- State Failure；
- Cleanup Failure；
- Commands 不被重复；
- Local HEAD 在中断后继续移动时 State 使用中断部署 Commit。

### 结论

**关闭。**

---

## 4.4 P1：Hybrid Local Root 等于 Project Root

### v1.4.0 问题

```toml
local = "."
```

可能让 Hybrid 扫描源码、`.git/` 和其他项目文件。

### v1.4.1 实现

配置解析后判断：

```python
if output.mode == "hybrid" and output.local == project_root:
    raise ConfigError
```

由于 Local 已 resolve，因此同时阻止：

```text
项目根目录的符号链接别名
```

### 结论

**关闭。**

---

## 4.5 P1：`.git/.deploy` 强保护缺失

### v1.4.1 实现

Mandatory Protect 增加：

```text
.git/**
.deploy/**
.git-deploy/**
```

Hybrid Direct Child 再次显式拒绝：

```text
.git
.deploy
.git-deploy
```

属于双重门禁。

### 结论

**关闭。**

---

## 4.6 P2：必要 Backup 缺失时静默清理 Recovery

### v1.4.1 实现

Recovery Schema 2 增加：

```python
completed_names
active_name
```

每个路径开始修改前，先原子写入 Recovery Progress。

如果：

```text
原路径此前存在
路径修改已经开始
必要 Backup 缺失
```

则：

```text
Fail Closed
保留 Recovery
保留 Stage
保留 Backup Root
Doctor 显示 Manual Inspection Required
```

旧 Schema 1 Recovery 无法证明新路径状态时也会保守拒绝。

### 结论

**关闭。**

---

## 4.7 P2：路径名不稳定和嵌套空目录

### 路径名

统一函数：

```python
is_stable_remote_component()
```

拒绝：

- 首尾空格；
- Tab；
- 控制字符；
- DEL；
- 不可见空白；
- `/`；
- `\`；
- `.` / `..`。

应用于：

- Local Hybrid；
- Ownership Parser；
- Recovery Parser；
- Paramiko `list_directory`；
- Native OpenSSH `list_directory`；
- 聚合参考脚本。

### 空目录

`HybridDirectoryManifest` 新增：

```python
directories: tuple[str, ...]
```

Stage 先按深度创建全部嵌套目录，再上传文件。

### 结论

**关闭。**

---

# 5. P1：Freshness Gate 之后仍存在二次 TOCTOU 窗口

## 5.1 定级

```text
单发布器、执行期间无外部修改：
    不构成阻断

多设备并发、手工修改或其他发布器：
    可能删除新出现的未知同名路径
```

因此本报告定为：

```text
P1
```

如果未来产品要声明“执行期间也抵抗外部并发修改”，则应提升为 P0。

## 5.2 当前执行顺序

```text
validate_remote_freshness
    ↓
ensure_root
    ↓
普通 Source / Incremental Operations
    ↓
创建 Recovery
    ↓
完整上传 Mirror Stage
    ↓
SWAPPING
    ↓
_backup_current
    ↓
Stage → Final
```

Freshness 只在最开始执行一次。

## 5.3 可复现流程

Remote Plan：

```text
assets/ = Missing
无需 Adoption
```

正式执行：

```text
1. Freshness Gate 检查 assets 仍为 Missing
2. 开始上传一个较大的 Stage/assets
3. 上传期间人工或另一个发布器创建：
       assets/important-data
4. Stage 完成
5. _backup_current() 再次 lstat assets
6. 因为现在存在，直接移动到 Backup
7. Stage/assets → assets
8. Ownership Commit
9. Cleanup 删除 Backup
```

结果：

```text
important-data 被永久删除
```

## 5.4 代码原因

执行前检查与真正路径替换之间存在：

- 普通文件操作；
- Recovery Metadata 写入；
- Stage Directory 创建；
- 全量 Stage Upload；
- 网络 Retry/Reconnect。

`old_existing_names` 还会在执行期间重新根据当前远端状态计算。

如果原本 Missing 的路径此时出现：

```text
它会被当成“部署开始前已存在”
```

但不会重新要求 Adoption。

`_backup_current()` 的语义是：

```text
当前存在
    → 备份
```

没有接收 Remote Plan 中的：

```text
expected_type
adopted
```

作为前置条件。

## 5.5 最小修复

不建议引入完整远端事务。

v1.4.2 只需增加：

### A. Stage 后二次 Freshness Gate

```text
所有 Stage 上传完成
    ↓
开始 SWAPPING 前
    ↓
再次验证 Ownership Hash + Path Types
```

如果变化：

```text
保留在线路径
清理或 Recovery Stage
停止 Swap
```

### B. `_backup_current()` 接收 Expected Type

```python
_backup_current(
    name,
    expected_type,
    allow_adoption,
)
```

规则：

```text
Expected Missing
    → 当前必须仍然 Missing
    → 不允许 Backup 已出现路径

Expected File
    → 当前必须仍然 File

Expected Directory
    → 当前必须仍然 Directory
```

### C. Missing 路径使用 No-overwrite Publish

对于 Remote Plan 时 Missing 的 Root File / Directory：

```text
不得调用“存在就 Backup”的通用替换
```

应该：

```text
Stage
    → rename_path(no overwrite)
```

目标在最后一刻出现时 Rename 必须失败。

### D. Ownership Commit 前复核 Hash

在写入新 Ownership 前，再确认：

```text
Remote Ownership 仍等于 approved old hash
```

### E. 可选 Remote Lease

只有真正需要多个设备同时发布时，再增加：

```text
.git-deploy/lock/<mapping>
```

的远端原子租约。

当前个人单发布器版本不必立即实现。

---

# 6. P2：`--recover` 仍依赖当前 Build、State 和 Local Hybrid

## 6.1 当前流程

CLI 即使收到：

```bash
git-deploy prod --recover
```

也会先调用普通：

```python
prepare_project()
```

该流程会：

1. 校验 Git；
2. 读取现有 Local State；
3. 检查 Dirty Worktree；
4. 默认运行 Build；
5. 扫描当前 Hybrid Local Root；
6. 创建当前 Deployment Plan；
7. 冻结当前上传字节；
8. 检查临时磁盘。

之后才连接远端读取 Recovery。

## 6.2 影响

远端急需恢复时，以下任一问题都会阻止 Recovery：

- 当前 Build 已损坏；
- Node/Composer 依赖暂时不可用；
- `.deploy/frontend-root` 被清理；
- 当前聚合输出冲突；
- 当前 Worktree 不满足 Clean Policy；
- Local State 损坏；
- 临时磁盘不足；
- 当前源码已经变更并产生新 Plan Error。

尤其 Local State 损坏时：

```text
--recover 禁止 --full
StateError 在 Remote Recovery 前直接失败
```

## 6.3 为什么 Recovery 不需要这些内容

Pre-ownership Restore 只需要：

- Config；
- Project ID；
- Target；
- Target Fingerprint；
- Target Lock；
- Remote Recovery Record；
- Remote Stage/Backup。

Committed Recovery 额外需要：

- 当前 `after_deploy` 与 Timeout，用于 Command Hash；
- StateStore 路径；
- Remote Ownership Last Commit。

它不需要：

- 当前 Build；
- 当前 Local Hybrid；
- 当前 Source Plan；
- 冻结上传字节；
- 当前 Output Manifest。

## 6.4 建议

增加独立：

```python
prepare_recovery()
```

流程：

```text
Load Config
Resolve Target
Acquire Target Lock
Resolve StateStore Path
Connect
Read Ownership / Recovery
Validate Command Hash
Render Recovery-only Plan
Confirm
Execute Recovery
```

不执行：

```text
Build
Local Hybrid Scan
Source Diff
Output Scan
Freeze
Temp Disk Check
Existing State Parse
```

这样 Recovery 才真正能够作为故障处理入口。

---

# 7. P2：Schema-1 Pending Command 无法绑定原命令契约

## 7.1 当前实现

命令漂移校验仅在：

```python
record.schema >= 2
```

时执行。

Schema 1 没有：

```text
command_hash
```

但如果旧记录的 Ownership 已提交、Phase 仍是：

```text
SWAPPING
OWNERSHIP_COMMITTED
```

Recovery 会判定：

```text
commands_pending = true
```

随后执行当前配置中的 `after_deploy`。

## 7.2 风险

无法判断：

- v1.4.0 中断部署原本有哪些命令；
- 命令是否已经修改；
- 原本无命令、现在新增命令；
- 原本有命令、现在删除命令；
- Timeout 是否变化。

所以 Schema-1 Pending Command 自动恢复不具备“用户审阅的原命令契约”。

## 7.3 建议

对 Schema 1：

```text
Pre-ownership Restore
    → 可以保守恢复

COMMANDS_COMPLETE / STATE_COMPLETE
    → 可以继续 State/Cleanup

Ownership 已提交且 Commands Pending
    → Fail Closed
    → Doctor 提示 Legacy Command Contract Unknown
```

可选提供显式人工开关：

```bash
git-deploy prod --recover --accept-current-commands
```

但为了保持极简，建议第一版直接要求人工处理旧记录。

---

# 8. P3：Recovery Plan 混入当前部署操作

## 8.1 当前行为

`prepare_project()` 先生成当前 Source/Output/Hybrid Plan。

发现 Recovery 后，`complete_remote_plan()` 将 Recovery 加到 HybridPlan，但保留：

```text
当前 plan.operations
当前 upload_count/delete_count
当前 after_deploy count
```

`--recover` 最终只执行 Recovery，不执行这些当前部署操作。

## 8.2 结果

确认界面可能显示：

```text
UPLOAD app.py
DELETE old-output.js
RECOVER [...]
3 after-deploy commands
```

但实际执行只恢复 Recovery。

这不会导致错误写入，但会让审阅内容与实际动作不完全一致。

## 8.3 建议

独立 Recovery Plan 后自然解决。

在此之前，Render 时 Recovery 存在应隐藏：

- 当前 Deployment Operations；
- 当前 Upload/Delete Summary；
- 非 Pending 的 Command Count。

只显示：

```text
Recovery Action
Pending Commands
Pending State
Pending Cleanup
```

---

# 9. P3：少量文档与 API 收口

## 9.1 Cleanup Warning

正常 Hybrid 部署 Cleanup 失败时仍打印：

```text
will resume next deployment
```

但 v1.4.1 的真实语义是：

```text
普通部署会拒绝
必须显式 --recover
```

应改为：

```text
cleanup is pending; review with --remote-plan and run --recover
```

## 9.2 `allow_recovery` 参数

`complete_remote_plan(..., allow_recovery=...)` 已经忽略该参数，仅为兼容保留。

内部调用点仍持续传入 True/False，增加认知成本。

建议 v1.5 或下一个 API 清理版本移除，不需要为此单独发布。

---

# 10. 非问题与明确边界

## 10.1 Root File 不做远端 Hash

Hybrid Root File 是否上传仍基于：

```text
本地成功 State Hash
Remote Path Type
```

不读取远端内容 Hash。

如果外部修改受管 Root File，但类型仍为 File：

```text
普通增量部署可能不覆盖
```

这是此前明确接受的设计，不是 v1.4.1 缺陷。

需要恢复时：

```bash
git-deploy prod --full
```

## 10.2 Mirror 每次执行

只要 Hybrid 中存在 Direct Mirror Directory：

```text
每次部署都会 Mirror
after_deploy 也会执行
```

这是目录强一致语义所需，不是 No-op 回归。

## 10.3 不支持多发布器

当前没有远端分布式 Lock。

支持：

```text
一个人
一个发布器
同一时间一个部署
```

不保证：

- 两台机器同时部署；
- CI 和本地同时部署；
- 手工发布器与 git-deploy 同时修改受管路径。

本地 Target Lock 只保护同一 Git Common Dir。

---

# 11. 建议版本规划

## v1.4.1

结论：

```text
可以作为单发布器 Hybrid 稳定基线
```

## v1.4.2

建议只做小型安全和恢复可用性收口：

1. Stage 后第二次 Freshness Gate；
2. Expected-type-aware `_backup_current`；
3. Missing Path No-overwrite Publish；
4. Ownership Commit 前 Hash Check；
5. Recovery-only Prepare；
6. Schema-1 Pending Command Fail Closed；
7. Recovery-only Plan Render；
8. Cleanup Warning 文案。

不新增：

- FTP Hybrid；
- 多 Hybrid 同根；
- Remote Full Reconcile；
- Release History；
- 通用 Rollback；
- Pipeline DSL；
- 全局 Hook。

---

# 12. v1.4.2 原子 TODO

## Freshness

- [x] 在所有 Stage 完成后重新调用 Freshness Gate；
- [x] Freshness 失败时在线 Final Path 零修改；
- [x] Freshness 失败时保留/清理内部 Stage 的规则明确；
- [x] `_backup_current` 接收 Expected Type；
- [x] Expected Missing 时新出现 Path 触发 Stale；
- [x] Expected File/Directory 类型变化触发 Stale；
- [x] Root File 使用 No-overwrite Staged Publish；
- [x] Ownership Commit 前重新核对 Old Hash；
- [x] Retry/Reconnect 后重新执行必要 Freshness。

## Recovery Prepare

- [x] 新增 `PreparedRecovery`；
- [x] 不读取 Existing State 内容；
- [x] 不运行 Build；
- [x] 不扫描 Local Hybrid；
- [x] 不生成 Source/Output Plan；
- [x] 不冻结上传字节；
- [x] 不做 Temp Disk Check；
- [x] 仍获取 Local Target Lock；
- [x] 仍冻结 Target/Command Contract；
- [x] Workspace 只准备存在 Recovery 的仓库。

## Legacy Recovery

- [x] Schema-1 Pre-commit Restore 保持支持；
- [x] Schema-1 Commands Pending Fail Closed；
- [x] Doctor 显示 Legacy Command Contract Unknown；
- [x] 不自动执行当前 Commands；
- [x] 增加迁移文档。

## UX

- [x] Recovery Render 隐藏当前部署 Operations；
- [x] Recovery Summary 只统计实际恢复动作；
- [x] Cleanup-only 不显示 Command Count；
- [x] Cleanup Warning 指向 `--recover`；
- [x] 移除内部 `allow_recovery` 参数。

---

# 13. 人工验收建议

## 13.1 首次 Adoption

```bash
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

确认：

- 只 Adoption 当前本地同名路径；
- `index.php`、`.env` 和未知目录不进入计划；
- Ownership 正确生成。

## 13.2 确认窗口 Stale

Remote Plan 后、确认前创建同名路径。

确认：

- Stale Plan；
- 零 Remote Mutation；
- 未知路径保留。

## 13.3 执行中不允许外部修改

当前 v1.4.1 使用期间：

```text
从确认开始直到命令结束
不得人工或通过另一个发布器修改 Hybrid 当前/历史直接路径
```

## 13.4 Recovery

```bash
git-deploy prod --remote-plan
git-deploy prod --recover
git-deploy prod --remote-plan
git-deploy prod --yes
```

确认：

- 普通部署不自动恢复；
- 取消 Recovery 零写入；
- Pending Command 正确续接；
- Cleanup 不重复命令。

## 13.5 State 丢失

删除本地 State 后：

- Root File Delete 仍来自 Remote Ownership；
- Mirror Directory 仍完整替换；
- 未知远端内容保留。

---

# 14. 最终结论

v1.4.1 对上一轮深度审计的响应是完整而有效的：

```text
Remote Freshness Snapshot
Explicit Recovery
Command/State/Cleanup Phases
Path Progress
Missing Backup Fail Closed
Local Root Protection
Stable Names
Nested Empty Directories
```

当前没有发现会在以下约束内阻止使用 Hybrid 的问题：

```text
单发布器
一个 Hybrid
部署执行期间无外部受管路径修改
```

因此：

> **v1.4.1 有条件通过，可以进入个人日常部署验证。**

但它还不能宣称：

> 部署正在执行时，仍能抵抗其他机器、人工操作或其他发布器对同名受管路径的并发修改。

若需要这一更强保证，应以 v1.4.2 增加 Stage 后二次 Freshness 与逐路径写入前置条件，而不必升级为完整分布式事务系统。
