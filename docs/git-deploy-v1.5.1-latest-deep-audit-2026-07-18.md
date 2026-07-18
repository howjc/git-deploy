# git-deploy v1.5.1 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`3f7c258a2a6dcf2143c57bfc70e055d739fe9abe`
> 功能提交：`9d65fd9575b6f70fe5549d1af7407798d93bd8a9`
> PR：`#16 fix: close FTP Hybrid safety gaps for v1.5.1`
> 版本：`v1.5.1`
> 审计日期：`2026-07-18`
> 审计结论：**有条件通过**
> 建议：发布一个小型 `v1.5.2` 完成 Pending 非 Hybrid Plan 合约与跨机器 State 收口

---

# 1. 执行摘要

v1.5.1 对 v1.5.0 深度审计提出的全部明确整改项进行了有效修复：

1. Capability Profile 升级到 Schema 2；
2. 显式证明 FTP 文件系统大小写敏感；
3. Local Hybrid Root 和 Mirror 内部拒绝 NFC + Casefold 冲突；
4. Remote MLSD 拒绝 NFC + Casefold 冲突；
5. Schema 1 Capability Profile 强制重新 Probe；
6. Initial Pending 写入失败后清理本次 Stage；
7. Shared Stage Parent 改为 Best-effort，不再阻断当前 Pending 完成；
8. Capability Probe 只强制清理本次随机 Probe Root；
9. Doctor 报告 Orphan Stage，不静默删除；
10. Pending Ownership Hash 使用严格 Phase Matrix；
11. `OWNERSHIP_COMMITTED` / `STATE_COMPLETE` 增加独立 FTP `--recover`；
12. FTP Post-commit Recovery 不运行 Build、不读取当前 State、不扫描 Local Hybrid；
13. 每次 FTP Freshness Gate 前主动清除 MLSD/NLST/Missing Cache；
14. `--reprobe` 已移除，显式 Probe 本身即表示重新探测并覆盖 Profile；
15. Python 3.11 / 3.12 全量测试、真实 Passive/Active FTP 测试通过。

在以下使用前提下，FTP Hybrid 已经可以进入真实项目的受控验证：

```text
单发布器
同一时间只有一个部署
Pending PREPARED / FILES_PUBLISHED 期间保持同一 Local State 和配置
不在 Pending 期间使用 --full 改写普通 Source/Incremental Plan
FTP 路径以 ASCII 或已确认可精确往返的 Unicode 名称为主
```

本轮没有发现新的 P0。

但是，本轮发现两个新的 P1 状态一致性问题：

## P1-01：Pre-commit Pending 没有冻结普通 Source / Incremental Plan

FTP Pending 当前冻结：

- Local Hybrid + Incremental Output Manifest Hash；
- HEAD；
- Next State；
- Old/New Ownership Hash；
- Target Identity；
- Phase。

但没有冻结：

- 原 Deployment 的普通 `plan.operations`；
- 原 Local State；
- Source Include/Exclude/Protect Policy；
- Incremental Output `delete_removed` Policy；
- `full` / Incremental 计划语义。

因此在 `PREPARED` 或 `FILES_PUBLISHED` 阶段，如果：

- Local State 被删除；
- 换机器后没有旧 State；
- 使用 `--full` 重跑；
- Source Policy 被修改；
- Incremental Output Policy 被修改；

当前 Planner 可能生成与原部署不同的 Source/Incremental Upload/Delete Operations，而 Pending 仍会通过 Manifest、HEAD 与 Ownership 校验。

随后工具会保存 Pending 中冻结的新 State，导致原本应删除的 Source/Incremental 文件永久残留，或者执行并非原部署审阅过的普通操作。

## P1-02：`STATE_COMPLETE` Recovery 不会在当前 Clone 重写冻结 State

FTP Explicit Recovery 当前仅在：

```text
OWNERSHIP_COMMITTED
```

阶段保存：

```text
pending.next_state
```

如果 Pending 已经是：

