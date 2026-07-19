# git-deploy v1.5.3 审计与 v1.6.0 传输速率可视化综合方案

> 当前稳定版本：`v1.5.3`
> 下一目标版本：`v1.6.0`
> 文档日期：`2026-07-19`
> 文档用途：稳定基线审计结论、剩余风险、下一版本功能设计与原子实施计划

---

# 文档总览

本文档分为三部分：

1. **v1.5.3 最新代码深度审计**：确认 FTP/SFTP Hybrid 当前稳定性、已关闭问题与剩余非阻断风险；
2. **v1.6.0 传输速率可视化方案**：定义实时速率、平均速率、Retry/Wire Bytes 统计及实现边界；
3. **综合版本建议**：将 v1.5.3 作为稳定基线，v1.6.0 作为纯可观测性版本。

---

# 第一部分：v1.5.3 最新代码深度审计

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`3f9226e5bb551a8f510eeb1ab96ec0742ca215fe`
> 功能提交：`69bd5c28251ab508fceb61231a168bf4c13b9224`
> PR：`#18 Harden FTP retry sessions and remote root alias safety`
> 版本：`v1.5.3`
> 审计日期：`2026-07-19`
> 审计结论：**通过**
> 建议：将 `v1.5.3` 作为 FTP/SFTP Hybrid 当前稳定基线，停止连续安全补丁迭代，转入真实项目验证与稳定维护。

---

# 1. 执行摘要

v1.5.3 对 v1.5.2 深度审计发现的两个 FTP 协议层 P1 完成了精确修复：

1. FTP UTF-8 要求从单 Session 状态提升为 Transport 生命周期状态；
2. 每次连接重试都会重新核对 Server Banner；
3. 每次连接重试都会重新执行 FEAT；
4. 每次连接重试都会确认 UTF8 Capability；
5. 每次连接重试都会重新执行 `OPTS UTF8 ON`；
6. 重连失败会在 STOR、RETR、DELE、RMD、Rename 等业务命令前关闭连接；
7. Capability Probe 在首次创建 `.git-deploy` 前读取远端根目录直接条目；
8. Remote Plan 在任何业务写入前检查未知根目录别名；
9. 每次 FTP Freshness Gate 都重新读取根目录并检查别名；
10. FTP Explicit Recovery 也检查内部 `.git-deploy` 根别名；
11. 大小写、NFC/NFD 和 `.GIT-DEPLOY` 等别名在写入前 Fail Closed；
12. 精确同名继续进入已有 Adoption、Ownership 和 Type Check；
13. 无关未知根目录不递归、不修改、不删除；
14. Stable Source Content Contract 只在 FTP Hybrid 下生成；
15. Source Blob SHA256 改为单个 `git cat-file --batch` 流式计算；
16. 普通 FTP Incremental、普通 SFTP、SFTP Hybrid 和 Native OpenSSH 不再承担额外 Source SHA256 成本；
17. `--remote-plan` 在 Build 前检查 Post-commit FTP Pending；
18. Python 3.11/3.12、Ruff、ty、构建和隔离安装通过；
19. Main 与 `v1.5.3` Tag 的版本和核心文件一致。

本轮没有发现：

```text
P0：无
P1：无
```

在项目声明的边界内：

```text
个人使用
单发布器
同一时间一个部署
不通过面板、CI、手工 FTP 同时修改受管路径
```

FTP In-place Hybrid 已具备足够完整的：

```text
Capability Proof
Remote Ownership
Upload-first / Prune-last
Forward Resume
Pending Plan Contract
Cross-clone State Recovery
UTF-8 Session Recovery
Remote Root Alias Gate
```

因此 v1.5.3 可以作为当前稳定基线。

本轮仍发现三个非阻断改进项：

- P2：`FILES_PUBLISHED` 恢复阶段的 Plan 与 Executor 语义不完全一致；
- P2：FTP Explicit Recovery 的 Alias Gate 在清 Root Cache 前执行；
- P2：Doctor 没有启用已证明的 UTF-8 Session，也没有显示远端根别名；
- P3：普通 FTP `upload/delete` 没有统一清理新增的 Root Metadata Cache。

这些问题不会在声明边界内造成未知内容删除，不阻止 v1.5.3 投入实际项目验证。

---

# 2. 审计范围

## 2.1 发布完整性

- Main 最新 Commit；
- PR #18；
- PR Head；
- Merge Commit；
- `v1.5.3` Tag；
- `pyproject.toml`；
- `__version__`；
- FTP Transport Blob；
- FTP Hybrid Blob；
- Planner；
- Deployer；
- Git Batch；
- GitHub Actions；
- Review Threads。

## 2.2 Sticky UTF-8

- Initial Connect；
- Initial `enable_utf8()`；
- Transport Lifetime；
- Close；
- Invalidate；
- Reconnect；
- Passive；
- Active；
- Server Banner；
- FEAT；
- OPTS；
- Source Upload；
- Incremental Output Upload；
- Delete；
- Hybrid Stage；
- Hybrid Publish；
- RMD；
- Failure before Business Command。

## 2.3 Remote Root Alias

- Capability Probe；
- Remote Plan；
- Freshness；
- Recovery；
- `.git-deploy`；
- Source Root；
- Incremental Root；
- Hybrid Root；
- Historical Root；
- Casefold；
- NFC/NFD；
- Unknown Type；
- Exact Match；
- Unrelated Unknown Root；
- Zero-mutation Rejection。

## 2.4 Git Performance

