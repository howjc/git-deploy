# git-deploy v1.4.0 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`48e912f6c7ab3777c0470caa6f4bffaf74b6a7de`
> PR：`#11 release v1.4.0: add safe hybrid output ownership`
> PR Head：`e4558b41f9bf3e94bccaf2f63f74b6ab7fe4a460`
> 版本：`v1.4.0`
> 审计日期：`2026-07-18`
> 审计结论：**不通过；Hybrid 暂不建议用于混合生产根目录**
> 建议动作：发布 `v1.4.1` 安全收口版本

> v1.4.1 整改状态（2026-07-18）：**已按本报告方案 A 完成安全收口并通过本地门禁**。实现包括执行前 Freshness 零写入门禁、显式 `--recover`、命令/State/Cleanup 阶段恢复、逐路径 Recovery 进度、Project Root 与保护路径拒绝、稳定路径名边界及嵌套空目录保留。下文原始 v1.4.0 结论保留作为审计基线；勾选项表示 v1.4.1 已实现并有自动测试覆盖。

---

## 1. 执行摘要

v1.4.0 已经完成此前规划的大部分核心能力：

- 本地聚合目录；
- 单个 SFTP Hybrid Output；
- Root File 增量上传；
- Direct Directory 每次完整 Mirror；
- Remote Ownership Manifest；
- State 丢失后仍可依据远端所有权删除旧前端文件；
- 显式 `--full` Adoption；
- `--dry-run` 与只读 `--remote-plan`；
- Stage / Backup / Swap；
- Recovery Record；
- Native OpenSSH 与 Paramiko；
- Workspace Root Ownership Gate；
- Doctor、迁移指南、聚合脚本和集成测试。

当前实现没有扫描或清理整个远端根目录，未知的 `index.php`、`.env`、后端目录和运行时目录在正常无竞态流程中能够保持不变。

但是，本轮发现一个发布阻断级问题：

> Remote Plan 与实际执行之间没有远端所有权新鲜度校验。
> 如果某个路径在 Remote Plan 时不存在、在确认窗口被人工或其他发布器创建，正式执行会直接将它备份、用 Hybrid 内容替换，并在清理阶段永久删除原内容，而不要求 `--full` Adoption。

这直接破坏了 v1.4.0 最核心的安全承诺：

```text
未知远端内容永远不处理
```

此外还存在：

1. Recovery 在用户确认前自动执行远端变更；
2. 命令失败后的“待重试命令”可能在删除-only 场景丢失；
3. Hybrid Local Root 可以错误配置为项目根目录；
4. Recovery 缺失必要 Backup 时可能静默丢弃恢复记录；
5. Native OpenSSH 的目录列表对首尾空格文件名不稳定；
6. 嵌套空目录不进入 Mirror Manifest。

因此，本轮结论为：

```text
Incremental Source / Output       继续可用
v1.3 after_deploy                 继续可用
v1.4 Hybrid                       暂停生产使用
```

---

# 2. 审计范围

本轮审计覆盖：

## 发布与版本

- Main 最新提交；
- PR #11；
- `v1.4.0` Tag；
- Main/Tag Package Version；
- GitHub Actions；
- Python 3.11/3.12；
- Ruff；
- ty；
- Wheel/sdist；
- Isolated Install。

## Config

- `project_id`；
- Output Name；
- `mode = "hybrid"`；
- 单 Hybrid；
- SFTP-only；
- `remote = "."`；
- `.deploy/**` Exclude；
- `.git-deploy/**` Protect；
- Git Ignore 检查；
- Source/Output/Hybrid Conflict。

## Local Hybrid

- Root File；
- Direct Mirror Directory；
- Recursive Hash；
- Symlink；
- Unsupported File Type；
- Frozen Bytes；
- Aggregation Example。

## Remote Ownership

- Ownership Schema；
- Project Identity；
- Mapping Identity；
- Bounded Read；
- Atomic Write；
- Adoption；
- Root File Delete；
- Directory Delete；
- State Loss；
- Unknown Remote Preservation。

