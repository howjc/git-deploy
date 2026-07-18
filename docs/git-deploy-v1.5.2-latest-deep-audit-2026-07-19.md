# git-deploy v1.5.2 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`655b9527583321e73e29f4b123e7027389118863`
> 功能提交：`973795513d011d86eb7da90fb58f89815a47ea78`
> PR：`#17 close FTP Hybrid contracts for v1.5.2`
> 版本：`v1.5.2`
> 审计日期：`2026-07-19`
> 审计结论：**有条件通过**
> 建议动作：发布一个极小的 `v1.5.3`，关闭 FTP 重连会话契约和远端未知根名称别名问题

---

# 1. 执行摘要

v1.5.2 对 v1.5.1 深度审计提出的主要整改项进行了完整收口：

1. FTP Pending 升级到 Schema 2；
2. Pending 冻结 `non_hybrid_plan_hash`；
3. Pending 冻结 `previous_state_hash`；
4. 普通 Source Upload 绑定内容 SHA256、Size、Git Path 和 Executable；
5. Incremental Output Upload 绑定内容 SHA256 和 Size；
6. Plan Hash 覆盖 Full、Previous Commit、Source Policy、Incremental Output Policy；
7. `PREPARED`、`FILES_PUBLISHED`、`PRUNED` 验证旧 State 和普通 Plan；
8. Legacy Pending Schema 1 的 Pre-commit Resume Fail Closed；
9. `FILES_PUBLISHED` 不再重放普通 Source/Incremental Operations；
10. `OWNERSHIP_COMMITTED` 和 `STATE_COMPLETE` 均幂等保存 Frozen State；
11. 普通部署在 Build 前检查 Post-commit Pending 并引导 `--recover`；
12. Workspace 在第一个 Build 前检查所有 FTP Post-commit Pending；
13. FTP Capability Profile 升级到 Schema 3；
14. 强制 FEAT `UTF8` 和 `OPTS UTF8 ON`；
15. Capability Probe 验证中文名称；
16. Capability Probe 验证 NFC/NFD 名称精确保留；
17. Capability Probe 验证 Unicode Rename/Delete；
18. Source、Incremental、Hybrid、历史受管根与 `.git-deploy` 统一使用 NFC + Casefold 根命名空间；
19. 历史 Git Commit 不可用时拒绝猜测 Source 根命名空间；
20. Python 3.11/3.12、Ruff、ty、构建和隔离安装通过；
21. Main 与 `v1.5.2` Tag 核心 Blob 一致。

上一轮两个 P1：

```text
Pre-commit Pending 普通 Plan 漂移
STATE_COMPLETE 跨机器不恢复 Frozen State
```

已经关闭。

上一轮两个 P2：

```text
跨 Owner Root Casefold
Unicode Path Semantics
```

也已实现。

但是，本轮发现两个新的 P1，均发生在 FTP 协议适配层，而不是 Pending 状态机：

## P1-01：FTP 重连后没有重新执行 `OPTS UTF8 ON`

Capability Probe 证明的是：

```text
当前 FTP Session 在执行 OPTS UTF8 ON 后
能够精确处理 Unicode 路径
```

但任何文件操作 Retry 都会：

```text
close old session
connect new session
直接重试命令
```

新 Session 没有重新执行：

```text
OPTS UTF8 ON
```

因此已经证明的 Unicode Session Contract 在网络重连后失效。

## P1-02：计划根名称没有与远端未知根条目做写前别名检查

v1.5.2 检查了：

```text
Source
Incremental
Hybrid
Historical Hybrid
.git-deploy
```

这些本地或已知受管根之间的 NFC + Casefold 冲突。

但没有把远端已有的未知根条目加入这次检查。

如果远端已有：

```text
Assets/
```

而本次计划创建：

```text
assets/
```

在大小写敏感服务器上两者可并存，但 FTPTransport 的严格 MLSD Policy 会拒绝两个名称同时出现。

当前流程可能先创建或发布 `assets`，然后在 Final Verify 时才检测到：

```text
Assets / assets collision
```

结果：

- Unknown `Assets` 没被删除；
- 新 `assets` 已经部分创建或发布；
- Pending 停留在 PREPARED；
- 后续读取 `.git-deploy` 也可能被根目录 Collision 阻断；
- 需要人工 FTP 清理刚创建的别名路径。