```text
STATE_COMPLETE
```

则只执行内部 Cleanup。

这在原机器 State 仍存在时成立，但跨机器或 State 文件随后丢失时：

```text
远端 Pending STATE_COMPLETE
当前 Clone 没有 Local State
--recover 只清理 Marker
```

Pending 中明明包含完整 Frozen State，却没有写入当前 Clone。之后普通部署退化成 Full Plan，无法恢复 Source/Incremental 的历史删除基线。

另外发现两个 P2 兼容性问题：

1. Remote Scanner 的 Casefold Collision Policy 可能被 Source/Incremental 根路径自行触发，但 Local Preflight 目前只检查 Hybrid 内部冲突；
2. Capability Probe 只证明 ASCII 大小写语义，没有证明 Unicode 编码与规范化能够精确往返。

因此本轮结论为：

> **v1.5.1 有条件通过，可以用于当前 FTP-only 项目验证，但建议在将其标记为稳定基线前发布 v1.5.2。**

---

# 2. 审计范围

## 2.1 发布完整性

- Main 最新 Commit；
- PR #16；
- `v1.5.1` Tag；
- Main/Tag Package Blob；
- Main/Tag FTP Hybrid Blob；
- GitHub Actions；
- Python 3.11；
- Python 3.12；
- Ruff；
- ty；
- Lockfile；
- Wheel/sdist；
- Isolated Wheel Install；
- CLI Smoke；
- PR Review Threads。

## 2.2 v1.5.0 整改项

- Case-sensitive Capability；
- Capability Schema Migration；
- Local Casefold Collision；
- Remote Casefold Collision；
- Initial Pending Write Cleanup；
- Orphan Stage；
- Shared Parent Cleanup；
- Probe Sibling；
- Phase-sensitive Pending；
- Ownership Hash Matrix；
- Post-commit FTP Recovery；
- Build/HEAD-independent Recovery；
- MLSD Cache Refresh；
- `--reprobe` 移除。

## 2.3 新状态机审查

- PREPARED；
- FILES_PUBLISHED；
- PRUNED；
- OWNERSHIP_COMMITTED；
- STATE_COMPLETE；
- Local State Loss；
- `--full` Resume；
- Source Policy Drift；
- Incremental Output Policy Drift；
- Cross-machine Recovery；
- State File Loss；
- Root Namespace Collision；
- Unicode Path Semantics。

---

# 3. 审计方式与限制

本轮通过 GitHub Connector 检查：

- Commit History；
- PR Metadata 和完整 Diff；
- Main/Tag 文件；
- CI Run 与 Job；
- Capability Schema；
- Capability Probe；
- FTP Transport；
- Hybrid Local Scanner；
- Pending Schema；
- Planner；
- Deployer；
- Explicit Recovery；
- Doctor；
- CLI；
- Unit Tests；
- Real FTP Integration Tests；
- Release Notes；
- ADR。

本地尝试：

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

因此无法在本轮独立执行：

```bash
uv lock --check
uv run --isolated --python 3.11 --all-groups pytest -q
uv run --isolated --python 3.12 --all-groups pytest -q
uv run ruff check src tests examples
uv run ty check src
uv build --clear
```

动态验证依据为成功的 GitHub Actions 和仓库中的真实 pyftpdlib / SFTP 集成测试；状态机结论来自当前 Main 的独立源码审计。

---

# 4. 版本、Tag 与 CI

## 4.1 Main

```text
3f7c258a2a6dcf2143c57bfc70e055d739fe9abe
Merge pull request #16 from howjc/agent/v1.5.1-ftp-hybrid-safety-closeout
```

功能提交：

```text
9d65fd9575b6f70fe5549d1af7407798d93bd8a9
fix: close FTP Hybrid safety gaps for v1.5.1
```

## 4.2 Package

Main：

```toml
version = "1.5.1"
```

Tag：

```toml
version = "1.5.1"
```

Main/Tag `pyproject.toml` Blob：