- `ls-tree -l`；
- Blob OID；
- Blob Size；
- `cat-file --batch`；
- Duplicate Blob；
- Large Blob；
- Streaming SHA256；
- Conditional Contract；
- Non-FTP Hybrid Regression；
- Freeze Verification。

## 2.5 Forward Resume Regression

- PREPARED；
- FILES_PUBLISHED；
- PRUNED；
- OWNERSHIP_COMMITTED；
- STATE_COMPLETE；
- Previous State；
- Non-Hybrid Plan Hash；
- Frozen State；
- Cleanup；
- Orphan Stage。

---

# 3. 审计方式与限制

本轮通过 GitHub Connector 检查：

- Commit History；
- PR Metadata；
- PR Diff；
- Changed Files；
- Main/Tag 文件；
- Workflow Runs；
- Workflow Jobs；
- Review Threads；
- Source Code；
- Unit Tests；
- Real FTP Integration Tests；
- Release Notes；
- ADR。

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

因此无法在本轮独立复跑：

```bash
uv lock --check
uv run --isolated --python 3.11 --all-groups pytest -q
uv run --isolated --python 3.12 --all-groups pytest -q
uv run ruff check src tests
uv run ty check src
uv build --clear
```

动态验证依据为成功的 GitHub Actions，以及仓库中的真实 pyftpdlib、SFTP 和 Native OpenSSH 集成测试。

实际目标 FTP 服务器的 Probe 和 Canary Deployment 尚未由本轮审计环境执行。

---

# 4. 版本、Tag 与 CI

## 4.1 Main

```text
3f9226e5bb551a8f510eeb1ab96ec0742ca215fe
Merge pull request #18 from howjc/agent/v1.5.3-ftp-session-alias-closeout
```

功能提交：

```text
69bd5c28251ab508fceb61231a168bf4c13b9224
feat: harden FTP retry and root alias safety
```

## 4.2 PR

```text
PR #18
title: Harden FTP retry sessions and remote root alias safety
merged: true
```

PR 记录：

```text
Python 3.11：281 passed
Python 3.12：281 passed
Ruff：passed
ty：passed
Passive/Active pyftpdlib：passed
FTP Hybrid E2E：passed
10k Git Batch：0.822s
Wheel/sdist：passed
Isolated Install：passed
```

PR 没有未解决 Review Thread。

## 4.3 Package

Main：

```toml
version = "1.5.3"
```

Tag：

```toml
version = "1.5.3"
```

Main/Tag `pyproject.toml` Blob：

```text
9853da76cf8e0b2d2b989946c51da3ac1bc9c74d
```

一致。

Main/Tag：

```python
__version__ = "1.5.3"
```

Blob：

```text
9881dd81ee47726df8ac9b47bd66569335452e3d
```

一致。

---

# 5. v1.5.2 P1：FTP 重连丢失 UTF-8 Session

## 5.1 旧问题

v1.5.2 的 UTF-8 Capability Proof 只绑定当前 Session：

```text
Connect
FEAT UTF8
OPTS UTF8 ON
Unicode Probe
```

文件操作 Retry 会建立新 Session，但没有重新执行 OPTS。

## 5.2 v1.5.3 实现

FTPTransport 新增：

```python
_require_utf8
_required_server_banner_hash
```

第一次成功：

```python
enable_utf8()
```

后：

```text
_require_utf8 = true
required banner = current banner
```

`close()` 和 `invalidate_connection()` 不清除这两个 Transport 生命周期事实。

每次新 `connect()`：

```text
Connect
Login
Passive/Active
if require_utf8:
    Validate Banner
    FEAT
    Require UTF8
    OPTS UTF8 ON
    ftplib.encoding = utf-8
```

## 5.3 Fail Closed

以下任一失败：

- Banner 变化；
- FEAT 失败；
- UTF8 不再广告；
- OPTS 拒绝；
- 非 2xx 响应；

都会：

```text
关闭新 Session
transport.ftp = None
业务命令 = 0
```

## 5.4 覆盖入口

普通 Operation Retry：

- Source Upload；
- Incremental Output Upload；
- Source Delete。

FTP Hybrid Mutation Retry：

- Stage Upload；
- Stage Verify；
- Publish；
- Final Verify；
- Orphan Delete；
- RMD。

## 5.5 测试

测试覆盖：

- Passive；
- Active；
- 两个真实独立 Session；
- 每个 Session 都收到 FEAT；
- 每个 Session 都收到 OPTS；
- 第二 Session encoding = UTF-8；
- OPTS Failure；
- Banner Drift；
- Source Unicode Upload；
- Output Unicode Upload；
- Unicode Delete；
- Hybrid Stage；
- Hybrid Publish；
- Unicode RMD。

## 5.6 结论

> **上一轮 P1 已关闭。**

---

# 6. v1.5.2 P1：未知远端根别名在写入后才暴露

## 6.1 旧问题

远端：

```text
Assets/
```

计划：

```text
assets/
```

旧 `lstat("assets")` 使用精确名称，可能判断 Missing。

工具创建 `assets/` 后，严格 MLSD Collision Gate 才发现：

```text
Assets / assets
```

造成 Partial Mutation 和 PREPARED Pending。

## 6.2 Root Name Adapter

FTPTransport 新增：

```python
list_root_names()
```

特点：

- 只执行 Root 单层 MLSD；
- 返回精确名称；
- 不递归未知内容；
- 不要求未知类型可被 git-deploy 管理；
- 记录 Exact Type 供后续 lstat；
- Root 可包含无关未知类型；
- Managed Directory 内仍使用严格 Typed Scanner。