同样的问题适用于：

```text
.GIT-DEPLOY / .git-deploy
NFC / NFD 等价根名
Unicode Casefold 等价根名
```

因此 v1.5.2 尚不适合被标记为“无条件稳定基线”。

综合判断：

```text
Pending / Ownership / State Machine：
    通过

FTP Capability Initial Session：
    通过

FTP Retry Session：
    有缺口

Remote Mixed Root Alias Preflight：
    有缺口

整体：
    有条件通过
```

---

# 2. 审计范围

## 2.1 发布与 CI

- Main 最新 Commit；
- PR #17；
- PR Head；
- Merge Commit；
- `v1.5.2` Tag；
- Package Version；
- Main/Tag Blob；
- Python 3.11；
- Python 3.12；
- Lock Check；
- Tests；
- Ruff；
- ty；
- Wheel/sdist；
- Isolated Install；
- CLI Smoke；
- Review Threads。

## 2.2 Pending Schema 2

- Schema；
- Previous State Hash；
- Non-Hybrid Plan Hash；
- Local Manifest；
- HEAD；
- Previous/Next Ownership；
- Frozen State；
- Phase Matrix；
- Schema 1 Migration。

## 2.3 Stable Non-Hybrid Plan

- Source Upload；
- Source Delete；
- Output Upload；
- Output Delete；
- SHA256；
- Size；
- Executable；
- Full；
- Adoption；
- Previous Commit；
- Source Include；
- Source Exclude；
- Source Protect；
- Clean Worktree Policy；
- Incremental Mapping；
- Delete Policy。

## 2.4 Post-commit Recovery

- Early Pending Check；
- Project；
- Workspace；
- Broken Build；
- Missing `.deploy`；
- Corrupt/Missing State；
- New HEAD；
- Frozen State；
- STATE_COMPLETE；
- Cleanup。

## 2.5 FTP Path Semantics

- FEAT UTF8；
- OPTS UTF8 ON；
- ASCII Case；
- Chinese Name；
- NFC；
- NFD；
- Rename；
- Delete；
- MLSD Exact Name；
- Root Namespace；
- Unknown Remote Root；
- Retry Session。

## 2.6 回归

- SFTP Staged Hybrid；
- Native OpenSSH；
- Paramiko；
- FTP Incremental；
- Workspace；
- Doctor；
- Dry-run；
- Remote Plan；
- Recovery。

---

# 3. 审计方式与限制

通过 GitHub Connector 检查：

- 最新 Commit；
- PR Metadata；
- PR Diff；
- Main/Tag 文件；
- CI Run；
- Workflow Jobs；
- FTP Hybrid；
- FTP Transport；
- Planner；
- Deployer；
- Prepared；
- Workspace；
- Git Reader；
- Manifest；
- Unit Tests；
- Integration Tests；
- Release Notes；
- ADR。

本地尝试：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

当前执行环境仍然无法解析：

```text
github.com
```

错误：

```text
Could not resolve host: github.com
```

因此无法独立复跑：

```bash
uv lock --check
uv run --isolated --python 3.11 --all-groups pytest -q
uv run --isolated --python 3.12 --all-groups pytest -q
uv run ruff check src tests
uv run ty check src
uv build --clear
```

动态验证依据为成功的 GitHub Actions 和仓库中的真实 pyftpdlib、SFTP、Native OpenSSH 集成测试。

---

# 4. 版本、Tag 与 CI

## 4.1 Main

```text
655b9527583321e73e29f4b123e7027389118863
Merge pull request #17 from howjc/agent/v1.5.2-pending-contract-closeout
```

功能提交：

```text
973795513d011d86eb7da90fb58f89815a47ea78
close FTP Hybrid contracts for v1.5.2
```

## 4.2 PR

```text
PR #17
merged: true
```

PR 描述记录：

```text
Python 3.11: 258 passed
Python 3.12: 260 passed
Lock Check
Ruff
ty
Real FTP/SFTP/Native integration
Wheel/sdist
Isolated install
```

PR 无未解决 Review Thread。

## 4.3 Package

Main：

```toml
version = "1.5.2"
```

Tag：

```toml
version = "1.5.2"
```

Main/Tag `pyproject.toml` Blob：

```text
ca84a29f701ffe06867f493a49762ec7dd5c310e
```

