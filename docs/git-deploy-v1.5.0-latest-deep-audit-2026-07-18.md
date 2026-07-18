# git-deploy v1.5.0 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`76138a95d92b022f6316344029182747f3cd3d75`
> 功能提交：`45664315707638a231212c3b7e9d554208e2c32f`
> PR：`#15 feat: add FTP in-place hybrid deployment`
> 版本：`v1.5.0`
> 审计日期：`2026-07-18`
> 审计结论：**不通过——FTP Hybrid 需要 v1.5.1 安全收口**
> SFTP Staged Hybrid：**未发现本次改动引入的阻断回归**
> 普通 FTP Incremental：**未发现本次改动引入的阻断回归**

---

# 1. 执行摘要

v1.5.0 已经完成 FTP In-place Hybrid 的主体能力，而且实现方向与迭代方案基本一致：

- 相同 `mode = "hybrid"` 根据协议选择 SFTP Staged 或 FTP In-place Backend；
- 显式 FTP Capability Probe；
- 本地 Capability Profile；
- FEAT/MLSD、Binary STOR/RETR、跨目录 Rename、Rename Replace、DELE、RMD 探测；
- MLSD-only Typed Scan；
- Remote Ownership Manifest；
- PREPARED → FILES_PUBLISHED → PRUNED → OWNERSHIP_COMMITTED → STATE_COMPLETE；
- Root File 与 Mirror File Stage；
- Stage RETR SHA256 校验；
- Rename Replace；
- Final RETR SHA256 校验；
- Upload-first；
- Prune-last；
- Ownership-last；
- State-after-ownership；
- Local State 丢失后的 Remote Ownership 恢复；
- 首次 `--full` Adoption；
- Pending Forward Resume；
- Passive/Active FTP；
- Doctor、Dry-run、Remote Plan 与 Workspace；
- SFTP Hybrid Schema 和主链兼容。

实现不是简单解除 FTP 禁止，而是增加了独立的协议能力模型和状态机，这是正确方向。

但是，本轮发现一个发布阻断级正确性问题和一个高优先级恢复问题：

## P0：未验证 FTP 路径大小写语义

当前 FTP Capability Probe 没有验证服务器是否大小写敏感，Local Scanner、Remote Scanner 和 Planner 又按 Python 字符串进行大小写敏感集合比较。

在大小写不敏感的 FTP 文件系统中：

```text
assets/App.js
assets/app.js
```

可能指向同一个远端文件。

工具可能：

- 将两者视为两个不同 Local 文件；
- 分别 Stage 和 Final Verify；
- 最终只保留其中一个；
- 仍然提交 Ownership 和 Local State；
- 或把远端 `app.js` 规划为 Orphan，在发布 `App.js` 后再删除同一个底层文件。

因此一个能够通过当前 Capability Probe 的服务器，仍可能无法忠实表达 Local Hybrid Manifest。

这是 FTP Hybrid 的发布阻断问题。

## P1：共享 Stage 父目录会让 Pending 永久卡住

执行器会：

```text
创建 stage/<deployment-id>
写 PREPARED Pending
```

如果 Pending 首次写入失败，可能留下没有 Pending Marker 的孤儿 Stage Root。

后续一次部署即使文件、Ownership 和 State 全部成功，Cleanup 仍会尝试删除共享父目录：

```text
.git-deploy/ftp-hybrid/stage
```

只要其中存在旧孤儿目录，RMD 就会失败；异常发生在删除当前 Pending Marker 之前，于是当前 Marker 停留在 `STATE_COMPLETE`。

后续每次重跑都会再次被同一个孤儿目录阻断，形成永久 Cleanup Loop，除非人工进入 FTP 删除孤儿目录。

## P2：后提交阶段仍被当前 Build/HEAD 强绑定

`OWNERSHIP_COMMITTED` 和 `STATE_COMPLETE` 已经不需要当前 Build：

- `OWNERSHIP_COMMITTED` 只需保存 Pending 中冻结的 `next_state`；
- `STATE_COMPLETE` 只需清理内部 Stage 和 Pending。

但当前 Planner 对所有 Pending Phase 都要求：

```text
当前 Local Manifest Hash = Pending Local Manifest Hash
当前 HEAD = Pending HEAD
```

如果用户在 State/Cleanup 失败后继续开发或重新构建，工具会拒绝完成已经提交的旧部署。

## P2：FTP Freshness 的“重新读取”可能命中旧 MLSD Cache

`FTPTransport.list_directory_typed()` 会缓存 MLSD 结果。