## 6.3 Alias Gate

新增：

```python
validate_remote_root_aliases()
```

计算：

```text
NFC(name).casefold()
```

规则：

```text
Remote Exact Name == Planned Name
    → 允许
    → 后续 Adoption/Ownership/Type Check

Normalized Key 相同但 Exact Name 不同
    → Fail Closed

无关 Unknown Root
    → 忽略
    → 不递归
    → 不修改
```

## 6.4 Probe

Capability Probe 顺序：

```text
Connect
FEAT/OPTS
Root Alias Gate for .git-deploy
    ↓
First MKD
```

如果远端存在：

```text
.GIT-DEPLOY
```

Probe 在创建 `.git-deploy` 前失败，远端文件变更为零。

## 6.5 Remote Plan

Remote Plan 在读取或写入业务路径前检查：

- Current Source；
- Historical Source；
- Current Incremental；
- Historical Incremental；
- Current Hybrid；
- Historical Hybrid；
- `.git-deploy`。

## 6.6 Freshness

每次执行前：

```text
refresh_remote_metadata
Root Alias Gate
Ownership
Pending
Path Type
Managed Tree
```

Stage 完成后和 Ownership Commit 前都会重新进入 Freshness。

## 6.7 Recovery

Recovery 至少检查：

```text
.git-deploy
```

的远端别名，避免把未知 Internal Alias 误当成正式内部目录。

## 6.8 真实测试

真实 pyftpdlib 覆盖：

```text
.GIT-DEPLOY / .git-deploy
Index.html / index.html
```

验证：

- Probe 失败前零 `.git-deploy` 创建；
- Remote Plan 失败前零 `index.html` 创建；
- Unknown Alias 内容保持不变；
- Pending 未创建。

## 6.9 结论

> **上一轮 P1 已关闭。**

---

# 7. Source Content Contract 性能回归

## 7.1 v1.5.2 问题

所有 Backend 都为 Source Upload：

- 读取完整 Git Blob；
- 计算 SHA256；
- Freeze 时再次读取和校验。

但该内容 Contract 只用于 FTP Hybrid Pending。

## 7.2 v1.5.3 条件

只有：

```text
Target Protocol = FTP
and Hybrid Mapping exists
```

才启用：

```text
Source Content Identity
Non-Hybrid Plan Hash
```

以下路径不额外 Hash：

- 普通 SFTP；
- Native OpenSSH；
- 普通 FTP Incremental；
- SFTP Hybrid。

## 7.3 Git Tree

`GitEntry` 增加：

```text
OID
Size
```

来自：

```bash
git ls-tree -r -l -z
```

## 7.4 Batch

所有计划上传 Blob OID 通过一个：

```bash
git cat-file --batch
```

流式处理：

- Header OID 验证；
- Object Type 必须 Blob；
- Size 验证；
- 1 MiB 分块 SHA256；
- Blob 分隔符验证；
- Duplicate OID 只 Hash 一次；
- stderr 独立临时文件，避免 Pipe Blocking；
- Error 时 Kill/Wait。

## 7.5 Freeze

FTP Hybrid Source：

```text
Export Exact Commit Blob
Hash Frozen File
Compare Plan SHA/Size
```

其他 Backend：

```text
Export Exact Commit Blob
不重复 Plan SHA 验证
```

## 7.6 测试和基准

覆盖：

- 大 Blob；
- Duplicate Blob；
- Content SHA；
- Size；
- Non-FTP no hashing；
- FTP Incremental no hashing；
- SFTP Hybrid no hashing；
- 10k Batch Benchmark。

## 7.7 结论

> **上一轮性能建议已关闭。**

---

# 8. P2-01：FILES_PUBLISHED 恢复阶段的 Plan 与 Executor 不一致

## 8.1 当前 Planner

对：

```text
FILES_PUBLISHED
```

仍设置：

```text
publish_needed = true
```

因此 Plan 包含：

- Root File Upload；
- Mirror File Upload；
- Create Directory。

## 8.2 当前 Executor

执行器只在：

```text
phase == PREPARED
```

时执行：

- 普通 Operations；
- Stage；
- Hybrid Publish；
- Create Directory。

如果 Phase 是：

```text
FILES_PUBLISHED
```

则直接进入：

```text
Prune
```

## 8.3 当前验证

Current File / Mirror Missing 检查只用于：