```text
ab87d3a4beb07bf8bbcc8fa9b0fb2ec0decc8187
```

一致。

## 4.3 FTP Hybrid Blob

Main/Tag `src/git_deploy/ftp_hybrid.py` Blob：

```text
336dc4f130a41cf458a15874c7a8133fc4452f09
```

一致。

## 4.4 CI

PR Head CI：

```text
status: completed
conclusion: success
```

Python 3.11 和 Python 3.12 均通过：

- Interpreter Matrix；
- Lockfile Check；
- Dependency Install；
- 249 Tests；
- Ruff；
- ty；
- Build Package；
- Isolated Wheel Install；
- CLI Version/Help Smoke。

PR 无未解决 Review Thread。

---

# 5. v1.5.0 整改关闭情况

---

## 5.1 P0：FTP 路径大小写语义

### 实现

Capability Profile 升级为：

```text
Schema 2
```

新增：

```python
case_sensitive_paths: bool
```

Schema 1 不做无证据迁移，直接要求重新 Probe。

### Probe

在同一目录创建：

```text
CaseProbe.bin
caseprobe.bin
```

验证：

- MLSD 同时返回两个独立名称；
- 两个文件内容独立；
- 删除一个不影响另一个；
- Case-only Rename 后 Source 消失；
- Destination 内容保持。

大小写不敏感服务器：

```text
Fail Closed
不保存 Capability Profile
```

### Local

Hybrid Root 和每个 Mirror Directory 都拒绝：

```text
NFC(name).casefold()
```

碰撞。

### Remote

每次 MLSD Typed Listing 都拒绝：

- Exact Duplicate；
- NFC Collision；
- Casefold Collision。

只有 Capability Probe 自己使用：

```python
allow_case_collisions=True
```

读取刻意创建的两个 Case Variant。

### 测试

覆盖：

- Schema 1 Reprobe；
- Root Collision；
- Nested Collision；
- Remote MLSD Collision；
- Case-insensitive Fake Server；
- Real Case-sensitive pyftpdlib。

### 结论

**上一轮 P0 已关闭。**

---

## 5.2 P1：Orphan Stage 永久阻断

### Initial Pending

第一次 Pending 写入失败时：

```text
清理当前 deployment-id Stage Root
Shared Stage Parent Best-effort
```

### Successful Cleanup

顺序改为：

```text
删除当前 Stage Root
删除当前 Pending Marker
Best-effort 删除 Shared Stage Parent
```

旧 Sibling Stage 不再阻止当前 Marker 完成。

### Probe

Capability Probe：

```text
只强制清理本次随机 Probe Root
Shared Probe Parent Best-effort
```

### Doctor

新增：

```text
FTP Hybrid Orphan Stage
```

显示：

- Deployment ID；
- Age；
- Entry Count。

不自动删除。

### 真实测试

覆盖：

- Initial Pending Rename Failure；
- Stage Root Cleanup；
- Old Orphan Sibling；
- New Deployment Success；
- Pending Marker 删除；
- Doctor Orphan Report；
- Probe Sibling Preservation。

### 结论

**上一轮 P1 已关闭。**

---

## 5.3 P2：Post-commit Pending 依赖 Build/HEAD

### Phase Validation

```text
PREPARED
FILES_PUBLISHED
PRUNED
```

继续要求：

- Local Manifest；
- HEAD。

```text
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

不再要求当前 Local Manifest 或 HEAD。

### Explicit FTP Recovery

FTP `--recover` 仅接受：

```text
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

流程：

```text
Load Config
Resolve Target
Acquire Lock
Connect FTP
Load Capability
Read Ownership
Read Pending
Validate Ownership Phase
Confirm
Save Frozen State when required
Cleanup Current Stage and Marker
```

不执行：

- Build；
- Existing State Load；
- Local Hybrid Scan；
- Source Plan；
- Output Plan；
- Freeze。

### 测试

覆盖：

- Ownership Committed；
- New HEAD；
- `.deploy` 删除；
- Broken Build；
- Frozen State Commit；
- State Complete；
- Newer HEAD；
- Cleanup Retry。