Remote Plan 与正式执行复用同一个 FTPTransport。Freshness 方法没有在读取前显式清空缓存，因此部分 `lstat()` 和 Tree Scan 可能比较的是 Remote Plan 时的缓存，而不是服务器当前事实。

普通 PREPARED/FILES_PUBLISHED 流程通常会因为 Pending/Stage 写入清空缓存，并在 Publish 前再做一次校验，所以主路径大多仍然安全；但初始 Freshness 契约并不真实，PRUNED/后期 Resume 也没有完整的第二次 Tree Gate。

---

# 2. 审计范围

## 2.1 发布完整性

- Main 最新 Commit；
- PR #15；
- `v1.5.0` Tag；
- Main/Tag Package Version；
- Main/Tag FTP Hybrid Blob；
- Main/Tag FTP Transport Blob；
- GitHub Actions；
- Python 3.11；
- Python 3.12；
- Ruff；
- ty；
- Lockfile；
- Wheel/sdist；
- Isolated Wheel Install；
- CLI Smoke；
- PR Review 状态。

## 2.2 FTP Capability

- FEAT；
- MLSD；
- Binary STOR/RETR；
- Zero-byte；
- Cross-directory Rename；
- Rename Replace；
- DELE；
- RMD；
- Probe Cleanup；
- Target Fingerprint；
- Server Banner Hash；
- Profile Atomic Store；
- Passive/Active。

## 2.3 Remote Scanner

- MLSD Facts；
- File/Directory；
- cdir/pdir；
- Unknown Type；
- Symlink；
- Unsafe Name；
- Size；
- Recursive Tree；
- Empty Directory；
- Maximum Depth；
- Maximum Entries；
- Permission Error；
- Cache Invalidation。

## 2.4 Ownership 与 Pending

- Ownership Schema 1；
- Project/Mapping/Remote Identity；
- Pending Schema；
- Frozen State；
- Previous/Next Ownership Hash；
- Local Manifest Hash；
- HEAD；
- Target Fingerprint；
- Phase；
- Bounded Read；
- Stage/Final Verify。

## 2.5 Planner

- Backend Resolve；
- Adoption；
- Direct Type Change；
- Nested Type Change；
- Root File Upload；
- Mirror Full Upload；
- Orphan File；
- Orphan Directory；
- Historical Directory；
- State Loss；
- Pending Resume；
- Remote Tree Snapshot；
- Large Delete Warning；
- Unknown Root Preservation。

## 2.6 Executor

- PREPARED；
- FILES_PUBLISHED；
- PRUNED；
- OWNERSHIP_COMMITTED；
- STATE_COMPLETE；
- Stage Verify；
- Publish Retry；
- Rename Uncertain Outcome；
- Prune Retry；
- Ownership Publish；
- State Save；
- Cleanup；
- Ctrl-C；
- Connection Reset。

## 2.7 回归

- SFTP Hybrid；
- Native OpenSSH；
- Paramiko；
- FTP Incremental；
- Workspace；
- `after_deploy`；
- Source/Output Conflict；
- Local State。

---

# 3. 审计方式与限制

本轮通过 GitHub Connector 读取：

- Commit；
- PR Metadata；
- PR Diff；
- Main/Tag 文件；
- CI Run 与 Job Steps；
- FTP Hybrid 核心源码；
- FTP Transport；
- Planner；
- Deployer；
- Config；
- Prepared；
- Doctor；
- CLI；
- Workspace；
- Unit Tests；
- Real Transport Integration Tests；
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

因此本轮无法独立复跑：

```bash
uv lock --check
uv run --isolated --python 3.11 --all-groups pytest -q
uv run --isolated --python 3.12 --all-groups pytest -q
uv run ruff check src tests examples
uv run ty check src
uv build --clear
```

动态验证依据为成功的 GitHub Actions 和仓库中的 pyftpdlib / SFTP 真实集成测试；安全结论来自当前 Main 的独立源码审计。

---

# 4. 版本、Tag 与 CI

## 4.1 Main

```text
76138a95d92b022f6316344029182747f3cd3d75
Merge pull request #15 from howjc/agent/v1.5.0-ftp-in-place-hybrid
```

功能提交：

```text
45664315707638a231212c3b7e9d554208e2c32f
feat: add FTP in-place hybrid deployment
```

## 4.2 Package

Main：

```toml
version = "1.5.0"
```

Tag `v1.5.0`：

```toml
version = "1.5.0"
```

`pyproject.toml` Blob：

```text
a0c456e7d823b5a434f1020b7c5907615c5eee73
```

一致。