```text
PRUNED
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

没有用于：

```text
FILES_PUBLISHED
```

## 8.4 影响

Marker 写入时，所有当前文件确实已经发布和 Final Verify。

在声明的单发布器且无外部修改前提下，这个事实可靠，因此不构成发布阻断。

但是，如果 Pending 存在期间发生：

- 人工删除当前文件；
- 面板修改；
- 另一个发布器修改；
- FTP 存储故障；
- Server Crash 后文件未持久化；

Resume 会：

```text
看到缺失文件
不重传
继续 Prune
提交 Ownership
保存 State
```

Plan 同时还会显示并不会执行的 Upload，降低审阅准确性。

## 8.5 建议方案

选择一个固定语义。

### 方案 A：Fail Closed

`FILES_PUBLISHED`：

- `publish_needed = false`；
- Plan 不显示 Upload；
- 验证全部 Root File 存在；
- 验证全部 Mirror File 存在；
- 验证全部 Local Directory 存在；
- 缺失时要求人工处理或退回 PREPARED。

### 方案 B：重新发布 Hybrid

推荐：

- 不重复普通 Source/Incremental Operations；
- 重新 Stage 和 Publish 当前 Hybrid Files；
- 再执行 Prune；
- 保持 Upload-first。

FTP Mirror 本来就是强发布单元，重新发布最符合“重跑收敛”。

## 8.6 定级

```text
P2
```

原因：

- 正常工具内失败流程不会产生该状态；
- 需要违反单发布器边界或发生远端存储异常；
- 不会删除未知根内容。

---

# 9. P2-02：FTP Recovery Alias Gate 在 Cache Refresh 前执行

## 9.1 当前顺序

`validate_recovery_freshness()`：

```text
enable_utf8
validate_remote_root_aliases
load capability
refresh_remote_metadata
read Ownership
read Pending
```

Recovery Plan 与执行复用同一个 FTPTransport。

Root Names 已在 Recovery Plan 阶段缓存。

## 9.2 影响

确认窗口中新出现：

```text
.GIT-DEPLOY
```

本次 Alias Gate 可能使用旧 Root Snapshot。

随后 Cache Refresh 会更新 Ownership/Pending 读取，但 Alias Gate 不再执行。

## 9.3 安全性

FTP Recovery 只会：

- 保存本地 Frozen State；
- 清理精确 `.git-deploy/ftp-hybrid/...`；
- 不访问未知 `.GIT-DEPLOY`。

Capability 已证明服务器大小写敏感且保留规范化，因此当前不会误删 Unknown Alias。

这属于 Freshness Contract 顺序不完整，而不是数据删除漏洞。

## 9.4 修复

调整为：

```text
enable_utf8
refresh_remote_metadata
validate_remote_root_aliases
load capability
read Ownership
read Pending
```

## 9.5 定级

```text
P2
```

---

# 10. P2-03：Doctor 没有激活 UTF-8 或报告 Root Alias

## 10.1 当前行为

有 Schema 3 Profile 时，Doctor：

```text
Connect
Load Local Capability Profile
Read Ownership/Pending/Remote Tree
```

没有调用：

```text
enable_utf8()
```

## 10.2 影响

对于要求 OPTS 才启用 UTF-8 的服务器：

- 中文 MLSD 可能 Decode 失败；
- Unicode lstat 可能失败；
- Doctor 可能误报 Target/Remote Failure；
- 正式部署却能够正常工作。

## 10.3 Alias

Doctor 的 FTP Remote 检查直接：

```text
lstat(".git-deploy")
```

没有调用：

```text
validate_remote_root_aliases()
```

远端只有：

```text
.GIT-DEPLOY
```

时，Doctor 可能显示 Internal Missing，而实际 Probe/Deploy 会因 Alias Fail Closed。

## 10.4 建议

FTP Hybrid Doctor：

```text
enable_utf8
validate internal alias
load profile
read Ownership
derive planned roots
validate all planned aliases
continue diagnostics
```

同时更新 Feature Detail：

```text
UTF8
Unicode Exact Paths
Normalization Preserving
Case Sensitive
MLSD
RETR
Rename
Delete/RMD
```

## 10.5 定级

```text
P2
```

---

# 11. P3：普通 FTP Upload/Delete 没有统一清 Root Cache

v1.5.3 新增：

```text
_root_names
_root_types
```

多数 Mutation 使用：

```python
_clear_remote_caches()
```

但普通：

```python
upload()
```

成功后只清：

```text
_typed_entries
```

普通：

```python
delete()
```

只维护 NLST Parent Cache。

FTP Hybrid 主链在依赖 Root Metadata 前会显式：

```text
refresh_remote_metadata
```

所以当前没有实际正确性回归。

为了保持 Transport 不变量：

> 任意 Mutation 后所有 Remote Metadata Cache 失效

建议 `upload/delete` 成功后也统一调用：

```python
_clear_remote_caches()
```

定级：

```text
P3
```

---

# 12. 已知设计边界

这些是明确产品约束，不是 v1.5.3 缺陷。

## 12.1 单发布器

不支持：

- 两台机器同时部署；
- CI 与本地同时部署；
- 面板与 git-deploy 同时部署；
- 手工 FTP 修改受管路径；
- 多发布器 Remote Lease。

## 12.2 FTP In-place

不提供：

- Directory Atomic Swap；
- Old Tree Rollback；
- Zero-downtime；
- Global Transaction；
- Historical Release。

## 12.3 Planned-Missing

FTP Rename Replace 不能提供 SFTP Legacy Rename 的最终 No-replace。

部署期间最后一刻出现同名路径，仍属于单发布器边界之外。

## 12.4 Root Unknown

Unknown Root：

- 不递归；
- 不删除；
- 不接管；
- 只有名称与 Planned Root 等价但拼写不同时阻断。

## 12.5 Actual Server

不同 FTP Server 对：

- FEAT；
- OPTS；
- MLSD；
- Rename；
- Unicode；
- Normalization；

实现存在差异。

自动 pyftpdlib 通过，不等于实际目标已验证。

---

# 13. 发布与使用建议

## 13.1 稳定基线

建议：

```text
v1.5.3 = 当前稳定基线
```

不建议继续因为本轮 P2/P3 发布：

```text
v1.5.4
```

除非真实项目使用触发对应问题。

## 13.2 首次目标验证

升级后执行：

```bash
git-deploy doctor prod --probe-ftp-hybrid
git-deploy prod --remote-plan --full
```

人工确认：

- Capability Schema 3；
- UTF8；
- NFC/NFD；
- Case Sensitive；
- Remote Root Alias；
- Adoption；
- Delete/RMD；
- Unknown Root。

## 13.3 Canary

第一次正式部署建议：

- 使用非关键目录；
- 备份 Remote Ownership；
- 保留 FTP 操作日志；
- 聚合目录规模较小；
- 无其他发布器；
- 先确认 Unknown Root。

## 13.4 Pending

出现：

```text
PREPARED
FILES_PUBLISHED
PRUNED
```

保持：

- 同一 State；
- 同一 HEAD；
- 同一 Config；
- 同一 Local Build；
- 不使用 `--full` 改变 Plan；
- 不人工修改 Remote Managed Paths。

出现：

```text
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