## Recovery

- PREPARED；
- STAGED；
- SWAPPING；
- OWNERSHIP_COMMITTED；
- COMMANDS_COMPLETE；
- STATE_COMPLETE；
- CLEANUP_COMPLETE；
- Stage Failure；
- Swap Failure；
- Ownership Failure；
- Command Failure；
- State Failure；
- Cleanup Failure；
- Ctrl-C。

## Transports

- Paramiko `lstat`；
- Native OpenSSH `lstat`；
- Read/Write Metadata；
- List Directory；
- Rename；
- Recursive Delete；
- ControlMaster Reuse；
- Endpoint Pinning。

## Workspace

- Local Prepare All；
- Same/Nested Remote Root Gate；
- Combined Plan；
- Remote Plan；
- Shared Native Master；
- Sequential Execution；
- Partial Failure。

---

# 3. 审计方式与限制

通过 GitHub Connector 读取：

- Commit；
- PR Metadata 和 Diff；
- Main/Tag 文件；
- GitHub Actions Run；
- Job Steps；
- 核心源码；
- 单元测试；
- Docker Integration Tests；
- 文档和示例。

本地尝试执行：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

当前执行环境无法解析 `github.com`：

```text
Could not resolve host: github.com
```

因此本轮无法独立执行：

```bash
uv lock --check
uv run pytest -q
uv run ruff check src tests examples
uv run ty check src
uv build --clear
```

动态验证依据为 GitHub Actions 和仓库中的测试源码；安全结论来自独立静态审计。

---

# 4. 版本与 CI

## 4.1 Main

```text
48e912f6c7ab3777c0470caa6f4bffaf74b6a7de
Merge pull request #11 from howjc/agent/v1.4.0-hybrid-output
```

## 4.2 PR

```text
PR #11
release v1.4.0: add safe hybrid output ownership
head: e4558b41f9bf3e94bccaf2f63f74b6ab7fe4a460
merged: true
```

PR 描述记录：

```text
195 tests
Python 3.11
Python 3.12
Ruff
ty
lock check
wheel/sdist
isolated install
Docker Paramiko / Native OpenSSH integration
```

## 4.3 Version

Main：

```toml
version = "1.4.0"
```

Tag `v1.4.0`：

```toml
version = "1.4.0"
```

两者 `pyproject.toml` Blob SHA 一致。

## 4.4 GitHub Actions

PR Head CI：

```text
status: completed
conclusion: success
```

Python 3.11、3.12 均通过：

- Interpreter Check；
- Lockfile Check；
- Dependency Install；
- Tests；
- Ruff；
- ty；
- Package Build；
- Isolated Wheel Install；
- Version/Help Smoke。

---

# 5. 已正确实现的能力

## 5.1 Config 收口

已经实现：

```text
OutputMode = incremental | hybrid
```

Hybrid 强制要求：

- 显式 Name；
- `remote = "."`；
- 禁止显式 `delete_removed`；
- 单 Config 最多一个 Hybrid；
- 配置中不能同时保留 FTP Target；
- 必须有稳定 `project_id`。

旧 Incremental Output 默认语义保持兼容。

## 5.2 默认保护

默认 Source Exclude 增加：

```text
.deploy/**
.git-deploy/**
```

默认 Protect 增加：

```text
.git-deploy/**
```

Hybrid Local Root 未被 Git Ignore 时：

- 普通模式警告；
- `require_clean_worktree = true` 时阻断。

## 5.3 本地聚合脚本

参考脚本实现：

- 显式 Source；
- 显式 Destination；
- Destination 不能为 Project Root；
- Source/Destination 不能重叠；
- Source 之间不能重叠；
- Duplicate Path 拒绝；
- File/Directory Conflict 拒绝；
- Symlink 拒绝；
- 本地 Stage + Atomic Replace。

脚本本身设计合理。

## 5.4 Local Hybrid Scanner

正确区分：

```text
Direct Regular File
    → Hybrid Root File

Direct Directory
    → Hybrid Mirror Directory
```