`src/git_deploy/__init__.py`：

```python
__version__ = "1.5.0"
```

Main/Tag Blob：

```text
09e4633b1638a8b9bca4236f659a521e38bbf9aa
```

一致。

## 4.3 FTP Hybrid

Main/Tag `src/git_deploy/ftp_hybrid.py` Blob：

```text
a89e288253c4de7cdd34dec5d70073de764a20dc
```

一致。

Main/Tag `src/git_deploy/transports/ftp.py` Blob：

```text
a1457bc0ca512e84903f88296ffffba570da44f7
```

一致。

## 4.4 CI

PR Head CI：

```text
status: completed
conclusion: success
```

Python 3.11 和 Python 3.12 Job 均通过：

- Interpreter Check；
- Lockfile Check；
- Dependency Install；
- Tests；
- Ruff；
- ty；
- Build Package；
- Isolated Wheel Install；
- CLI Version/Help Smoke。

PR 记录：

```text
Python 3.11 full suite: 245 passed
Python 3.12 core/Hybrid/transport: 95 passed
```

---

# 5. 已正确实现的能力

## 5.1 Backend 选择

```text
SFTP
    → SFTP_STAGED

FTP
    → FTP_IN_PLACE
```

配置继续使用统一：

```toml
mode = "hybrid"
```

没有增加用户需要理解的 `ftp-hybrid` Mode。

## 5.2 FTP after_deploy

FTP Target 配置层明确拒绝：

```text
after_deploy
command_timeout
SSH Alias/Key/Agent
```

不存在命令被静默忽略的问题。

## 5.3 Capability Profile

Profile 严格包含：

- Target Fingerprint；
- Server Banner Hash；
- MLSD；
- RETR；
- Cross-directory Rename；
- Rename Replace；
- Delete File；
- Remove Directory；
- Probe Timestamp。

Profile：

- 本地原子写；
- 不含密码；
- 绑定 Target；
- 绑定 Banner；
- 任一 Capability False 即拒绝。

## 5.4 显式 Probe

Probe 只写：

```text
.git-deploy/ftp-probe/<random-id>
```

验证：

- Binary Round-trip；
- Zero-byte；
- MLSD File/Directory；
- Empty Directory；
- Cross-directory Rename；
- Rename Replace；
- Source Consumed；
- DELE；
- RMD。

CLI 会提示临时写入，并在交互环境确认；非交互要求 `--yes`。

## 5.5 MLSD-only

Hybrid 不使用 `LIST` 文本解析，也不使用 `NLST + CWD` 猜测类型。

允许：

```text
file
dir
cdir
pdir
```

其中 cdir/pdir 被跳过。

拒绝：

```text
symlink
OS.unix=slink
unknown
missing type
unsafe name
invalid size
```

权限/MLSD 失败不会解释成 Missing 或 Empty。

## 5.6 扫描范围

只扫描：

```text
Current Hybrid Directories
+
Historical Ownership Directories
```

不扫描完整项目根目录。

未知：

```text
index.php
.env
uploads/
app/
```

不进入 Remote Tree，也不进入删除集合。

## 5.7 Upload-first / Prune-last

执行顺序正确：

```text
Pending PREPARED
普通 Source/Incremental Operations
所有 FTP Hybrid File Stage
所有 Stage RETR Verify
Freshness
创建当前目录
发布并 Final Verify 当前文件
Pending FILES_PUBLISHED
删除 Orphan Files
删除 Orphan Directories
Pending PRUNED
Ownership
Pending OWNERSHIP_COMMITTED
State
Pending STATE_COMPLETE
Cleanup
```

当前文件全部完成前不会 Prune。

## 5.8 Root 与 Mirror

Root Files：

- Full；
- State Hash 变化；
- Remote Missing；
- Remote Type 异常；
- Adoption；
- Pending Resume；

时上传。

Mirror Files 每次全部上传。

这符合 FTP 无远端内容 Hash 的设计。

## 5.9 Type Change

FTP Hybrid 拒绝：

```text
Direct File → Directory
Direct Directory → File
Nested File → Directory
Nested Directory → File
```

避免先删除旧类型而没有回滚能力。

## 5.10 Forward Resume

Pending 严格绑定：

- Project ID；
- Mapping；
- Remote；
- Target Fingerprint；
- Deployment ID；
- Previous Ownership Hash；
- Next Ownership Hash；
- Local Manifest Hash；
- HEAD；
- Frozen TargetState；
- Phase。

实现覆盖：