执行：

```bash
git-deploy prod --recover
```

---

# 14. 最终结论

v1.5.3 精确关闭了 v1.5.2 的核心协议缺口：

```text
Sticky UTF-8 Session
Remote Unknown Root Alias Gate
Conditional Source Content Contract
Git cat-file --batch
```

实现具备：

- 最小改动；
- 清晰 Contract；
- 真实 Session Retry 测试；
- 真实 FTP Alias 零写入测试；
- Passive/Active；
- Python 3.11/3.12；
- Main/Tag 一致；
- 无未解决 Review Thread；
- SFTP/Native 回归覆盖。

本轮没有发现新的 P0 或 P1。

综合结论：

> **git-deploy v1.5.3 通过本轮深度审计。**

建议：

> **将 v1.5.3 作为 FTP/SFTP Hybrid 稳定基线，停止连续安全补丁迭代，进入实际 FTP 目标 Probe、Canary Deployment 和稳定性观察阶段。**


---


# 第二部分：v1.6.0 传输速率可视化实施方案

> 目标版本：`v1.6.0`
> 功能定位：上传过程可观测性增强
> 设计原则：纯展示层、零状态机侵入、FTP/SFTP/Native OpenSSH 统一统计
> 核心输出：实时上传速率、单文件平均上传速率、部署级平均上传速率、Retry/Wire Bytes 汇总

---

## 16. 功能背景

当前 `ProgressReporter` 已由 FTP 与 SFTP 上传共用，但只按百分比显示：

```text
UPLOAD assets/app.js: 60%
```

这能确认上传仍在推进，却不能回答：

- 当前网络传输速度是多少；
- 当前速度是否稳定；
- 是链路慢，还是大量小文件造成协议开销；
- 是否发生了重传；
- 部署完成后的整体平均上传速率是多少；
- 当前网络状态是否明显异常。

因此建议增加一套轻量的上传速率统计能力。

该功能只依赖现有上传回调：

```python
callback(transferred: int, total: int | None)
```

不修改：

- Planner；
- Ownership；
- Pending；
- Recovery；
- Local State；
- Remote Manifest；
- Transport 协议语义。

---

## 17. 北极星目标

> 在不影响部署稳定性和状态机的前提下，让用户在上传过程中能够直观看到当前速率，并在部署结束后获得可信的平均上传速率与重试信息，用于快速判断网络链路是否正常。

### 17.1 成功标准

上传期间：

```text
UPLOAD assets/app.js  63%  3.8 MiB / 6.0 MiB  2.42 MiB/s
```

单文件完成：

```text
UPLOAD assets/app.js 100%  6.0 MiB  avg 2.36 MiB/s
```

全部上传完成：

```text
TRANSFER SUMMARY
  files:          126
  payload:        52.8 MiB
  wire bytes:     55.4 MiB
  active time:    8.42s
  average upload: 6.58 MiB/s (55.2 Mbps)
  retries:        2
```

---

## 18. 统计口径

### 18.1 Payload Bytes

```text
最终成功部署的逻辑文件总大小
```

特点：

- 按远端路径去重；
- 一个文件即使重试多次，只计算一次；
- 用于表示本次部署真正需要发布的数据量。

### 18.2 Wire Bytes

```text
所有上传尝试实际发送的字节总量
```

包括：

- 成功上传；
- 失败上传已发送部分；
- Retry Restage；
- FTP Hybrid Stage 重传；
- SFTP/Native 重传。

例如：

```text
文件大小：10 MiB
第一次上传 6 MiB 后断线
第二次完整上传 10 MiB
```

结果：

```text
payload:    10 MiB
wire bytes: 16 MiB
retries:    1
```

### 18.3 Active Upload Time

只统计真正执行上传的时间。

不包含：

- Build；
- Git Freeze；
- Remote Plan；
- FTP RETR 校验；
- Rename；
- Delete；
- RMD；
- Ownership；
- Pending；
- Remote Command；
- State Save；
- Cleanup。

### 18.4 Average Upload Rate

```text
wire bytes / active upload time
```

名称必须明确为：

```text
average upload
```

不建议写成泛化的：

```text
average transfer
```

因为 FTP Hybrid 还会执行 RETR 校验，但第一版不统计下载字节。

---

## 19. 实时速率算法

### 19.1 不使用单次 Callback 瞬时值

直接计算：

```text
本次新增字节 / 本次回调时间差
```

会受到：

- TCP Buffer；
- FTP/SFTP Block；
- Python 调度；
- 终端刷新；
- 小文件完成；

影响，数值会剧烈抖动。

### 19.2 滑动窗口

建议默认：

```text
window = 1.5s
```

维护：

```text
(timestamp, cumulative_wire_bytes)
```

当前速率：

```text
窗口内字节增量 / 窗口时间
```

### 19.3 刷新频率

TTY：

```text
每 250ms 最多刷新一次
```

即：

```python
refresh_interval = 0.25
```