拒绝：

- Local Root Symlink；
- Direct Symlink；
- Nested Symlink；
- Unsupported File Type；
- Unsafe Path Component。

所有上传字节在远端连接前冻结并重新校验 Hash。

## 5.5 Remote Ownership Manifest

Ownership Manifest 包含：

- Schema；
- Project ID；
- Mapping；
- Remote；
- Directories；
- Root Files；
- Last Commit；
- Updated Timestamp。

读取时校验：

- Size；
- UTF-8 JSON；
- Exact Fields；
- Schema；
- Project ID；
- Mapping；
- Remote；
- Sorted Unique Names；
- File/Directory Ownership Collision。

Manifest 路径：

```text
.git-deploy/hybrid/<mapping>.json
```

未知远端路径不进入 Manifest，也不进入删除集合。

## 5.6 Adoption

当前同名路径存在但不属于历史 Ownership 时：

```text
普通部署
    → 拒绝

--full
    → 显式 ADOPT
```

只接管本地当前存在的同名路径，不接管未知内容。

## 5.7 State 丢失恢复所有权

Hybrid 删除依据来自 Remote Ownership：

```text
历史 Ownership
-
当前 Local Hybrid
=
Root File / Directory Delete
```

因此删除本地 State 后，仍能删除历史拥有的旧 Root File 和旧 Mirror Directory。

## 5.8 Directory Stage / Swap

Mirror Directory：

```text
完整上传到 Stage
旧目录移动到 Backup
Stage Rename 到 Final
Ownership Commit
Cleanup Backup
```

Stage 上传失败不会替换在线旧目录。

Swap/Ownership 失败会留下 Recovery Record，供后续部署恢复。

## 5.9 Unknown Remote Preservation

测试覆盖：

- `index.php`；
- `.env`；
- `manual-backup/`。

这些未知内容在正常流程中不会被扫描或删除。

## 5.10 Workspace

Workspace 在 Build 前解析 Endpoint/Root，并拒绝：

- 相同 Root；
- 父子嵌套 Root。

因此两个 Repository 不会共同管理同一物理 Hybrid Root。

---

# 6. P0-01：Remote Plan 与执行之间的竞态可绕过 Adoption

## 6.1 严重性

```text
级别：P0
类型：未知远端内容删除 / Adoption Bypass / TOCTOU
状态：发布阻断
```

## 6.2 根因

Remote Plan 阶段只读取一次：

```text
Ownership Manifest
Current/Old Path Type
Adoption Requirement
```

确认后正式执行时，没有重新验证：

- Ownership Manifest Hash 是否变化；
- 每个受管路径的类型是否仍与 Plan 一致；
- 原本 Missing 的路径是否仍然 Missing；
- 原本 Owned 的路径是否被其他发布器替换；
- Remote Plan 是否已经过期。

执行阶段 `_backup_current()` 只做：

```text
当前路径存在
    → 移动到 Backup
```

它不会区分：

```text
Plan 时已经存在并明确 Adoption
```

和：

```text
Plan 后、确认窗口中新出现的未知路径
```

## 6.3 可复现流程

初始：

```text
Local Hybrid:
  assets/

Remote:
  assets/ 不存在

Ownership:
  assets 不属于历史 Ownership
```

执行：

```text
1. git-deploy 完成 Remote Plan
2. Plan 认为 assets Missing，因此不要求 --full Adoption
3. 工具显示 Plan，等待用户确认
4. 此时人工或另一个发布器创建：
      assets/important-user-content
5. 用户确认
6. _backup_current() 发现 assets 存在
7. assets 被移动到 Backup
8. Stage/assets 被发布到 assets
9. Ownership Manifest 声明 assets 属于 Hybrid
10. Cleanup 删除 Backup
```

结果：

```text
important-user-content 永久删除
```

## 6.4 为什么这是核心承诺破坏

v1.4.0 的设计承诺：

```text
未知远端内容永远不处理
```

但当前实现只保证：

```text
Remote Plan 时未知内容不存在
```