- Stage Upload Failure；
- Stage Verify Failure；
- Publish Failure；
- Prune Failure；
- RMD Failure；
- Ownership Failure；
- State Failure；
- Cleanup Failure；
- Ctrl-C；
- Connection Retry。

## 5.11 State Loss

Local State 丢失后：

- Remote Ownership 继续提供顶层删除事实；
- Mirror Remote Tree 继续提供内部 Orphan；
- Unknown Root 保留；
- 不要求重新接管已拥有路径。

## 5.12 Real Integration

真实 pyftpdlib 测试覆盖：

- Passive；
- Active；
- Probe；
- Adoption；
- Mixed Root；
- Unknown Preservation；
- Mirror；
- Empty Directory；
- State Loss；
- Historical Directory；
- 每个 Pending Phase；
- Retry；
- Cleanup。

---

# 6. P0-01：没有证明 FTP 文件系统大小写敏感

## 6.1 严重性

```text
级别：P0
影响：FTP Hybrid Mirror 正确性
结果：工具可能报告成功，但远端无法表达 Local Manifest
```

## 6.2 根因

Capability Profile 没有：

```text
case_sensitive_paths
```

Probe 创建的名称也没有大小写碰撞测试。

Local Scanner 和 Planner 使用精确字符串集合：

```text
App.js != app.js
```

但 FTP 服务端文件系统不一定如此。

常见风险环境：

- Windows FTP；
- IIS FTP；
- 大小写不敏感 NAS；
- 映射到大小写不敏感卷的 FTP Server；
- 某些托管 FTP 服务。

这些服务器可能完整通过当前 Probe：

- MLSD；
- RETR；
- Rename；
- Rename Replace；
- DELE；
- RMD；

但仍无法同时存储：

```text
App.js
app.js
```

## 6.3 明确失败场景一：Local Case Collision

Local：

```text
assets/App.js
assets/app.js
```

Planner 产生两个 Upload。

远端 Stage 在大小写不敏感服务器上可能把两个路径映射到同一个文件。

每次单文件 Stage/Final Verify 都可能成功，但最终只存在一个文件。

Ownership 只记录顶层：

```text
assets/
```

不会记录 Mirror 内部的两个文件名，因此部署仍可提交：

```text
Ownership
State
```

结果：

```text
部署显示成功
远端 Mirror != Local Mirror
```

## 6.4 明确失败场景二：大小写重命名

历史 Remote Tree：

```text
assets/app.js
```

当前 Local：

```text
assets/App.js
```

Planner 按精确字符串计算：

```text
UPLOAD assets/App.js
DELETE assets/app.js
```

在大小写不敏感服务器上，两条路径可能引用同一个底层文件。

根据服务器的大小写保留方式，可能：

- 发布后删除刚发布的文件；
- 只保留一个名称但工具误认为已完全收敛；
- 每次部署反复 Upload/Delete；
- State 已成功但远端文件缺失。

## 6.5 为什么现有测试没有发现

所有真实 FTP 测试使用：

```text
pyftpdlib
Linux 临时目录
```

该环境大小写敏感。

当前测试没有：

- Case-insensitive Filesystem Fixture；
- Case-only Rename；
- Local Casefold Collision；
- Remote Casefold Collision；
- Capability Case Probe。

## 6.6 必须修复

### Capability Schema 2

增加：

```python
case_sensitive_paths: bool
```

### Probe

在同一目录创建：

```text
CaseProbe.bin = A
caseprobe.bin = B
```

验证：

1. MLSD 同时返回两个独立名称；
2. RETR 分别返回 A/B；
3. 删除其中一个不影响另一个；
4. Case-only Rename 行为明确。

如果失败：

```text
FTP Hybrid unsupported:
remote filesystem is case-insensitive
```

### Local Collision Gate

无论 Probe 结果如何，建议拒绝同一目录中：

```text
normalize(NFC, name).casefold()
```

重复的 Local Entry。

这保持跨平台可移植性。

### Remote Collision Gate

MLSD 每个目录必须拒绝：

- 完全重复名称；
- NFC + casefold 冲突名称；
- Server 返回的大小写不稳定名称。

## 6.7 必须新增测试

- Case-sensitive Probe Success；
- Case-insensitive Probe Failure；
- Local `App.js/app.js` Reject；
- Root `Index.html/index.html` Reject；
- Remote MLSD Case Collision Reject；
- Case-only Rename；
- Capability Profile Schema Migration；
- Real Windows/IIS 或 Case-insensitive Fake Filesystem。

---

# 7. P1-01：孤儿 Stage 会让后续 Pending 永久卡死

## 7.1 当前顺序