非 TTY：

```text
不持续刷新
只在文件完成和最终 Summary 时输出
```

---

## 20. Retry 语义

### 20.1 逻辑文件与物理尝试分离

每个 `remote_path` 对应一个逻辑文件。

每次重新建立上传回调，视为新的物理 Attempt。

内部应记录：

```python
logical_files[path] = expected_size
attempts[path] += 1
retry_count += 1
```

### 20.2 Callback 回退

如果：

```text
previous transferred = 6 MiB
current transferred  = 0
```

表示新的 Attempt，不应计算：

```text
-6 MiB
```

规则：

```text
current < previous
    → 新 Attempt
    → previous = 0
```

### 20.3 Retry 入口

需要覆盖三个主要入口：

1. 普通 Source / Incremental Output Upload；
2. SFTP Staged Hybrid Upload；
3. FTP In-place Hybrid Stage / Restage Upload。

Delete 和 RMD Retry 不进入上传速率统计。

---

## 21. 终端展示模式

### 21.1 TTY

使用：

```text
\r
```

原地刷新。

示例：

```text
UPLOAD assets/app.js  63%  3.8 MiB / 6.0 MiB  2.42 MiB/s
```

完成后换行：

```text
UPLOAD assets/app.js 100%  6.0 MiB  avg 2.36 MiB/s
```

### 21.2 非 TTY

适用于：

- CI；
- 日志重定向；
- Pipe；
- 文件输出。

只输出：

```text
UPLOAD assets/app.js  6.0 MiB  avg 2.36 MiB/s
```

以及最终 Summary。

### 21.3 `--verbose`

`--verbose` 不应输出每个 Network Block。

建议：

```text
TTY 默认：
    250ms 刷新

TTY verbose：
    250ms 刷新
    每个文件完成保留一行

非 TTY：
    每个文件完成一行
    最终 Summary
```

---

## 22. 小样本提示

以下任一条件成立：

```text
payload < 1 MiB
active time < 1s
```

显示：

```text
average upload: 18.7 MiB/s (sample too small)
```

避免根据极小文件错误判断网络。

第一版不自动输出：

```text
network normal
network abnormal
```

因为上传速度还受以下因素影响：

- 服务器限速；
- FTP/SFTP 协议开销；
- 加密性能；
- 文件数量；
- RTT；
- 服务器磁盘；
- RETR 校验；
- VPS 共享带宽；
- 被动/主动模式。

---

## 23. 数据模型

建议继续使用现有：

```python
@dataclass(slots=True)
class ProgressReporter:
    verbose: bool = False
```

扩展为：

```python
@dataclass(slots=True)
class ProgressReporter:
    verbose: bool = False
    refresh_interval: float = 0.25
    speed_window: float = 1.5
```

建议内部状态：

```python
@dataclass(slots=True)
class TransferAttempt:
    started_at: float
    last_at: float
    last_transferred: int
    wire_bytes: int
    samples: deque[tuple[float, int]]
```

```python
@dataclass(slots=True)
class TransferSummary:
    logical_files: int
    payload_bytes: int
    wire_bytes: int
    active_seconds: float
    retry_count: int
```

`ProgressReporter` 内部：

```python
_files: dict[str, int]
_attempts: dict[str, TransferAttempt]
_completed: set[str]
_wire_bytes: int
_active_seconds: float
_retry_count: int
_started_at: float | None
_last_render_at: float
```

---

## 24. 时间源

必须使用：

```python
time.monotonic()
```

原因：

- 不受系统时间调整影响；
- 不受 NTP 校时影响；
- 不会出现负时间；
- 适合持续时间和速率计算。

为了便于测试，建议注入：

```python
clock: Callable[[], float] = time.monotonic
```

---

## 25. 单位格式

### 25.1 Byte

建议 IEC：

```text
B
KiB
MiB
GiB
```

### 25.2 Rate

```text
KiB/s
MiB/s
GiB/s
```

### 25.3 Mbps

最终 Summary 可附带：

```text
55.2 Mbps
```

计算：

```text
MiB/s × 8 × 1024² / 1,000,000
```

单文件动态行只显示：

```text
MiB/s
```

避免过于拥挤。

---

## 26. API 设计

建议新增：

```python
def callback(self, path: str, total: int) -> ProgressCallback:
    ...
```

```python
def record_retry(self, path: str) -> None:
    ...
```

```python
def finish(self) -> TransferSummary | None:
    ...
```

```python
def render_summary(self) -> None:
    ...
```

### 26.1 Callback 生命周期

```text
callback()
    → 注册逻辑文件
    → 创建 Attempt
    → 返回 Transport Callback
```

完成时：

```text
transferred >= total
    → 结束 Attempt
    → 输出单文件完成信息
    → 标记逻辑文件成功
```

### 26.2 失败 Attempt

Retry 发生前：

```python
progress.record_retry(path)
```

完成：

- 关闭旧 Attempt 时间；
- 保留旧 Attempt Wire Bytes；
- Retry Counter +1；
- 下一次 `callback()` 创建新 Attempt。

---

## 27. 集成位置

### 27.1 Reporter 创建

当前一次 Deployment 只创建一个：

```python
progress = ProgressReporter(verbose)
```

应保持不变，使普通上传与 Hybrid 上传共享同一个统计器。

### 27.2 Deployment 结束

在以下时机调用：

```python
progress.finish()
```

成功路径：

- 普通部署所有上传完成后；
- SFTP Hybrid 完成后；
- FTP Hybrid 完成后；
- State Save 前后均可，但建议远端文件传输完成后立即输出。