### 结论

**上一轮 P2 主问题已关闭。**

---

## 5.4 P2：MLSD Cache Freshness

FTP Transport 新增：

```python
refresh_remote_metadata()
```

清理：

- Typed MLSD Cache；
- NLST Cache；
- Confirmed Missing Cache。

每次 FTP Freshness Gate 开始前执行。

真实测试覆盖：

```text
Remote Plan 后外部写入 Mirror File
Freshness 重新 MLSD
StaleRemotePlanError
零 Pending 写入
外部文件保留
```

PRUNED Resume 同样覆盖外部 Tree Drift。

### 结论

**上一轮 P2 已关闭。**

---

## 5.5 Pending Ownership Matrix

严格矩阵：

```text
PREPARED
    previous

FILES_PUBLISHED
    previous

PRUNED
    previous or next

OWNERSHIP_COMMITTED
    next

STATE_COMPLETE
    next
```

Doctor 对不一致状态显示：

```text
Manual Inspection Required
```

### 结论

**已关闭。**

---

## 5.6 CLI `--reprobe`

无独立语义的：

```text
--reprobe
```

已移除。

显式：

```bash
doctor --probe-ftp-hybrid
```

本身即表示重新探测和覆盖本地 Profile。

### 结论

**已关闭。**

---

# 6. P1-01：Pre-commit Pending 没有冻结普通 Deployment Plan 合约

## 6.1 严重性

```text
级别：P1
影响：Source / Incremental Output 历史删除与 Resume 正确性
结果：远端旧文件可能永久残留，State 仍推进
```

## 6.2 当前 Pending 冻结内容

```python
schema
project_id
mapping
remote
target_fingerprint
deployment_id
phase
previous_ownership_hash
next_ownership_hash
local_manifest_hash
head
next_state
created_at
```

缺少：

```text
原 Local State Hash
原 Previous Commit
原普通 Operation Queue
Source Policy Hash
Incremental Output Policy Hash
Full/Incremental Plan Contract
```

## 6.3 当前 Resume 校验

Pre-commit 只验证：

```text
Local Manifest Hash
HEAD
Ownership Phase Matrix
```

`Local Manifest Hash` 包含：

- Hybrid Local Manifest；
- Current Incremental Output Content。

不包含：

- Source Diff Base；
- Previous Output Manifest；
- Source Include/Exclude；
- Output Delete Policy；
- 当前 `plan.operations`。

## 6.4 Planner 对 State/Full 的依赖

Planner：

```python
effective_full = full or state is None
```

Full Source Plan：

```text
上传当前所有 Source
不生成历史 Source Delete
```

Full Output Plan：

```text
上传当前所有 Output
不生成历史 Output Delete
```

Incremental Plan 才根据旧 State 产生：

```text
Source Delete
Output Delete
```

## 6.5 复现一：Pending 后 State 丢失

初始成功 State：

```text
Commit C1
Remote old.php 存在
```

新 HEAD C2：

```text
删除 old.php
增加 FTP Hybrid 文件
```

原 Plan：

```text
DELETE old.php
FTP Hybrid Upload
```

流程：

```text
1. 写 Pending PREPARED
2. 在执行普通 DELETE 前中断
3. 删除本地 .git/git-deploy/prod.json
4. 保持 HEAD C2 和 Local Hybrid 不变
5. 重跑
```

当前 Planner：

```text
state = None
effective_full = true
```

新 Plan：

```text
上传当前 Source
没有 DELETE old.php
```

Pending：

```text
Manifest 相同
HEAD 相同
Ownership 相同
```

验证通过。

最终：

```text
old.php 仍在远端
Pending Next State C2 被保存
未来 Diff 不会再次看到 old.php 的删除
```

## 6.6 复现二：Pending 期间使用 `--full`

即使旧 State 还在：

```bash
git-deploy prod --full
```

也会重新生成 Full Plan，丢弃原计划中的 Source/Output Delete。