首次执行：

```text
_ensure_ftp_internal_directories()
    创建 stage/<deployment-id>

_write_ftp_pending(PREPARED)
```

如果 `_write_ftp_pending()` 失败：

```text
stage/<deployment-id> 可能已存在
Pending Marker 可能不存在
```

没有对应 Cleanup。

## 7.2 后续成功部署

后续 Deployment 使用新的 ID，并最终进入：

```text
STATE_COMPLETE
```

Cleanup 顺序：

```text
remove_tree(current stage/<id>)
remove_directory(shared stage parent)
delete current Pending Marker
```

如果旧孤儿 Stage 存在：

```text
remove_directory(.git-deploy/ftp-hybrid/stage)
    → Non-empty
    → Exception
```

由于异常发生在 Pending 删除之前：

```text
当前 Pending 保留在 STATE_COMPLETE
```

## 7.3 永久循环

下一次重跑：

```text
创建当前 Stage Root
进入 STATE_COMPLETE Cleanup
删除当前 Stage Root
删除 shared stage parent
    → 仍被旧孤儿阻断
当前 Pending 仍无法删除
```

除非人工删除旧孤儿目录，否则无法完成。

## 7.4 触发条件

只需一次：

- Pending 初次写入网络失败；
- Pending RNTO 不确定失败；
- 进程在创建 Stage 后、Pending 完成前中断；
- 旧版本留下内部目录；
- 人工复制内部 Stage；
- 另一个失败 Probe/部署留下受保护残留。

FTP 本身网络不稳定，这不是理论上的极端场景。

## 7.5 修复

### 不把共享 Parent RMD 作为成功条件

Cleanup 应改为：

```text
remove_tree(current stage/<deployment-id>)
delete current Pending Marker
best-effort remove shared stage parent if empty
```

共享 Parent 非空：

```text
不影响当前 Deployment 完成
```

### Pending 初写失败 Cleanup

```python
try:
    write PREPARED
except:
    best_effort remove current stage_root
    raise
```

### Doctor

增加：

```text
FTP Hybrid Orphan Stage
```

列出：

- Stage ID；
- 是否有对应 Pending；
- Age；
- Entry Count。

首版可以只报告，不自动删除。

### 可选 GC

显式：

```bash
git-deploy doctor TARGET --cleanup-ftp-hybrid-internal
```

不是 v1.5.1 必需；不应静默删除。

## 7.6 Probe 同类问题

Capability Probe Cleanup 也要求删除共享：

```text
.git-deploy/ftp-probe
```

如果存在旧 Probe Sibling，新 Probe 即使所有能力验证成功也会因 Parent 非空而失败。

同样应：

- 强制删除本次 Random Probe Root；
- Shared Parent RMD 只做 Best-effort。

## 7.7 测试

- PREPARED Marker Write Failure；
- Stage Root Orphan；
- Next Deployment Success；
- Shared Parent Non-empty；
- Pending Marker 正常删除；
- Doctor Reports Orphan；
- Probe Sibling Exists；
- Probe Capability Still Succeeds；
- Current Probe Root Cleans。

---

# 8. P2-01：后提交 Pending 不应依赖当前 Local Manifest 与 HEAD

## 8.1 当前规则

所有 Pending Phase 都调用：

```text
pending.local_manifest_hash == current_manifest_hash
pending.head == current_head
```

## 8.2 合理阶段

以下阶段必须要求精确 Local：

```text
PREPARED
FILES_PUBLISHED
PRUNED
```

原因：

- 仍可能需要重新上传文件；
- 仍可能需要继续 Prune；
- Next Ownership 必须对应当前 Local View。

## 8.3 不合理阶段

### OWNERSHIP_COMMITTED

Remote Ownership 已是新值。

接下来只执行：

```text
state_store.save(pending.next_state)
```

`pending.next_state` 已冻结，不应使用当前 HEAD。

### STATE_COMPLETE

Remote Ownership 和 Local State 均已完成。

接下来只执行：

```text
清理 current Stage
删除 Pending Marker
```

完全不需要 Local Build。

## 8.4 实际影响

流程：

```text
Ownership 已提交
State Save 失败
用户继续提交代码或重新 Build
重跑
```

结果：

```text
Pending HEAD/Manifest mismatch
拒绝保存冻结 State
```

或者：

```text
State 已成功
Cleanup 失败
用户继续开发
重跑
```

结果：

```text
无法清理旧 Pending
新部署也被旧 Pending 阻断
```

## 8.5 修复

### Phase-sensitive Validation