一致。

## 4.4 FTP Hybrid

Main/Tag `ftp_hybrid.py` Blob：

```text
dc5750dcddc5f9559323a8acfa35285eb66c0fea
```

一致。

Main/Tag `ftp.py` Blob：

```text
f7bbabdfbdbda7f77774e45a72a51bcd49431916
```

一致。

Main/Tag `deployer.py` Blob：

```text
168394e1f02571d739b6a196543a56063b4c0136
```

一致。

---

# 5. v1.5.1 整改关闭情况

---

## 5.1 P1：Pre-commit Pending 普通 Plan 漂移

### 新 Schema

FTP Pending：

```text
Schema 2
```

新增：

```text
non_hybrid_plan_hash
previous_state_hash
```

Schema 2 Parser 要求两个字段都是完整 SHA256。

### Previous State Hash

覆盖：

- State Schema；
- Target；
- Target Fingerprint；
- Last Commit；
- Deployed At；
- Output Manifest。

State 为 None 时也有确定 Hash。

### Non-Hybrid Plan Hash

覆盖：

- HEAD；
- Previous Commit；
- Previous State Hash；
- Target Fingerprint；
- Full；
- Adoption；
- Operation Type；
- Origin；
- Remote Path；
- Git Path；
- Executable；
- SHA256；
- Size；
- Source Policy；
- Incremental Output Policy。

### Source Content

Source Upload 不再只记录 Git Path。

Planner 额外读取：

```text
Committed Blob SHA256
Committed Blob Size
```

Freeze 后重新计算 SHA256/Size，确保导出的 Git Blob 与 Plan Contract 一致。

### Resume

```text
PREPARED
FILES_PUBLISHED
PRUNED
```

验证：

- Local Manifest；
- HEAD；
- Non-Hybrid Plan；
- Previous State；
- Ownership Matrix。

### Legacy Pending

Schema 1：

```text
Pre-commit
    → Fail Closed

Post-commit
    → Explicit Recovery
```

### FILES_PUBLISHED

Resume 时不再重复普通 Source/Output Operations。

### 测试

覆盖：

- Missing State；
- Source Policy Drift；
- Plan Hash；
- Content Hash；
- FILES_PUBLISHED no replay；
- Schema 1；
- Schema 2 Strict Fields。

### 结论

**上一轮 P1 已关闭。**

---

## 5.2 P1：STATE_COMPLETE Cross-clone State

FTP Explicit Recovery 现在对：

```text
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

都先执行：

```python
state_store.save(pending.next_state)
```

再进行 Marker/Stage Cleanup。

因此：

```text
State 已经存在
    → 幂等重写

State 不存在
    → 恢复

State 损坏
    → 替换

当前 HEAD 已变化
    → 仍保存 Frozen Pending State
```

测试覆盖：

- Ownership Committed；
- Build 已损坏；
- `.deploy` 已删除；
- State Corrupt；
- New HEAD；
- State Complete；
- State File 删除；
- Frozen Commit 恢复。

### 结论

**上一轮 P1 已关闭。**

---

## 5.3 P2：跨 Owner Root Namespace

FTP Hybrid Local Plan 现在统一检查：

- 当前 Source；
- Historical Source；
- Current Incremental；
- Historical State Outputs；
- Hybrid Direct；
- `.git-deploy`。

使用：

```text
NFC(name).casefold()
```

作为根名称 Key。

如果旧 State Commit 在本地 Git 不可用：

```text
Fail Closed
```

而不是猜测历史 Source Root。

Remote Plan 再将：

- 当前 Non-Hybrid；
- 当前 Hybrid；
- Historical Hybrid；
- Internal；

做一次相同检查。

### 结论

**上一轮 P2 主问题已关闭。**

---

## 5.4 P2：Unicode Path Contract

Capability Profile：

```text
Schema 3
```

新增：

```text
utf8
unicode_paths
normalization_preserving
```

Probe 强制：

```text
FEAT UTF8
OPTS UTF8 ON
```

然后验证：

```text
中文文件名
中文 Rename
NFC Filename
NFD Filename
NFC/NFD 同时存在
NFC/NFD 内容独立
删除 NFD 不影响 NFC
MLSD Exact Names
```

Schema 1/2 Profile 必须重新 Probe。

### 结论

**上一轮 P2 初始 Session Contract 已关闭。**

---

## 5.5 普通部署 Post-commit Gate

普通 Project Deployment：

```text
非 Dry-run
非 Remote-plan
```

会在 Build 和 State Load 前只读检查 FTP Pending。

发现：

```text
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