并没有保证：

```text
执行时仍然不存在
```

## 6.5 并发部署同样受影响

如果另一个设备在确认窗口：

- 更新 Ownership Manifest；
- 发布同名目录；
- 完成另一个 Hybrid 部署；

当前命令仍会依据过期 Plan 覆盖它。

本地 Target Lock 不能解决：

- 跨机器；
- 跨 Clone；
- 手工部署；
- 服务器侧发布器。

## 6.6 必须修复

HybridPlan 必须冻结：

```python
expected_ownership_hash
expected_path_types: dict[str, RemotePathType]
```

确认后、任何普通文件或 Hybrid 写入前执行 Freshness Gate：

```text
1. 重新读取 Ownership Manifest Raw Hash
2. 与 approved hash 比较
3. 重新 lstat 所有 Current ∪ Historical 路径
4. 与 approved path type 比较
5. 任一变化：
       StaleRemotePlanError
       Remote Mutation = 0
       要求重新规划和确认
```

Freshness Gate 必须发生在：

```text
_execute_hybrid_plan()
    最开始
```

并且在普通 `plan.operations` 前执行。

否则可能出现：

```text
普通 Source 已上传
Hybrid Stale Gate 才失败
```

## 6.7 必须新增测试

- [x] Missing Path 在确认窗口变成 File；
- [x] Missing Path 在确认窗口变成 Directory；
- [x] Owned File 在确认窗口变成 Directory；
- [x] Owned Directory 在确认窗口变成 File；
- [x] Ownership Manifest 在确认窗口变化；
- [x] Freshness Failure 时所有 Remote Mutation 为零；
- [x] Workspace 中后一仓 Stale 时前面尚未执行的仓不被写入。

---

# 7. P1-01：Recovery 在用户确认前自动修改远端

## 7.1 现状

普通 Hybrid Deployment：

```text
Local Prepare
prepare_remote_plan(allow_recovery=True)
    ↓
reconcile_recovery()
    ↓
render_plan()
    ↓
confirm()
```

因此，只要存在 Recovery Record：

```text
用户看到新 Plan 之前
```

工具就可能：

- 删除 Stage；
- 删除 Backup；
- 将 Backup 恢复到线上；
- 删除新发布目录；
- 删除 Recovery Record。

## 7.2 影响

用户可能：

```text
运行 git-deploy
看到 Plan
选择取消
```

但远端已经发生恢复或清理。

这违背通常可理解的交互契约：

> 用户确认前不应发生远端写操作。

`--remote-plan` 使用 `allow_recovery=False`，保持只读；问题只存在普通部署。

## 7.3 建议方案

推荐最简单、最清晰的 v1.4.1 方案：

```text
发现 Pending Recovery
    → 普通部署不自动修改
    → 显示 Recovery Summary
    → 要求显式执行：
        git-deploy TARGET --recover
```

或者增加双阶段确认：

```text
Confirm Recovery
Execute Recovery
Rebuild Remote Plan
Confirm Deployment
Execute Deployment
```

不建议：

```text
自动恢复后继续沿用恢复前的 Plan
```

因为恢复会改变远端事实。

## 7.4 原子 TODO

- [x] Recovery 检测和 Recovery 执行分离；
- [x] Remote Plan 显示 Recovery；
- [x] 用户取消时 Remote Mutation = 0；
- [x] Recovery 后重新读取 Ownership 和 Path Types；
- [x] Recovery 后重新生成 Plan；
- [x] 新增取消部署回归测试。

---

# 8. P1-02：命令失败后的待重试语义在删除-only 场景可能丢失

## 8.1 现状

Hybrid 顺序：

```text
Files / Swap
Ownership Commit
after_deploy
Local State
```

命令失败时：

```text
Ownership = 新值
Local State = 旧值
Recovery Phase = OWNERSHIP_COMMITTED
```

下一次 Remote Preflight：

```text
reconcile_recovery()
    认为 Ownership 已提交
    清理 Backup / Recovery
```

然后重新 Planner。