Pending 当前没有拒绝这种 Plan Drift。

## 6.7 复现三：Config Policy 变化

保持：

```text
HEAD 不变
Local Hybrid 不变
Current Outputs 不变
```

修改：

```toml
[source]
exclude = [...]

[[outputs]]
delete_removed = false
```

普通 Operation Queue 会改变，但 Pending 仍可能通过。

## 6.8 风险边界

这不会越过 Hybrid Remote Ownership 删除未知根内容。

但会破坏：

```text
Forward Resume
Source/Incremental 收敛
State 与远端实际状态一致
```

如果当前项目同时通过 git-deploy 发布后端 Source，这个问题具有实际影响。

## 6.9 修复方案

### Pending Schema 2

增加：

```python
non_hybrid_plan_hash: str
previous_state_hash: str
```

其中 `non_hybrid_plan_hash` 应覆盖：

```text
plan.full
plan.previous_commit
每个普通 Operation：
    type
    origin
    remote_path
    git_path
    executable
    content SHA/Size
Source Policy
Incremental Output Mapping/Delete Policy
```

或者直接对稳定序列化后的：

```text
plan.operations + relevant config contract
```

计算 SHA256。

### Resume

```text
PREPARED
FILES_PUBLISHED
```

必须：

```text
Current Non-Hybrid Plan Hash
=
Pending Non-Hybrid Plan Hash
```

不相等：

```text
Fail Closed
要求恢复原 State/Config
```

### `--full`

存在 Pre-commit Pending 时：

```text
拒绝 --full
```

除非新 Plan Hash 与 Pending 完全一致。

### FILES_PUBLISHED 优化

更简单的另一方向：

```text
FILES_PUBLISHED
    不再重复执行普通 Operations
    只继续 Prune
```

因为该 Phase 已证明：

```text
普通 Operations
当前 Hybrid Files
```

全部完成。

但 PREPARED 仍然必须冻结 Operation Contract。

## 6.10 测试

- Pending PREPARED + State Missing；
- Pending PREPARED + Different Previous Commit；
- Pending PREPARED + `--full`；
- Pending PREPARED + Source Exclude Change；
- Pending PREPARED + Output delete_removed Change；
- Pending PREPARED + Original Source Delete；
- Pending PREPARED + Original Output Delete；
- Plan Drift Zero Mutation；
- Schema 1 Pending Migration；
- FILES_PUBLISHED 是否重复普通 Operations。

---

# 7. P1-02：STATE_COMPLETE Recovery 没有恢复当前 Clone 的 Frozen State

## 7.1 当前实现

FTP Recovery：

```python
if pending.phase == OWNERSHIP_COMMITTED:
    state_store.save(pending.next_state)
    pending = STATE_COMPLETE
cleanup()
```

如果已经：

```text
STATE_COMPLETE
```

则直接 Cleanup。

## 7.2 问题场景

机器 A：

```text
Ownership 已提交
State 已保存
Pending STATE_COMPLETE
Cleanup 失败
```

之后：

- 切换机器 B；
- 重新 Clone；
- 删除机器 A 的 Local State；
- 在机器 B 执行 `--recover`。

机器 B：

```text
没有 Local State
Pending 包含完整 next_state
```

但当前实现：

```text
只删除 Pending
不保存 next_state
```

下一次部署：

```text
state = None
effective_full = true
```

Hybrid Ownership 仍可恢复 Hybrid 删除，但 Source/Incremental 历史删除基线丢失。

## 7.3 修复

FTP Explicit Recovery 对两个 Phase 都应先执行：

```python
state_store.save(pending.next_state)
```

即：

```text
OWNERSHIP_COMMITTED:
    Save Frozen State
    Write STATE_COMPLETE
    Cleanup

STATE_COMPLETE:
    Idempotently Save Frozen State
    Cleanup
```

State Save 是原子且幂等的。

## 7.4 为什么安全

Pending Phase 为 STATE_COMPLETE 时：