```text
PREPARED / FILES_PUBLISHED / PRUNED:
    require Local Manifest
    require HEAD
    require Ownership matrix

OWNERSHIP_COMMITTED:
    ignore current Local Manifest
    ignore current HEAD
    require Ownership = next
    save pending.next_state

STATE_COMPLETE:
    ignore current Local Manifest
    ignore current HEAD
    require Ownership = next
    cleanup only
```

### Preparation

更完整的做法：

- 普通部署 Remote Preflight 先轻量检查 Pending Phase；
- Post-commit Pending 使用 Recovery-only Prepare；
- 不运行 Build；
- 不读取当前 State 内容；
- 不冻结上传文件。

也可以让：

```bash
git-deploy TARGET --recover
```

处理 FTP 的 `OWNERSHIP_COMMITTED/STATE_COMPLETE`，而 PREPARED/FILES_PUBLISHED/PRUNED 保持普通 Forward Resume。

## 8.6 测试

- Ownership Committed + New HEAD；
- Ownership Committed + Missing `.deploy`；
- Ownership Committed + Broken Build；
- State Complete + New HEAD；
- State Complete + Missing Local Hybrid；
- State Complete + Dirty Worktree；
- Frozen State Uses Pending Commit；
- Cleanup Does Not Start New Deployment。

---

# 9. P2-02：FTP Freshness 没有保证重新读取 MLSD

## 9.1 当前 Cache

`FTPTransport.list_directory_typed()`：

```text
absolute path → tuple[FTPRemoteEntry]
```

存入：

```python
_typed_entries
```

后续相同目录直接返回 Cache。

## 9.2 Remote Plan 与 Execute

`prepare_remote_plan()`：

- 使用 FTPTransport 扫描；
- 把同一个 Transport 保留到确认之后。

`execute_prepared()`：

- 复用同一个 Transport；
- `validate_remote_freshness()` 再次调用 `lstat()` / `scan_ftp_tree()`。

但是这些调用可能直接命中 Remote Plan 时缓存。

## 9.3 为什么主路径没有立即造成 P0

新 Deployment 会：

1. 写 Pending；
2. Stage 文件；
3. Mutation 清空 Cache；
4. Publish 前执行第二次 Freshness。

所以 PREPARED 主路径通常会得到新事实。

但以下契约仍不准确：

- Initial Freshness 不是真正重新读取；
- Workspace All-project Freshness 可能使用 Planning Cache；
- PRUNED Resume 没有 Publish 前第二次完整 Tree Gate；
- Root-only No-op 的 Initial Gate 可能使用旧 Type；
- Doctor/调试输出难以解释。

## 9.4 修复

Transport 增加公开方法：

```python
refresh_remote_metadata()
```

FTP 实现：

```python
_clear_remote_caches()
```

每次 Freshness Validation 开始前必须调用。

不要依赖“前面刚好发生了 Mutation”来刷新。

## 9.5 测试

- Remote Plan 后外部添加 Mirror File；
- Remote Plan 后外部删除 Mirror File；
- Remote Plan 后 Direct Type Change；
- Pending PRUNED Confirmation Window Change；
- Workspace Later Repository Change；
- 断言执行了新的 MLSD；
- Stale 时业务路径零修改。

---

# 10. P2-03：Pending Phase 与 Ownership Hash 矩阵不够严格

## 10.1 当前规则

Pending Resume 允许 Current Ownership 为：

```text
previous_ownership_hash
或
next_ownership_hash
```

所有 Phase 共用。

额外只检查：

```text
OWNERSHIP_COMMITTED / STATE_COMPLETE
    必须 next
```

## 10.2 更严格的不变量

```text
PREPARED:
    必须 previous

FILES_PUBLISHED:
    必须 previous

PRUNED:
    previous 或 next
    （Ownership Publish 成功但 Marker Phase 写失败）

OWNERSHIP_COMMITTED:
    必须 next

STATE_COMPLETE:
    必须 next
```

## 10.3 风险

如果 PREPARED/FILES_PUBLISHED 时 Ownership 已经是 Next：

- 状态机事实不一致；
- 工具仍继续重发和 Prune；
- 隐藏远端人工修改或损坏；
- 降低 Fail Closed 可解释性。

这未必立刻造成数据丢失，但与严格状态机目标不一致。

## 10.4 修复

新增：

```python
validate_pending_ownership_phase(pending, current_hash)
```

错误信息显示：

- Phase；
- Expected Hash Class；
- Actual Hash；
- Manual Inspection 指引。

---

# 11. P3：`--reprobe` 当前没有独立行为