## 8.2 被现有测试覆盖的场景

现有命令失败测试仍包含：

```text
assets/ Mirror Directory
```

Mirror Directory 每次都会产生 Remote Work，所以重跑仍会执行命令。

## 8.3 未覆盖的失败场景

假设本次部署只做：

```text
删除最后一个 Root File
```

或：

```text
删除最后一个 Mirror Directory
```

文件删除与 Ownership Commit 成功，命令失败。

下一次：

```text
Local Hybrid 已为空
Remote Ownership 也已为空
无 Mirror Directory
无 Root File Upload
无 Ownership Change
```

Planner 可能得到：

```text
has_remote_work = false
```

随后直接保存 Local State，不再执行失败的 `after_deploy`。

这破坏了 v1.3 已建立的语义：

```text
命令失败
    → 下次部署重复命令
```

## 8.4 修复方向

Recovery 需要记录：

```text
commands_pending = true
```

或让 `reconcile_recovery()` 返回恢复结果：

```python
HybridRecoveryOutcome(
    ownership_committed=True,
    commands_complete=False,
    state_complete=False,
)
```

Planner/PreparedDeployment 必须将它冻结到 Plan：

```text
RESUME AFTER-DEPLOY COMMANDS
```

即使没有文件操作，也要：

```text
Run after_deploy
Save Local State
Complete Recovery
```

## 8.5 必须新增测试

- [x] Root File Delete-only + Command Failure；
- [x] Last Mirror Directory Delete + Command Failure；
- [x] Ownership-only + Command Failure；
- [x] Commands Complete + State Failure；
- [x] Cleanup Failure 不重复已经完成的命令；
- [x] Remote Plan 显示 Commands Pending。

---

# 9. P1-03：Hybrid Local Root 可以错误配置为项目根目录

## 9.1 规格要求

此前方案明确要求：

```text
Hybrid Local Root != Project Root
```

参考聚合脚本也显式拒绝 Destination 为项目根目录。

## 9.2 当前 Core Config

Config 只检查：

```text
Local Path 在 Project Root 内
```

没有检查：

```text
Local Path 是否等于 Project Root
```

因此以下配置可被接受：

```toml
[[outputs]]
name = "frontend-root"
local = "."
remote = "."
mode = "hybrid"
```

## 9.3 风险

Scanner 会遍历 Project Root 的直接子项，包括：

```text
.git/
.deploy/
源码目录
deploy.toml
证书或其他未被 Protect 命中的内容
```

`.git/**` 当前只属于默认 Exclude，不属于 Mandatory Protect。

Hybrid Scanner 不使用 Source Exclude；它只根据：

- Direct Name；
- Symlink；
- File Type；
- Conflict；
- Protect；

进行判断。

在特殊 Source Include 配置下，`.git/` 可能不触发 Source Conflict，从而作为 Mirror Directory 被完整冻结并上传。

风险包括：

- Git Object 泄露；
- Repository History 泄露；
- Remote `.git/` 被创建或覆盖；
- `.deploy/` 被错误递归部署；
- 大量非构建文件进入远端所有权。

## 9.4 必须修复

Config 加载阶段：

```python
if output.mode == "hybrid" and output.local == project_root:
    raise ConfigError("hybrid local root must not be the project root")
```

同时建议：

```text
DEFAULT_PROTECT 增加：
.git/**
.deploy/**
```

Hybrid Direct Name 显式拒绝：

```text
.git
.deploy
.git-deploy
```

即使未来 Protect 被调整，也保留第二层门禁。

## 9.5 必须新增测试

- [x] `local = "."` 拒绝；
- [x] Local Root 为 Project Root Symlink Alias 拒绝；
- [x] Hybrid Direct `.git` 拒绝；
- [x] Hybrid Direct `.deploy` 拒绝；
- [x] Narrow Source Include 不得绕过；
- [x] Reference Aggregator 与 Core Config 规则一致。

---

# 10. P2-01：Recovery 中必要 Backup 缺失时可能静默丢弃记录

## 10.1 当前逻辑