```text
Remote Ownership = Next
Pending Identity = Valid
Pending Frozen State = Next Ownership Commit
```

在 Cleanup 前再次写相同 State 不会触发远端 Mutation，也不会重复业务操作。

## 7.5 测试

- STATE_COMPLETE + Missing Local State；
- STATE_COMPLETE + New Clone；
- STATE_COMPLETE + Different Current HEAD；
- STATE_COMPLETE + Existing Older State；
- STATE_COMPLETE + Existing Newer-but-uncommitted Local State；
- State Save Failure 保留 Pending；
- Retry Saves Same Frozen State。

---

# 8. P2-01：Casefold Policy 没有覆盖所有远端根级 Owner

## 8.1 当前行为

Local Hybrid Scanner 拒绝 Hybrid 内部 Casefold Collision。

但是 Source/Incremental 与 Hybrid 的冲突检查仍按精确字符串比较。

示例：

```text
Source:
    Assets/backend.php

Hybrid:
    assets/
```

本地 Preflight 可能通过。

## 8.2 执行结果

FTP Server 已证明大小写敏感，因此可同时创建：

```text
Assets/
assets/
```

但是 Remote `list_directory_typed(".")` 会按 NFC + Casefold 拒绝这两个 Sibling。

部署可能：

1. 普通 Source 创建 `Assets/`；
2. Hybrid 创建 `assets/`；
3. 当前部署完成；
4. 下一次 Remote Plan 因 Root Casefold Collision 失败。

如果两个 Source Root 本身存在 Casefold Collision，也可能在第二次 Freshness 时被工具自己阻断。

## 8.3 修复选择

### 方案 A：统一 Portable Root Policy

FTP Hybrid Local Preflight 对所有当前 Managed Root Component 执行：

```text
NFC + Casefold Unique
```

包括：

- Source；
- Incremental Output；
- Hybrid；
- `.git-deploy` Internal Reservation。

推荐此方案，行为简单可预测。

### 方案 B：放宽 Remote Casefold Policy

Capability 已证明服务器大小写敏感，因此 Remote Scanner 只拒绝：

- Exact Duplicate；
- Unicode Normalization Duplicate。

允许 Case-distinct Names。

这提高兼容性，但需要重新审视 v1.5.0 的 Portable Naming 目标。

## 8.4 推荐

个人极简工具建议选：

```text
方案 A
```

## 8.5 测试

- Source `Assets/` + Hybrid `assets/`；
- Incremental `Assets/` + Hybrid `assets/`；
- Source Root 两个 Case Variants；
- Source `.GIT-DEPLOY`；
- Remote Unknown Root Case Variants；
- First Deployment 不得创建未来无法扫描的 Root。

---

# 9. P2-02：Unicode 编码与规范化语义尚未证明

## 9.1 当前能力证明

Probe 使用 ASCII：

```text
CaseProbe.bin
caseprobe.bin
```

能够证明：

- ASCII Case Sensitivity；
- Case-only Rename。

不能证明：

- UTF-8 Command/Response；
- Unicode Filename Exact Round-trip；
- NFC/NFD Preservation；
- 非 ASCII Rename；
- Server Encoding。

## 9.2 失败场景

Local：

```text
assets/é.js        NFC
```

服务器可能报告：

```text
assets/e◌́.js      NFD
```

下一次 Plan：

```text
UPLOAD NFC Name
DELETE NFD Orphan
```

若服务器把两种形式视为同一文件：

```text
发布后又删除同一底层文件
```

类似大小写问题。

旧 FTP Server 也可能使用非 UTF-8 编码，但仍通过纯 ASCII Capability Probe。

## 9.3 修复

选择其一：

### 方案 A：Unicode Round-trip Probe

验证：

- FEAT UTF8；
- `OPTS UTF8 ON`；
- Chinese Filename；
- NFC Composed Filename；
- MLSD Exact Name；
- RETR；
- Rename；
- Delete。

Profile 增加：