CLI 提供：

```bash
--reprobe
```

Help 表示：

```text
replace an existing FTP Hybrid capability profile
```

但当前只验证：

```text
--reprobe requires --probe-ftp-hybrid
```

实际 `--probe-ftp-hybrid` 无论是否带 `--reprobe` 都会执行 Probe 并覆盖 Profile。

选择其一：

1. 移除 `--reprobe`，因为 `--probe-ftp-hybrid` 本身已经足够显式；
2. 没有 `--reprobe` 且 Profile 已存在时只显示/拒绝覆盖；
3. `--reprobe` 才替换。

推荐第一种，保持 CLI 简单。

---

# 12. 验证覆盖评价

## 12.1 已覆盖良好

- Capability Schema；
- Target/Banner Mismatch；
- Missing Capability；
- Pending Identity；
- Pending Manifest；
- Pending HEAD；
- Pending Ownership；
- Unknown Phase；
- MLSD File/Directory；
- Empty Directory；
- Unknown/Symlink Type；
- Depth/Entry Limits；
- Permission Failure；
- Passive Probe；
- Active Probe；
- Adoption；
- Unknown Root；
- State Loss；
- Nested Type Change；
- Direct Type Change；
- Historical Directory；
- Stage Reset；
- Stage Verify；
- Publish Failure；
- Manifest Change During Resume；
- Prune Failure；
- RMD Failure；
- Ownership Failure；
- State Failure；
- Cleanup Failure；
- SFTP Regression；
- Native Regression。

## 12.2 缺失的关键测试

- Case-insensitive FTP Server；
- Local Casefold Collision；
- Remote Casefold Collision；
- Case-only Rename；
- Initial Pending Write Failure；
- Orphan Stage without Pending；
- Shared Stage Parent Non-empty；
- Probe Sibling Cleanup；
- State Complete + New HEAD；
- Ownership Committed + Broken Build；
- Freshness Cache External Mutation；
- Pending Phase/Ownership Invalid Matrix；
- vsftpd；
- ProFTPD/Pure-FTPd；
- 实际目标服务器。

---

# 13. v1.5.1 原子 TODO

## P0：Path Semantics

### TODO-001：Capability Schema 2

- [x] 增加 `case_sensitive_paths`
- [x] Profile Parser 支持 Schema Migration 或明确要求 Reprobe
- [x] v1.5.0 Profile 升级后要求 Reprobe
- [x] Release Notes 说明

### TODO-002：Case Probe

- [x] 创建 `CaseProbe.bin`
- [x] 创建 `caseprobe.bin`
- [x] MLSD 必须同时返回
- [x] RETR 内容必须独立
- [x] 删除一者不得影响另一者
- [x] Case-only Rename 行为测试
- [x] 不支持时 Fail Closed

### TODO-003：Collision Gate

- [x] Local 每目录 NFC + casefold 去重
- [x] Remote MLSD 每目录 NFC + casefold 去重
- [x] Root Files/Directories 去重
- [x] Nested Files/Directories 去重
- [x] 清晰错误路径

---

## P1：Internal Cleanup

### TODO-101：不要求删除 Shared Stage Parent

- [x] 删除 Current Deployment Stage Root
- [x] 删除 Current Pending Marker
- [x] Shared Parent RMD Best-effort
- [x] Shared Parent 非空不算部署失败
- [x] Pending 必须可以完成

### TODO-102：PREPARED Write Failure Cleanup

- [x] Pending 初写失败时清理 Current Stage Root
- [x] Cleanup 失败警告具体 ID
- [x] 不创建无 Marker 的长期孤儿

### TODO-103：Doctor Orphan Report

- [x] 枚举 Stage Deployment ID
- [x] 对比 Pending Deployment ID
- [x] 报告 Orphan
- [x] 不静默删除

### TODO-104：Probe Parent

- [x] 只强制清理 Current Probe Root
- [x] Shared Probe Parent Best-effort
- [x] 旧 Probe Sibling 不阻断新 Probe

---

## P2：Post-commit Resume

### TODO-201：Phase-sensitive Local Validation

- [x] PREPARED require Local/HEAD
- [x] FILES_PUBLISHED require Local/HEAD
- [x] PRUNED require Local/HEAD
- [x] OWNERSHIP_COMMITTED ignore Local/HEAD
- [x] STATE_COMPLETE ignore Local/HEAD

### TODO-202：Post-commit Recovery-only Prepare

- [x] 不运行 Build
- [x] 不读取 State 内容
- [x] 不扫描 Local Hybrid
- [x] 不 Freeze
- [x] 使用 Pending Next State
- [x] Cleanup-only