失败路径：

- 不显示“最终成功平均速率”；
- 可选显示：

```text
TRANSFER INTERRUPTED
```

但第一版可不实现。

### 27.3 Workspace

每个 Repository 应独立显示 Summary：

```text
[frontend] TRANSFER SUMMARY
[backend] TRANSFER SUMMARY
```

不建议首版增加 Workspace 全局平均，因为：

- 多 Target；
- 多协议；
- 多服务器；
- 顺序执行；
- RTT 和限速不同。

---

## 28. 重试集成

当前 Retry 代码在捕获异常后准备下一次尝试。

应在：

```text
下一次 Attempt 之前
```

调用：

```python
progress.record_retry(path)
```

覆盖：

```text
_execute_with_retry()
_upload_with_retry()
_retry_ftp_mutation() 中确实包含上传的调用者
```

注意：

`_retry_ftp_mutation()` 同时用于 Delete/RMD，不能在通用函数内无条件统计 Upload Retry。

推荐：

```python
_retry_ftp_mutation(
    ...,
    on_retry=lambda: progress.record_retry(path),
)
```

只有 Stage/Publish Upload 传入 `on_retry`。

---

## 29. 对 FTP Hybrid 的特殊说明

FTP Hybrid 单个逻辑文件可能发生：

```text
Stage Upload
Stage RETR Verify
Stage → Final Rename
Final RETR Verify
```

第一版只统计：

```text
Stage Upload / Restage Upload
```

不统计：

- Stage RETR；
- Final RETR；
- Rename。

因此：

```text
部署总耗时
```

可能显著大于：

```text
active upload time
```

这是正确且必须在文档中说明的。

---

## 30. 建议输出格式

### 30.1 单文件

```text
UPLOAD public/assets/app.js  42%  8.1 MiB / 19.3 MiB  3.46 MiB/s
```

### 30.2 完成

```text
UPLOAD public/assets/app.js 100%  19.3 MiB  avg 3.21 MiB/s
```

### 30.3 Summary

```text
TRANSFER SUMMARY
  files:          126
  payload:        52.8 MiB
  wire bytes:     55.4 MiB
  active time:    8.42s
  average upload: 6.58 MiB/s (55.2 Mbps)
  retries:        2
```

### 30.4 无上传

部署只有 Delete/RMD：

```text
TRANSFER SUMMARY
  files:          0
  payload:        0 B
  retries:        0
```

也可以直接不显示 Summary。

推荐：

```text
没有上传文件时不显示
```

---

## 31. 不纳入 v1.6.0 的功能

- 下载速率；
- FTP RETR 速率；
- 历史速度持久化；
- 与上次部署自动比较；
- 自动判断网络正常/异常；
- Ping；
- 丢包检测；
- 抖动检测；
- 速度曲线；
- JSON Metrics；
- Prometheus；
- 配置化阈值；
- 并发传输；
- ETA 精确预测。

---

## 32. 文件修改范围

```text
src/git_deploy/progress.py
src/git_deploy/deployer.py
tests/test_progress.py
tests/test_deployer.py
tests/test_transports.py
README.md
docs/release-notes-v1.6.0.md
```

不需要修改：

```text
planner.py
ftp_hybrid.py
hybrid.py
manifest.py
config.py
ownership schema
pending schema
state schema
```

---

## 33. 原子 TODO

### Phase 1：数据与格式化

#### TODO-001：Byte Formatter

- [x] B
- [x] KiB
- [x] MiB
- [x] GiB
- [x] Zero
- [x] Decimal Precision

#### TODO-002：Rate Formatter

- [x] KiB/s
- [x] MiB/s
- [x] GiB/s
- [x] Mbps
- [x] Small Rate

#### TODO-003：Fake Clock

- [x] Clock Injection
- [x] Monotonic Default
- [x] Deterministic Tests

---

### Phase 2：Attempt Tracking

#### TODO-101：Logical File Registration

- [x] Path
- [x] Total
- [x] Payload Dedup
- [x] Zero-byte

#### TODO-102：Wire Delta

- [x] Positive Delta
- [x] Callback Reset
- [x] Duplicate Callback
- [x] Transfer > Total Clamp

#### TODO-103：Active Time

- [x] Attempt Start
- [x] Callback Update
- [x] Attempt Finish
- [x] Failure Close

#### TODO-104：Retry

- [x] Retry Counter
- [x] Attempt Reset
- [x] Wire Preserve
- [x] Payload Dedup

---

### Phase 3：实时显示

#### TODO-201：Sliding Window

- [x] 1.5s Default
- [x] Old Sample Eviction
- [x] Zero-duration Guard
- [x] Retry Reset

#### TODO-202：TTY Refresh

- [x] 250ms Throttle
- [x] `\r`
- [x] Clear Longer Previous Line
- [x] Completion Newline

#### TODO-203：Non-TTY

- [x] No Dynamic Refresh
- [x] Completion Line
- [x] Summary

#### TODO-204：Verbose

- [x] No Per-block Spam
- [x] Completion History
- [x] Retry Message Compatibility

---

### Phase 4：Summary

#### TODO-301：TransferSummary

- [x] Files
- [x] Payload
- [x] Wire
- [x] Active Time
- [x] Average Rate
- [x] Mbps
- [x] Retries

#### TODO-302：Small Sample

- [x] Payload < 1 MiB
- [x] Active < 1s
- [x] Hint Rendering

#### TODO-303：No Upload