未提交 Ownership 的 SWAPPING Recovery：

```text
遍历 backup_names
```

如果 Backup Missing：

```text
原路径此前不存在且 Final 存在
    → 删除 Final

其他情况
    → continue
```

如果：

```text
name in old_existing_names
Backup Missing
```

当前逻辑直接继续，最后删除：

- Stage；
- Backup Root；
- Recovery Record。

## 10.2 风险

Backup Missing 可能意味着：

- 人工删除；
- 外部清理；
- 不完整 Rename；
- 文件系统异常；
- Recovery 数据和远端事实不一致。

对原本存在的路径，没有 Backup 就无法证明旧内容已经恢复。

此时不应删除 Recovery Record。

## 10.3 修复

如果：

```text
name in old_existing_names
and backup missing
and ownership still old
```

必须：

```text
Fail Closed
保留 Recovery Record
要求人工检查
```

只有能够通过其他强证据证明 Final 就是旧内容时才允许继续；当前没有内容 Hash，因此不能证明。

---

# 11. P2-02：Native OpenSSH 目录列表对首尾空格名称不稳定

## 11.1 当前实现

Native `list_directory()` 使用：

```text
sftp ls -1
```

然后：

```python
line = raw.strip()
```

本地和 Remote Ownership 的 `_safe_component()` 允许：

```text
" assets"
"assets "
```

这样的名称。

## 11.2 风险

首尾空格会被剥离，导致：

- Recovery Filename 识别错误；
- Recursive Cleanup 定位错误；
- 目录枚举不一致；
- Recovery 永久卡住；
- 潜在删除错误路径。

## 11.3 修复选项

最简单的个人工具方案：

```text
Hybrid Path Component 必须等于 value.strip()
```

直接拒绝首尾空格。

同时建议拒绝：

- Tab；
- 其他不可见空白；
- SFTP `ls` 难以稳定表示的名称。

不需要为极端文件名增加复杂二进制远端枚举协议。

---

# 12. P2-03：嵌套空目录不会被 Mirror

## 12.1 当前 Scanner

`HybridDirectoryManifest` 只记录：

```text
files
file_count
total_size
```

递归扫描遇到空的嵌套目录时不会保存目录路径。

Stage 创建目录的依据来自每个文件的父目录。

因此：

```text
assets/empty/nested/
```

如果没有任何文件，部署后不会存在。

## 12.2 影响

前端静态构建通常不依赖空目录，风险较低。

但“目录完整 Mirror”的严格语义并未完全满足。

## 12.3 修复

增加：

```python
directories: tuple[str, ...]
```

记录所有嵌套目录。

Stage 时先按深度创建目录，再上传文件。

如果决定不支持空目录，应在文档中明确：

> Hybrid Mirror 保证文件树一致，不保证嵌套空目录。

---

# 13. P2-04：Root File 增量判断仍信任本地 State

## 13.1 当前语义

Root File 是否上传主要根据：

```text
Local State Hash
Remote Path Type
--full
Adoption
```

它不会读取远端 Root File 的真实 Hash。

如果：

```text
Local State Hash 与本地一致
Remote Root File 仍是 File
但远端内容被人工修改
```

Planner 会跳过上传。

## 13.2 是否属于缺陷

这是此前讨论中接受的设计：

```text
Direct Directory → 强 Mirror
Direct Root File → Incremental
```

因此不算实现偏差。

但文档应明确：

```text
State 丢失可恢复删除所有权
不代表 Root File 内容会在每次部署进行远端 Hash 对账
```

如果用户以后要求 Root File 强一致，需要新增可选：

```text
verify_remote_root_files = true
```

不建议 v1.4.1 立即增加。

---

# 14. 测试覆盖评价

## 14.1 已覆盖

### Config

- SFTP-only；
- 单 Hybrid；
- Name；
- `remote = "."`；
- `delete_removed`；
- Project ID；
- Protect；
- Source Conflict；
- Git Ignore。

### Scanner

- Root File；
- Direct Directory；
- Empty Direct Directory；
- Symlink；
- Frozen Bytes。