```python
unicode_paths: bool
normalization_preserving: bool
```

### 方案 B：FTP Hybrid 仅允许 ASCII Path Component

实现最简单，但会限制图片和静态资源名称。

## 9.4 推荐

优先：

```text
Unicode Round-trip Probe
```

若实际目标服务器仅使用 ASCII 构建产物，可作为 P2 后续收口，不阻断当前验证。

---

# 10. P3：普通部署仍能处理 Post-commit Pending

文档将：

```text
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

定义为显式 `--recover`。

但普通部署在 Local Build 与 HEAD 未变化时，仍可能通过 `_complete_ftp_remote_plan()` 继续这些 Phase。

这不会越过确认：

- 普通部署仍显示 Plan；
- 仍需要确认；
- 使用 Frozen Pending State。

但存在两套入口：

```text
normal deploy
--recover
```

不利于形成单一操作模型。

建议在 Remote Plan 读取到 Post-commit Pending 时：

```text
普通部署直接提示：
run --recover
```

不再生成普通 Deployment Plan。

---

# 11. 测试覆盖评价

## 11.1 已覆盖良好

- Capability Schema 2；
- Schema 1 Reprobe；
- ASCII Case Sensitivity；
- Local Root Collision；
- Local Nested Collision；
- Remote MLSD Collision；
- Case-insensitive Fake Server；
- Probe Sibling；
- Initial Pending Failure；
- Orphan Stage；
- Doctor Orphan；
- Freshness External Mutation；
- PRUNED External Mutation；
- Ownership Matrix；
- Ownership Committed Broken Build；
- State Complete New HEAD；
- Passive FTP；
- Active FTP；
- SFTP Regression；
- Native Regression。

## 11.2 缺失关键测试

- PREPARED + Local State Missing；
- PREPARED + `--full`；
- PREPARED + Source Policy Change；
- PREPARED + Output Delete Policy Change；
- PREPARED + Original Source Delete；
- PREPARED + Original Output Delete；
- STATE_COMPLETE + Missing State on Current Clone；
- Cross-owner Root Casefold Collision；
- Unicode NFC/NFD；
- FTP UTF8 Disabled；
- vsftpd / ProFTPD / Pure-FTPd；
- Actual Target Server。

---

# 12. v1.5.2 原子 TODO

## P1：Pending Plan Contract

### TODO-001：Pending Schema 2

- [x] 增加 `non_hybrid_plan_hash`
- [x] 增加 `previous_state_hash`
- [x] 严格 Parser
- [x] Size Limit
- [x] Schema 1 Migration Policy

### TODO-002：Stable Plan Hash

- [x] Operation Type
- [x] Origin
- [x] Remote Path
- [x] Git Path
- [x] Executable
- [x] Output SHA/Size
- [x] Full Flag
- [x] Previous Commit
- [x] Source Policy
- [x] Incremental Output Policy

### TODO-003：Resume Gate

- [x] PREPARED Validate Plan Hash
- [x] FILES_PUBLISHED Validate/Skip Regular Plan
- [x] Reject State Missing
- [x] Reject Config Drift
- [x] Reject `--full` Drift
- [x] Zero Mutation on Mismatch

---

## P1：Frozen State Recovery

### TODO-101：Always Save Frozen State

- [x] OWNERSHIP_COMMITTED Save
- [x] STATE_COMPLETE Save
- [x] Atomic/Idempotent
- [x] Save Failure Retains Pending
- [x] Cross-machine Test

---

## P2：Portable Root Namespace

### TODO-201：All-owner Root Collision Gate

- [x] Source Root Components
- [x] Incremental Root Components
- [x] Hybrid Direct Names
- [x] Internal `.git-deploy`
- [x] NFC + Casefold
- [x] Historical Managed Roots

---

## P2：Unicode

### TODO-301：FTP Unicode Capability

- [x] FEAT UTF8
- [x] OPTS UTF8 ON
- [x] Chinese Name
- [x] NFC Name
- [x] Exact MLSD Round-trip
- [x] Rename/Delete
- [x] Capability Schema 3 or extension

---

## P3：CLI

### TODO-401：Single Post-commit Entry

- [x] Normal Deploy Detects Post-commit Pending
- [x] Displays `--recover` Instruction
- [x] Does Not Build New Deployment Plan
- [x] Workspace Same Behavior

---

# 13. 修复后验收标准

v1.5.2 至少满足：

1. PREPARED 后 State 丢失时 Fail Closed；
2. PREPARED 后 `--full` 不改变 Operation Queue；
3. Source Delete 不会因 Resume Plan Drift 丢失；
4. Output Delete 不会因 Resume Plan Drift 丢失；
5. Config Policy Drift 被检测；
6. Pending Plan Hash 不一致时 Remote Mutation = 0；
7. STATE_COMPLETE Recovery 在空 Clone 中写入 Frozen State；
8. Cross-owner Casefold Collision 在连接前失败；
9. 工具不会创建下一次无法扫描的 Root Namespace；
10. Unicode Path 要么通过 Probe，要么明确拒绝；
11. Python 3.11/3.12 全量 CI；
12. Real Passive/Active FTP；
13. SFTP Hybrid 回归；
14. Native OpenSSH 回归；
15. Actual Target FTP Probe 和一次完整 Deployment。

> v1.5.2 落实说明：1–14 纳入本地/CI 自动门禁；第 15 项保持独立的可选人工兼容增强，需要目标账号、测试目录与人工确认。本轮未读取或记录真实凭据，也不以它反向阻塞已通过的 Mock/pyftpdlib 主线。

---

# 14. 当前使用建议

## 可以使用

```text
SFTP Staged Hybrid
Native OpenSSH Hybrid
Paramiko Hybrid
普通 FTP Incremental
FTP Hybrid 正常无中断部署
FTP Hybrid 同一 Clone/State 下的 Forward Resume
```

## FTP Hybrid v1.5.1 临时约束

如果出现：

```text
PREPARED
FILES_PUBLISHED
```

Pending：

- 不删除 `.git/git-deploy/<target>.json`；
- 不换机器；
- 不运行 `--full`；
- 不修改 Source Include/Exclude/Protect；
- 不修改 Incremental Output Mapping/Delete Policy；
- 保持原 HEAD 和 Local Build；
- 直接使用相同命令重跑。

如果出现：

```text
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