- [x] No Summary
- [x] Zero-byte-only Behavior

---

### Phase 5：Deployer Integration

#### TODO-401：Ordinary Upload

- [x] Source
- [x] Incremental
- [x] Retry

#### TODO-402：SFTP Hybrid

- [x] Root File
- [x] Mirror File
- [x] Retry
- [x] Recovery Resume

#### TODO-403：FTP Hybrid

- [x] Stage Upload
- [x] Restage Upload
- [x] Retry
- [x] Do Not Count RETR

#### TODO-404：Finish

- [x] Ordinary Success
- [x] SFTP Hybrid Success
- [x] FTP Hybrid Success
- [x] Workspace Per Project

---

### Phase 6：测试

#### TODO-501：Single File

- [x] Progressive Callback
- [x] Rate
- [x] Average
- [x] Completion

#### TODO-502：Multiple Files

- [x] Payload Sum
- [x] Active Sum
- [x] Average
- [x] File Count

#### TODO-503：Retry

- [x] Partial Failure
- [x] Wire > Payload
- [x] Retry Count
- [x] New Attempt

#### TODO-504：TTY

- [x] Dynamic Refresh
- [x] Throttle
- [x] Completion Newline

#### TODO-505：Non-TTY

- [x] No Carriage Refresh
- [x] Completion Line
- [x] Summary

#### TODO-506：Protocol Regression

- [x] FTP
- [x] Paramiko
- [x] Native OpenSSH
- [x] FTP Hybrid
- [x] SFTP Hybrid

> **v1.6.0 落实说明（2026-07-19）**：TODO-001 至 TODO-506 已实现并纳入自动门禁。为完成 Workspace 独立命名 Summary，最小扩展修改了 `prepared.py` 与 `workspace.py`；版本发布同时更新 `pyproject.toml`、`__init__.py` 与由 `uv lock` 生成的 `uv.lock`。本机协议回归覆盖 Fake Transport、pyftpdlib FTP、Paramiko、Native OpenSSH、FTP Hybrid 与 SFTP Hybrid。真实外部目标速率验收仍是独立可选人工增强，本轮未读取或记录真实凭据。

---

## 34. 测试矩阵

| 场景 | Payload | Wire | Retry | 预期 |
|---|---:|---:|---:|---|
| 单文件成功 | 10 MiB | 10 MiB | 0 | 正常完成 |
| 6 MiB 后重试 | 10 MiB | 16 MiB | 1 | Wire 大于 Payload |
| 两文件成功 | 15 MiB | 15 MiB | 0 | File=2 |
| Zero-byte | 0 | 0 | 0 | 不除零 |
| Callback 重复 | 10 MiB | 10 MiB | 0 | 不重复计数 |
| Callback 回退 | 10 MiB | 16 MiB | 1 | 新 Attempt |
| FTP RETR | 10 MiB | 10 MiB | 0 | RETR 不统计 |
| Delete-only | 0 | 0 | 0 | 不显示 Summary |
| 非 TTY | 正常 | 正常 | 正常 | 无动态刷屏 |

---

## 35. 人工验收

### 35.1 大文件

准备：

```text
100 MiB
```

验收：

- 实时速率每 250ms 左右刷新；
- 数值没有毫秒级剧烈跳动；
- 完成后显示文件平均；
- Summary 与系统网络监控大致一致。

### 35.2 大量小文件

准备：

```text
5,000 × 10 KiB
```

验收：

- 不产生每个 Block 日志；
- 非 TTY 不刷屏；
- 平均速度明显低于大文件是可接受结果；
- 文件数量准确。

### 35.3 Retry

人工断开网络或注入一次失败。

验收：

```text
wire bytes > payload
retries > 0
```

### 35.4 FTP Hybrid

验收：

- Stage Upload 统计；
- RETR 不统计；
- Final Summary 名称为 `average upload`；
- 不误称完整部署吞吐量。

---

## 36. 版本规划

建议：

```text
v1.5.3
    当前稳定基线

v1.6.0
    Transfer Rate Visualization
```

该功能属于用户可见能力，不建议使用：

```text
v1.5.4
```

因为：

- 输出行为明显变化；
- 引入新的统计模型；
- 增加 Summary；
- 属于小型功能版本，而不是安全补丁。

---

# 第三部分：综合结论与下一步

## 37. 当前系统状态

`v1.5.3` 已达到：

```text
SFTP Staged Hybrid 稳定
FTP In-place Hybrid 稳定
Remote Ownership 稳定
Pending Resume 稳定
UTF-8 Session 稳定
Root Alias Gate 稳定
```

仍保留少量 P2/P3 加固项，但不阻断真实使用。

## 38. 下一版本优先级

建议只实现：

```text
Transfer Rate Visualization
```

暂不增加：

- 并发上传；
- 历史版本；
- Remote Lock；
- FTP Rollback；
- 更多 Hybrid Mapping；
- 自动网络诊断；
- Metrics Server。

## 39. 最终建议

> 将 `v1.5.3` 作为部署安全与状态机稳定基线，将 `v1.6.0` 定位为纯可观测性版本。

核心原则：

```text
不改部署语义
不改远端状态
不改恢复模型
只增强用户对上传过程的判断能力
```

实现完成后，用户将能够通过：

- 当前滑动速率；
- 单文件平均速率；
- 全局平均速率；
- Payload/Wire 差异；
- Retry 次数；

快速区分：

```text
网络慢
服务器慢
大量小文件
连接抖动
重复传输
```

而不会给工具引入新的部署风险。