立即提示：

```text
run --recover
```

Workspace 会在任何仓库 Build 前检查全部 Repository。

### 结论

**关闭。**

---

# 6. P1-01：FTP Retry 没有恢复 UTF-8 Session Contract

## 6.1 严重性

```text
级别：P1
类型：Protocol Session State Loss
影响：Unicode 文件上传、读取、删除、Rename 和 Resume
触发：连接抖动或任一 Retry
```

## 6.2 已证明的能力范围

Capability Probe 的证明顺序：

```text
Connect
FEAT
OPTS UTF8 ON
Set ftplib.encoding = utf-8
Unicode Operations
```

因此证明只对当前 Session 成立。

## 6.3 Retry 顺序

普通 Source/Incremental Retry：

```text
invalidate_connection
connect
ensure_root
retry operation
```

FTP Hybrid Stage/Publish/Delete Retry：

```text
invalidate_connection
connect
retry action
```

两条路径都没有：

```text
OPTS UTF8 ON
```

## 6.4 FTPTransport

`connect()` 只执行：

```text
TCP Connect
Login
Passive/Active
```

没有记忆：

```text
This transport requires UTF-8 on every new session
```

`enable_utf8()` 只修改当前 `ftplib.FTP` 实例。

连接失效后，新建的 `ftplib.FTP` 不继承 Server Session 的 OPTS 状态。

## 6.5 影响

对 ASCII 文件：

```text
通常没有可见影响
```

对 Unicode 文件：

- STOR 可能失败；
- RETR 可能失败；
- MLSD Decode 可能失败；
- Rename 可能失败；
- Delete 可能命中错误编码名称；
- Stage Verification 可能进入长期 PREPARED；
- Resume 每次重连都可能再次失败。

即使服务器通过 Capability Probe，也不能证明 Retry Session 满足同一能力。

## 6.6 为什么测试没有发现

现有 Connection Reset 测试使用：

```text
assets/reset.js
```

属于 ASCII 名称。

没有测试：

```text
Unicode 文件
第一次操作失败
第二次连接必须再次收到 OPTS UTF8 ON
```

## 6.7 修复建议

### Sticky Session Requirement

FTPTransport 增加：

```python
self._require_utf8 = False
```

首次成功调用：

```python
enable_utf8()
```

后设置：

```python
_require_utf8 = True
```

之后每次 `connect()` 登录完成后：

```text
如果 _require_utf8:
    FEAT/OPTS UTF8 ON
    encoding = utf-8
```

`close()` / `invalidate_connection()` 不清除 `_require_utf8`。

### 可选加强

重连后同时验证：

```text
Server Banner Hash
UTF8 Feature
```

若后端发生变化：

```text
Fail Closed
```

## 6.8 测试

- Unicode Stage Upload Retry；
- Unicode Final Publish Retry；
- Unicode Orphan Delete Retry；
- Unicode Root File Retry；
- Ordinary Source Unicode Retry；
- Ordinary Incremental Unicode Retry；
- New Session 收到 OPTS；
- OPTS Reconnect Failure；
- Banner Drift；
- Passive；
- Active。

---

# 7. P1-02：远端未知根名称没有进入写前 Namespace Gate

## 7.1 严重性

```text
级别：P1
类型：Mixed Root Preflight Incompleteness
影响：Partial Mutation + Pending Trap
```

## 7.2 当前检查

Local：

```text
Source
Historical Source
Incremental
State Outputs
Hybrid
Internal
```

Remote：

```text
Non-Hybrid Managed
Current Hybrid
Historical Hybrid
Internal
```

未包含：

```text
远端已有但不受管的 Direct Root Entries
```

## 7.3 `lstat()` 行为

FTP `lstat("assets")`：

1. MLSD Root；
2. 检查 Root Listing 内是否已经存在两条碰撞名称；
3. 按 Exact String 查找 `assets`；
4. 如果只有 `Assets`，返回 Missing。

它不会报告：

```text
Requested "assets" aliases existing unknown "Assets"
```

## 7.4 目录复现