### Remote Plan

- Read-only；
- No Remote Mutation；
- Dry-run Zero Connection；
- Adoption；
- Unknown Remote Ignore。

### Ownership

- Schema；
- Identity；
- Size；
- Symlink Manifest；
- State Loss Delete；
- Root File Delete；
- Directory Delete。

### Recovery

- Stage Failure；
- Swap Failure；
- Ownership Write Failure；
- Ctrl-C；
- State Save Failure；
- Cleanup Failure；
- Command Failure；
- Rerun。

### Workspace

- Same Root Rejection；
- Disjoint Root；
- Combined Plan；
- Shared Pool。

### Integration

PR 声明覆盖：

- Paramiko；
- Native OpenSSH；
- Preservation；
- Adoption；
- Recovery；
- Stage/Swap；
- Permissions；
- Cleanup。

## 14.2 缺失的关键测试

- [x] Path 在 Remote Plan 与 Execute 之间从 Missing 变为 Existing；
- [x] Ownership 在确认窗口变化；
- [x] Stale Plan 必须零写入；
- [x] 用户取消时 Pending Recovery 不得修改远端；
- [x] Delete-only Command Failure；
- [x] Last Directory Delete + Command Failure；
- [x] `local = "."`；
- [x] `.git/` Direct Child；
- [x] Old Existing Backup Missing；
- [x] Leading/Trailing Space Name；
- [x] Nested Empty Directory。

当前 195 个测试数量充足，但缺失的正是所有权竞态和极端 Recovery 不变量。

---

# 15. v1.4.1 必须完成的原子 TODO

## P0：Remote Freshness Gate

### TODO-001：冻结 Remote Plan Snapshot

- [x] HybridPlan 增加 `expected_ownership_hash`；
- [x] 增加 `expected_path_types`；
- [x] 包含 `Current ∪ Historical` 全部直接路径；
- [x] Snapshot 进入 Plan Render 调试信息但不暴露秘密。

### TODO-002：执行前重新校验

- [x] 在任何 `plan.operations` 前读取 Ownership Hash；
- [x] 逐个重新 `lstat`；
- [x] 任一变化抛出 `StaleRemotePlanError`；
- [x] Remote Mutation 必须为零；
- [x] Transport 保持可安全关闭；
- [x] 用户重新运行后重新 Plan/Confirm。

### TODO-003：并发回归测试

- [x] Missing → File；
- [x] Missing → Directory；
- [x] File → Directory；
- [x] Directory → File；
- [x] Ownership Changed；
- [x] Unknown Path 内容保留；
- [x] Workspace。

---

## P1：Recovery Confirmation Boundary

### TODO-101：只读 Recovery Plan

- [x] `complete_remote_plan()` 不直接调用 `reconcile_recovery()`；
- [x] Recovery Action 进入 Plan；
- [x] Plan 显示 Restore/Cleanup；
- [x] `--remote-plan` 继续严格零写入。

### TODO-102：确认后恢复

- [x] 用户确认后执行 Recovery；
- [x] Recovery 完成后重新读取 Remote Facts；
- [x] 重新构造并校验 Plan；
- [x] 若 Plan 变化，要求二次确认或重新运行；
- [x] 用户取消时零写入。

---

## P1：Pending Commands

### TODO-201：Recovery Outcome

- [x] `reconcile_recovery()` 返回 Phase Outcome；
- [x] 区分 Ownership Committed / Commands Complete / State Complete；
- [x] Pending Command 进入 DeploymentPlan；
- [x] 无文件操作也能执行命令；
- [x] 命令成功后保存 State。

### TODO-202：回归测试

- [x] Root Delete-only；
- [x] Directory Delete-only；
- [x] Empty Ownership；
- [x] Command Failure；
- [x] State Failure；
- [x] Cleanup Failure。

---

## P1：Local Root Safety

### TODO-301：拒绝 Project Root

- [x] Hybrid Local == Project Root 直接 ConfigError；
- [x] Resolve 后相等也拒绝；
- [x] Symlink Alias 拒绝。