使用：

```bash
git-deploy TARGET --recover
```

如果在另一台机器执行 STATE_COMPLETE Cleanup：

- 先保留 Pending；
- 手工确认或复制 Local State；
- 或等待 v1.5.2 让 Recovery 幂等写入 Frozen State。

路径建议：

- Root 级所有 Source/Output/Hybrid 名称保持 NFC + Casefold 唯一；
- FTP 构建产物暂时优先使用 ASCII 文件名；
- 不由面板、CI、手工 FTP 同时修改受管路径。

---

# 15. 最终结论

v1.5.1 对 v1.5.0 审计的响应质量很高：

```text
Case-sensitive Capability
Schema 2 Reprobe
Portable Collision Gate
Orphan-safe Cleanup
Phase Ownership Matrix
Post-commit Explicit Recovery
Fresh MLSD Gates
```

上一轮所有明确 P0/P1/P2 均已关闭。

本轮没有发现新的 P0。

但 FTP Pending 仍没有冻结普通 Source/Incremental Deployment Plan，导致 Pre-commit Resume 在 State、Config 或 `--full` 发生变化时，可能漏掉历史删除并仍然推进 State。

此外，STATE_COMPLETE Recovery 没有在当前 Clone 幂等写入 Pending Frozen State，削弱跨机器恢复。

综合结论：

> **git-deploy v1.5.1 有条件通过。**

建议：

> **在相同 Clone、相同 State、相同配置下开始真实 FTP 项目验证；同时用一个小型 v1.5.2 完成 Pending Plan Contract 与 Cross-machine State 收口。**