远端：

```text
Assets/
└── backend-data
```

本地 Hybrid：

```text
assets/
└── app.js
```

Remote Plan：

```text
lstat("assets") = Missing
```

用户确认后：

```text
Create assets/
```

此时 Root 同时存在：

```text
Assets/
assets/
```

下一次 Stage Final Verification 读取 Root MLSD 时，严格 Collision Gate 报错。

结果：

- `Assets/` 保留；
- `assets/` 已创建；
- Pending PREPARED；
- Future Plan/Recovery 可能因 Root MLSD Collision 无法读取 `.git-deploy`；
- 必须人工删除 `assets/`。

## 7.5 Root File 复现

远端：

```text
Index.html
```

本地 Hybrid：

```text
index.html
```

流程可能在：

```text
Stage → Final Rename
```

后才报 Collision。

这意味着业务文件已经发布，但：

- Final Verify 失败；
- Ownership 不推进；
- State 不推进；
- Future Resume 被 Root Collision 阻断。

## 7.6 Internal 复现

远端未知：

```text
.GIT-DEPLOY/
```

Capability Probe 或首次部署计划创建：

```text
.git-deploy/
```

当前 Probe 没有在创建前对 Remote Root Unknown Names 做 Alias 检查。

Probe 可能自己制造：

```text
.GIT-DEPLOY
.git-deploy
```

随后在读取 Probe 文件时失败，并且 Cleanup 也可能被同一 Collision 阻断。

## 7.7 修复建议

### Read-only Root Alias Gate

在 Capability Probe 写入前：

```text
MLSD remote root
Validate planned ".git-deploy" against all existing root entries
```

在 FTP Remote Plan 中：

```text
MLSD remote root
```

计算：

```text
Managed Planned Root Keys
+
Internal Root Key
```

对每个 Remote Root Entry：

```text
NFC + casefold 相同
Exact Name 不同
    → Fail Closed
```

Exact Name 相同：

- Hybrid 继续走 Adoption/Ownership；
- Source/Incremental 保持现有语义；
- `.git-deploy` 继续读取内部元数据。

无关 Unknown Root：

```text
保留且忽略
```

### 不需要递归扫描未知内容

只读取：

```text
Remote Root Direct Entries
```

不会扩大删除范围，也不违反 Unknown Root Preservation。

## 7.8 测试

- Unknown `Assets` vs Planned `assets`；
- Unknown `Index.html` vs Planned `index.html`；
- Unknown NFC vs Planned NFD；
- Unknown `.GIT-DEPLOY` vs Internal `.git-deploy`；
- Probe Zero Mutation；
- Plan Zero Mutation；
- Unknown Exact Name Adoption；
- Unrelated Unknown Root Preserved；
- Workspace Later Repository Alias。

---

# 8. P2-01：Source Blob Hash 对所有 Backend 产生性能回归

## 8.1 当前实现

每个 Source Upload：

```text
git cat-file blob <commit:path>
读取完整 Blob 到 Python 内存
SHA256
```

之后 Freeze：

```text
再次 export Blob
再次 SHA256
```

这对以下全部目标执行：

- 普通 SFTP；
- Native OpenSSH；
- FTP Incremental；
- SFTP Hybrid；
- FTP Hybrid。

但稳定 Non-Hybrid Plan Hash 实际只用于：

```text
FTP Hybrid Pending Resume
```

## 8.2 影响

First/Full Deployment 中，若 Source 文件很多：

- 每个文件一个 Git 子进程；
- 每个 Blob 至少读取两次；
- 大 Blob 完整进入内存；
- SFTP/普通 FTP 也承担额外成本。

## 8.3 修复建议

只对：

```text
Target = FTP
and Hybrid exists
```

生成 Stable Operation Content Contract。

更高效的实现：

1. Git `cat-file --batch`；
2. 在 `ls-tree` 中保留 Blob OID；
3. Plan Hash 使用 Blob OID + Size；
4. Freeze 仍从 exact commit 导出；
5. 需要 SHA256 时流式读取，而不是一次性 `_run()` 捕获。

## 8.4 定级

```text
P2
```

不影响删除安全，但可能明显影响大型仓库首发性能和内存稳定性。

---

# 9. P2-02：Legacy Post-commit Recovery 依赖 Schema 3 Profile