### TODO-302：强化 Protect

- [x] `.git/**` 加入 Mandatory Protect；
- [x] `.deploy/**` 加入 Mandatory Protect；
- [x] Direct `.git` 显式拒绝；
- [x] Direct `.deploy` 显式拒绝；
- [x] Direct `.git-deploy` 显式拒绝。

---

## P2：Recovery Fail Closed

### TODO-401：Missing Backup

- [x] Old Existing Path 的 Backup Missing 时拒绝；
- [x] Recovery Record 保留；
- [x] Doctor 显示 Manual Inspection；
- [x] 不删除 Stage/Backup Root；
- [x] 不删除 Recovery Record。

---

## P2：Path Name Boundary

### TODO-501：拒绝不稳定名称

- [x] Direct/Nested Component 必须 `value == value.strip()`；
- [x] 拒绝 Tab；
- [x] 拒绝不可见空白；
- [x] Ownership Parser 使用同一规则；
- [x] Native/Paramiko 一致。

---

## P2：Empty Directory

### TODO-601：选择并固定语义

方案 A：

- [x] Manifest 记录 Nested Directories；
- [x] Stage 创建所有空目录；
- [x] Integration Test。

方案 B：

- [ ] 文档明确不保留 Nested Empty Directory；（不适用：已采用方案 A）
- [ ] Plan 提示；（不适用：已采用方案 A）
- [ ] Test 固定行为。（不适用：已采用方案 A）

推荐方案 A。

---

# 16. 修复后验收标准

v1.4.1 至少满足：

1. Remote Plan 后人工创建 `assets/`，确认部署必须 Stale Fail；
2. 未经 `--full` 不得备份、替换或删除新出现的路径；
3. Ownership Manifest 变化后旧 Plan 不得执行；
4. Freshness Failure 时普通 Source/Output 也未开始写入；
5. 用户取消普通部署时 Pending Recovery 不得执行；
6. Delete-only 命令失败后，下次仍会执行命令；
7. `local = "."` 配置加载失败；
8. `.git/` 永远不能成为 Hybrid Direct Directory；
9. 原路径存在但 Backup 缺失时 Recovery Fail Closed；
10. Native/Paramiko 对支持的 Path Name 行为一致；
11. Main/Tag Blob 一致；
12. Python 3.11/3.12 CI 通过；
13. Docker Native/Paramiko 竞态测试通过；
14. 无未解决 Review Thread。

---

# 17. 当前版本使用建议

## 可以继续使用

```text
Git Source Incremental
Incremental Output
FTP
SFTP 普通文件同步
Native OpenSSH
1Password Agent
Thin Workspace
after_deploy
```

## 暂停使用

```text
mode = "hybrid"
```

尤其不要在包含以下内容的生产根目录启用：

- `.env`；
- `index.php`；
- 上传文件；
- 后端源码；
- 手工文件；
- 多个发布器。

## 临时替代

在 v1.4.1 修复前：

- 继续使用 v1.3 Incremental Output；
- 对专属静态子目录使用经过人工审阅的 `rsync --delete`；
- 不对混合根目录自动执行未知文件清理；
- 保存并备份现有本地 State。

---

# 18. 最终结论

v1.4.0 的总体架构方向是正确的，完成度也较高：

```text
Local Aggregation
Single Hybrid
Remote Ownership
Adoption
Stage/Swap
Recovery
Workspace
```

但 Hybrid 是一个具有删除权限的所有权系统，其核心不变量必须是：

> 执行时的远端事实必须与用户审阅并确认的 Remote Plan 完全一致。

当前版本缺少这个执行前 Freshness Gate，导致确认窗口中新出现的未知路径可能绕过 Adoption 并被永久删除。

因此：

> **v1.4.0 Hybrid 审计不通过，建议立即发布 v1.4.1 安全收口；修复前不要在混合生产根目录启用 Hybrid。**

非 Hybrid 主链未发现由本次改动引入的同等级阻断问题。