---

## P2：Freshness

### TODO-301：公开 Cache Refresh

- [x] `FTPTransport.refresh_remote_metadata()`
- [x] 清 Typed Cache
- [x] 清 NLST Cache
- [x] 清 Missing Cache

### TODO-302：每次 Freshness 前 Refresh

- [x] Project Initial Gate
- [x] Workspace All-project Gate
- [x] Post-Stage Gate
- [x] PRUNED Ownership Gate
- [x] Post-commit Resume Gate

---

## P2：Phase Matrix

### TODO-401：Ownership Hash Matrix

- [x] PREPARED = previous
- [x] FILES_PUBLISHED = previous
- [x] PRUNED = previous or next
- [x] OWNERSHIP_COMMITTED = next
- [x] STATE_COMPLETE = next
- [x] Doctor 输出 Manual Inspection

---

## P3：CLI

### TODO-501：移除或实现 `--reprobe`

推荐：

- [x] 移除 `--reprobe`
- [x] `--probe-ftp-hybrid` 明确表示重新探测并覆盖
- [x] Help/README 更新

---

# 14. 修复后验收标准

v1.5.1 至少满足：

1. Case-insensitive Server Probe 失败；
2. Local Casefold Collision 在连接前失败；
3. Remote Casefold Collision 在 Remote Plan 失败；
4. Case-only Rename 不产生 Upload/Delete 自冲突；
5. Pending 初写失败不留下无法识别 Stage；
6. 一个旧 Orphan Stage 不阻止新 Deployment 完成；
7. Current Pending Marker 可在 Shared Parent 非空时删除；
8. Probe Sibling 不阻止 Reprobe；
9. Ownership Committed 后 Build 损坏仍可保存 Frozen State；
10. State Complete 后 HEAD 改变仍可清理；
11. Freshness 每次重新发送 MLSD；
12. PRUNED 前远端树变化会 Stale；
13. Invalid Phase/Ownership Matrix Fail Closed；
14. Python 3.11/3.12 CI 通过；
15. SFTP Hybrid 全量回归；
16. FTP Incremental 回归；
17. Real pyftpdlib Passive/Active；
18. 至少一个 vsftpd 或实际目标 FTP 验证。

> v1.5.1 落实说明：1–17 纳入本地/CI 自动门禁；第 18 项保持独立的可选人工兼容增强，未读取真实服务器配置或凭据，也不反向阻塞已通过的 Mock/pyftpdlib 主线。

---

# 15. 当前版本使用建议

## 可以继续使用

```text
SFTP Staged Hybrid v1.4.3/v1.5.0
Native OpenSSH Hybrid
Paramiko Hybrid
普通 FTP Incremental
普通 SFTP Incremental
Workspace 顺序部署
```

## FTP Hybrid 暂时使用条件

只有同时确认以下条件时，才建议进行非关键环境试用：

```text
服务器文件系统大小写敏感
本地聚合目录没有 casefold 冲突
远端受管目录没有 casefold 冲突
只有一个发布器
部署期间不使用面板/手工 FTP 修改受管路径
部署前人工清理旧 .git-deploy/ftp-hybrid/stage 孤儿
当前项目可以容忍 In-place 非原子目录更新
```

不建议当前直接用于：

- Windows/IIS FTP；
- 大小写语义未知的托管 FTP；
- 多人共用 FTP；
- 无法人工检查 `.git-deploy` 的关键生产站点；
- 高可用静态站点；
- 同时由面板和 git-deploy 发布的目录。

---

# 16. 最终结论

v1.5.0 的 FTP In-place Hybrid 主体设计是成立的：

```text
Capability Probe
MLSD Typed Scan
Remote Ownership
File Stage + Verify
Upload-first
Prune-last
Forward Resume
```

实现完成度较高，失败阶段覆盖也明显优于简单 FTP Mirror。

但是当前 Capability Contract 缺少文件系统大小写语义，导致一个通过所有现有 Probe 的 FTP Server 仍可能无法忠实表达 Local Hybrid Manifest，并可能产生成功状态下的文件缺失。

同时，共享 Stage Parent Cleanup 可能被一次无 Marker 的孤儿目录永久阻断。

因此：

> **v1.5.0 FTP Hybrid 本轮审计不通过。**

建议不要扩展其他功能，直接发布：

```text
v1.5.1
FTP path semantics + internal cleanup + post-commit resume closeout
```

完成上述最小收口后，再将 FTP In-place Hybrid 作为稳定能力投入真实项目。