v1.5.2 声明：

```text
Legacy Pending Schema 1 Post-commit 可恢复
```

但 FTP Explicit Recovery 仍要求：

```text
Capability Profile Schema 3
UTF8
Unicode/Normalization Probe
```

如果 v1.5.1 的服务器：

- 通过 Schema 2；
- 不支持 UTF8；
- 存在 STATE_COMPLETE Pending；

升级到 v1.5.2 后无法使用当前版本完成 Cleanup/State Recovery。

用户只能：

- 临时安装 v1.5.1；
- 人工保存 Pending State；
- 人工清理内部目录。

这属于安全保守失败，不是数据破坏问题。

建议至少在错误中明确：

```text
Legacy Pending recovery requires v1.5.1 or a successful Schema 3 re-probe.
```

未来可提供：

```text
ASCII metadata-only legacy recovery
```

但不是 v1.5.3 必须项。

---

# 10. P3：Remote-plan 对 Post-commit Pending 仍会先 Build

Early Post-commit Gate 当前只用于：

```text
普通 Deploy
```

不用于：

```text
--remote-plan
```

用户面对 Post-commit Pending 时运行：

```bash
git-deploy prod --remote-plan
```

仍可能：

- 运行 Build；
- 扫描 Local Hybrid；
- 冻结文件；
- 最后才遇到 Pending/Local Mismatch。

更统一的行为是：

```text
所有非 dry-run 命令
先做 Post-commit Pending Read-only Gate
```

或者新增真正的：

```text
--recovery-plan
```

当前只是 UX/效率问题。

---

# 11. 已确认正确的能力

## 11.1 Pending

- Schema 2；
- Strict Fields；
- State Hash；
- Plan Hash；
- Ownership Matrix；
- HEAD；
- Local Manifest；
- Legacy Pre-commit Fail Closed；
- Legacy Post-commit Recovery；
- FILES_PUBLISHED no replay。

## 11.2 Frozen State

- Ownership Committed；
- State Complete；
- Missing State；
- Corrupt State；
- New Clone；
- New HEAD；
- Broken Build；
- Missing Local Hybrid。

## 11.3 Path Capability

- Case Sensitive；
- UTF8 Advertisement；
- OPTS Acceptance；
- Chinese；
- NFC；
- NFD；
- Exact MLSD；
- Rename；
- Delete；
- Profile Schema 3。

## 11.4 Root Namespace

- Current Source；
- Historical Source；
- Current Incremental；
- Historical Outputs；
- Hybrid；
- Historical Hybrid；
- Internal；
- Historical Commit Existence。

## 11.5 Other

- Unknown Root Deletion Boundary；
- Adoption；
- State Loss Hybrid Ownership；
- Upload-first；
- Prune-last；
- Ownership-last；
- State-after-ownership；
- Orphan Stage Doctor；
- SFTP Regression；
- Native Rename Safety；
- Passive/Active FTP。

---

# 12. v1.5.3 原子 TODO

## P1：Sticky UTF-8 Session

### TODO-001：Transport State

- [x] 增加 `_require_utf8`
- [x] `enable_utf8()` 成功后设为 True
- [x] `close()` 不清除
- [x] `invalidate_connection()` 不清除

### TODO-002：Connect Hook

- [x] Login 后检查 `_require_utf8`
- [x] 重新 FEAT UTF8
- [x] 重新 OPTS UTF8 ON
- [x] 设置 `encoding = utf-8`
- [x] Failure Fail Closed

### TODO-003：Retry Tests

- [x] Source Unicode Upload
- [x] Incremental Unicode Upload
- [x] Hybrid Stage Unicode
- [x] Hybrid Publish Unicode
- [x] Unicode Delete
- [x] Unicode RMD
- [x] Passive
- [x] Active

---

## P1：Remote Root Alias Gate

### TODO-101：Root Snapshot

- [x] MLSD `.`
- [x] Stable Exact Names
- [x] NFC + Casefold Index

### TODO-102：Plan Union

- [x] Source Root
- [x] Incremental Root
- [x] Hybrid Root
- [x] Historical Root
- [x] `.git-deploy`

### TODO-103：Remote Alias

- [x] Exact same allowed
- [x] Normalized alias rejected
- [x] Unknown unrelated ignored
- [x] Zero mutation

### TODO-104：Probe Internal Alias

- [x] `.GIT-DEPLOY`
- [x] Unicode equivalent
- [x] Fail before MKD
- [x] No leftover `.git-deploy`

---

## P2：Performance

### TODO-201：Conditional Content Contract

- [x] Only FTP Hybrid
- [x] Ordinary SFTP no extra Source hash
- [x] FTP Incremental no extra Source hash
- [x] SFTP Hybrid no extra Source hash

### TODO-202：Batch Git

- [x] Blob OID in GitEntry
- [x] Batch Size
- [x] Streaming Hash
- [x] Large Blob Test
- [x] 10k File Benchmark

> v1.5.3 落实说明：TODO-001 至 TODO-202 已纳入本地自动门禁；10k 文件本机基准为 0.822 秒（约 12,169 files/s）。验收项 15/16 的实际目标 FTP Probe 与 Canary Deployment 仍是独立的可选人工增强，需要测试账号、隔离目录与人工确认；本轮未读取或记录真实凭据，也不以它们反向阻塞 Mock/pyftpdlib 主线。

---

# 13. 修复后验收标准

v1.5.3 至少满足：

1. Retry 新 Session 再次发送 OPTS UTF8 ON；
2. Unicode Upload Retry 成功；
3. Unicode Delete Retry 成功；
4. OPTS Retry Failure 不执行后续业务命令；
5. Unknown `Assets` / Planned `assets` 在 Remote Plan 失败；
6. Unknown `.GIT-DEPLOY` 在 Probe 前失败；
7. Alias Failure Remote Mutation = 0；
8. Exact Existing Hybrid Name 仍走 Adoption；
9. Unrelated Unknown Root 保留；
10. Main/Tag Blob 一致；
11. Python 3.11/3.12；
12. Passive/Active FTP；
13. SFTP Hybrid；
14. Native OpenSSH；
15. Actual Target FTP Probe；
16. Actual Target Canary Deployment。

---

# 14. 当前版本使用建议

## 可以使用

```text
SFTP Staged Hybrid
Native OpenSSH Hybrid
Paramiko Hybrid
FTP Incremental
FTP Hybrid ASCII 路径的稳定网络部署
FTP Hybrid Pending Schema 2 Resume
FTP Hybrid Post-commit Cross-clone Recovery
```

## v1.5.2 FTP Hybrid 临时约束

### 路径

在 Remote Root 中人工确认不存在：

```text
计划根名称的大小写异形
计划根名称的 NFC/NFD 异形
.GIT-DEPLOY 等 Internal Alias
```

例如本地要部署：

```text
assets/
```

确认远端没有：

```text
Assets/
ASSETS/
```

### 网络

部署包含中文或非 ASCII 路径时：

- 尽量在稳定连接下执行；
- 若发生 Retry 后失败，保留 Pending；
- 不立即手工删除业务路径；
- 重新运行前确认 Stage/Final；
- 必要时使用 ASCII 临时名称。

### Probe

升级到 v1.5.2 后必须：

```bash
git-deploy doctor prod --probe-ftp-hybrid
```

然后先执行：

```bash
git-deploy prod --remote-plan
```

再进行 Canary Deployment。

### 单发布器

继续禁止：

- CI 与本地同时发布；
- 面板与 git-deploy 同时修改；
- 手工 FTP 同时修改受管目录；
- 多台机器同时部署。

---

# 15. 最终结论

v1.5.2 的主要目标已经达成：

```text
Pending Stable Plan Contract
Previous State Contract
FILES_PUBLISHED No Replay
Cross-clone Frozen State
All-owner Root Namespace
UTF8/NFC/NFD Capability
```

Pending、Ownership 与 State 的核心状态机已经具备较高可信度。

本轮没有发现新的 P0。

但 FTP 的 Unicode 能力目前只绑定到初始 Session，连接 Retry 后没有自动恢复；同时 Remote Root 的未知名称没有参与计划名称 Alias Gate，可能让工具先创建冲突路径再被自己的严格 MLSD Policy 阻断。

因此：

> **git-deploy v1.5.2 有条件通过。**

建议：

> **不要扩展新功能，仅发布 v1.5.3，修复 Sticky UTF-8 Session 和 Remote Unknown Root Alias Preflight；完成后 FTP Hybrid 可以进入稳定维护阶段。**
