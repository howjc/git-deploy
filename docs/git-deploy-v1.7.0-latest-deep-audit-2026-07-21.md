# git-deploy v1.7.0 最新代码深度审计报告

> 仓库：`howjc/git-deploy`  
> 分支：`main`  
> 最新提交：`9b00e93fbe8b8ab219b22d606359ec8f765a04de`  
> 功能提交：`4e27a611bf25f5f5e0b66de1219fce261a48aeec`  
> FTP 加固提交：`feb83552b4cd9cbbc4f7412cf5c092f56fd51adf`  
> 版本：`v1.7.0`  
> 审计日期：`2026-07-21`  
> 代码审计结论：**有条件通过**  
> Release Gate：**未闭合——Tag 与 CI 证据不完整**  
> 建议动作：发布一个小型 `v1.7.1`，收口 Target Filter、Git Repository Gate 和 Bootstrap 异常隔离。

---

# 1. 执行摘要

v1.7.0 已经完成 FTP Hybrid 一键初始化能力：

```bash
git-deploy bootstrap
git-deploy bootstrap --yes
git-deploy bootstrap --force --yes
git-deploy bootstrap prod staging --yes
```

实现并不是简单循环调用 Doctor，而是新增了独立的 Bootstrap 编排模型：

- 自动识别 Project / Workspace；
- 枚举所有 Repository 和 Target；
- 只选择 FTP + Hybrid；
- 跳过 SFTP、非 Hybrid 和被过滤 Target；
- 检查凭据环境变量；
- 解析运行期 Target；
- 连接远端；
- 检查 Remote Root；
- 检查本地 Capability Profile；
- 区分 READY / PROBE / REPROBE / CREATE ROOT + PROBE；
- 输出统一 Plan；
- 整批只确认一次；
- 顺序执行；
- 每个 Target 使用部署同款本地锁；
- 单 Target 失败后继续；
- 最终统一 Summary；
- 任一失败返回非零；
- 二次执行对有效 Profile 保持幂等。

同时，v1.7.0 关闭了上一轮提出的两个 FTP 加固项：

1. `OPTS UTF8 ON` 只接受 `500/501/502/504` 作为命令不支持；
2. Banner 不再整行删除，而是字段脱敏，并拒绝空的稳定身份材料。

Bootstrap 没有调用：

- Build；
- Freeze；
- Source/Output 上传；
- Hybrid 业务上传；
- Adoption；
- Ownership；
- Pending 写入；
- Deployment State；
- after_deploy；
- Prune。

执行阶段还会在 Target Lock 内重新检查 FTP Pending，`--force` 不会绕过中断部署保护。

因此，本轮没有发现：

```text
P0：无
未知业务内容删除：无
Ownership 扩张：无
State 提前提交：无
Pending 覆盖：无
```

但新增命令仍存在一个 P1：

## P1：不存在的 Target Filter 会静默成功

执行：

```bash
git-deploy bootstrap prdo --yes
```

其中 `prdo` 是 `prod` 的拼写错误。

当前行为：

- 所有真实 Target 都被标记为 `SKIP filtered`；
- Mutation Count 为 0；
- 不要求确认；
- Summary 全部 SKIP；
- Exit Code 为 0。

自动化会认为初始化成功，但实际上没有任何 Target 被初始化。

另外存在三个 P2：

1. 非 Git 项目退化到伪 `.git` State Base 后仍可执行远端 Probe；
2. Transport Factory 在加锁后、异常保护前执行，异常可能泄漏锁并中止整个 Batch；
3. READY / Pending / Profile 状态只在 Preflight 决定，确认窗口后不会统一重新验证。

发布完整性方面：

- `main` 的 Package Version 为 `1.7.0`；
- 当前 GitHub Connector 无法解析 `v1.7.0` Tag；
- 最新提交没有关联 Workflow Run 或 Combined Status；
- 当前审计环境仍无法解析 `github.com`，无法独立 Clone 和复跑测试。

综合结论：

> **v1.7.0 的核心设计和安全边界成立，但 Bootstrap 的成功语义仍需 v1.7.1 收口。**

---

# 2. 最新版本与变更范围

## 2.1 最新提交

```text
9b00e93fbe8b8ab219b22d606359ec8f765a04de
release v1.7.0: FTP Hybrid bootstrap and OPTS/banner hardening
```

## 2.2 Bootstrap 功能提交

```text
4e27a611bf25f5f5e0b66de1219fce261a48aeec
feat(bootstrap): add FTP Hybrid one-shot remote initialization
```

## 2.3 FTP 加固提交

```text
feb83552b4cd9cbbc4f7412cf5c092f56fd51adf
```

完成：

```text
OPTS 永久错误白名单
Banner 字段脱敏
空 Banner Identity 拒绝
```

## 2.4 主要变更文件

```text
src/git_deploy/bootstrap.py          +800
tests/test_bootstrap.py              +885
src/git_deploy/cli.py                +130
src/git_deploy/ftp_hybrid.py         +94
src/git_deploy/transports/ftp.py     +73
tests/test_cli.py
tests/test_ftp_hybrid.py
tests/test_transports.py
README / Release Notes / Audit Docs
```

没有修改：

```text
Planner
Deployer 主状态机
Ownership Schema
Pending Schema
Recovery Schema
State Schema
SFTP Hybrid Executor
FTP Hybrid Executor
```

---

# 3. 发布与验证状态

## 3.1 Package Version

Main：

```toml
version = "1.7.0"
```

```python
__version__ = "1.7.0"
```

## 3.2 Tag

本轮通过 GitHub Connector 读取：

```text
ref = v1.7.0
```

返回：

```text
No commit found for the ref v1.7.0
```

因此当前只能确认 Main 已更新到 1.7.0，不能确认正式 Tag 已创建并指向相同 Blob。

## 3.3 CI

以下提交均没有返回关联 Workflow Run：

```text
9b00e93...
4e27a611...
feb83552...
```

Combined Status 同样为空。

不能声称：

```text
v1.7.0 CI Verified
```

## 3.4 独立复跑限制

本轮再次尝试：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

结果：

```text
Could not resolve host: github.com
```

因此无法独立执行：

```bash
uv lock --check
uv run --isolated --python 3.11 --all-groups pytest -q
uv run --isolated --python 3.12 --all-groups pytest -q
uv run ruff check src tests
uv run ty check src
uv build --clear
```

仓库新增了大量针对性测试，但本轮无法独立确认其动态执行结果。

---

# 4. FTP 加固审计

## 4.1 OPTS 永久错误白名单

当前只接受：

```text
500
501
502
504
```

作为：

```text
OPTS UTF8 ON 命令未知或未实现
```

其他错误：

```text
530
532
550
553
临时错误
网络错误
```

全部 Fail Closed。

这关闭了 v1.6.3 审计中“所有 error_perm 都被当作 always-on UTF-8”的问题。

## 4.2 UTF-8 能力仍需真实证明

即使 OPTS 返回允许的 Unsupported Code，仍然必须：

- FEAT 广告 UTF8；
- MLSD 可用；
- 中文文件名精确往返；
- NFC/NFD 两个名称独立存在；
- 大小写名称独立存在；
- RETR；
- Rename；
- Rename Replace；
- DELE；
- RMD；
- Binary Round-trip。

没有把 Pure-FTPd 兼容变成无条件放宽。

## 4.3 Banner 字段脱敏

当前脱敏：

```text
You are user number 1 of 50 allowed.
    ↓
You are user number <n> of <n> allowed.

Local time is now 09:01. Server port: 21.
    ↓
Local time is now <time>. Server port: 21.
```

保留：

- 行结构；
- 端口；
- Node 信息；
- 产品信息；
- TLS/privsep；
- Welcome Policy；
- Inactivity Policy。

这比 v1.6.3 的整行删除更可靠。

## 4.4 空身份材料拒绝

规范化后为空：

```text
DeployError:
FTP server banner lacks stable identity material
```

避免多个“无稳定 Banner”的服务器都退化成空字符串 Hash。

## 4.5 结论

> **上一轮两个 FTP P2 已关闭。**

---

# 5. Bootstrap 架构审计

## 5.1 独立职责

```text
init
    → 纯本地配置模板

bootstrap
    → 远端 FTP Hybrid Runtime 初始化

deploy
    → 业务内容发布
```

`init` 的旧语义没有被破坏。

## 5.2 Candidate 枚举

Project：

- 按 Target 名称稳定排序；
- Target Filter；
- 无 Hybrid → SKIP；
- 非 FTP → SKIP；
- FTP + Hybrid → Candidate。

Workspace：

- 按 Workspace Repository 顺序；
- 每个 Repository 加载自己的 Config；
- 每个 Repository 使用自己的 Git Common Dir；
- 每个 Target 使用自己的 Profile 路径。

## 5.3 Read-only Preflight

Preflight 只执行：

- 本地 Config/Git/Env 检查；
- Target Resolution；
- FTP Connect；
- Banner Hash；
- Root Exists；
- Profile Inspect；
- Pending Read（VALID Profile 路径）。

不执行：

- Root 创建；
- Probe 文件创建；
- Profile 写入；
- State 写入；
- 业务上传。

## 5.4 Plan

统一展示：

```text
READY
PROBE
REPROBE
CREATE ROOT + PROBE
SKIP
FAIL
```

同时统计：

- Root Create；
- Probe；
- Existing Profile；
- Skip；
- Precheck Failure。

## 5.5 单次确认

有远端 Mutation 时：

- TTY 请求一次确认；
- 非 TTY 必须 `--yes`；
- 无 Mutation 不确认。

## 5.6 顺序执行

首版没有引入并发，符合个人工具定位。

## 5.7 Target Lock

执行每个变更 Target 前取得：

```text
<git-common-dir>/git-deploy/<target>.lock
```

与 Deploy 共用同一锁命名空间。

## 5.8 Pending Gate

执行阶段在锁内重新读取 Pending。

存在：

```text
PREPARED
FILES_PUBLISHED
PRUNED
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

任何 Phase 都拒绝 Bootstrap，要求先完成 Deploy/Recover。

## 5.9 Probe Service 复用

Doctor 与 Bootstrap 共用：

```python
inspect_capability_profile()
probe_and_save_ftp_hybrid_capabilities()
```

没有复制第二套 Capability Probe。

## 5.10 Continue-on-error

正常 Probe/Connect/Remote Error 被转成 `BootstrapResult(False)`，后续 Target 继续执行。

最终任何失败：

```text
exit 1
```

成功 Target 的 Profile 保留。

---

# 6. 安全边界验证

## 6.1 远端写入范围

Bootstrap 只允许：

```text
Configured Remote Root（缺失时）
.git-deploy/ftp-probe/<random-id>
```

## 6.2 不写业务内容

没有调用：

```text
execute_plan
execute_prepared
run_build
freeze
write_ownership
write_pending
state_store.save
after_deploy
```

## 6.3 Profile

只有完整 Probe 成功并清理后才原子写本地 Profile。

## 6.4 密码

Plan/Summary 只展示：

```text
host:remote_root
```

不展示：

- Password；
- 环境变量值；
- URL Credential；
- Session。

## 6.5 Root Alias

Probe 继续在创建 `.git-deploy` 前检查：

```text
.GIT-DEPLOY
Unicode/Casefold Equivalent
```

## 6.6 结论

> **没有发现 Bootstrap 触碰未知业务路径、Ownership 或 State。**

---

# 7. P1-01：未知 Target Filter 静默成功

## 7.1 严重性

```text
等级：P1
类型：Command Success Contract
影响：自动化与人工可能误判初始化完成
```

## 7.2 当前逻辑

用户传入：

```bash
git-deploy bootstrap prdo --yes
```

Filter Set：

```text
{"prdo"}
```

Candidate 枚举只遍历配置中真实 Target：

```text
prod
staging
```

由于两者不在 Filter Set：

```text
prod     → SKIP filtered
staging  → SKIP filtered
```

代码没有创建一个：

```text
UNKNOWN TARGET prdo
```

的错误项，也没有校验 Filter 是否至少匹配一个 Target。

## 7.3 后续结果

```text
mutation_count = 0
confirm = skipped
execute SKIP items
all success = True
exit code = 0
```

最终可能输出：

```text
SKIP project/prod     filtered
SKIP project/staging  filtered

ready:   0
skipped: 2
failed:  0
```

Shell/CI 会认为 Bootstrap 成功。

## 7.4 实际影响

- Capability Profile 没有创建；
- 用户以为所有目标已经初始化；
- 后续 Deploy 才因 Profile Missing 失败；
- 自动初始化脚本不会立刻暴露拼写错误；
- Workspace 中问题更加隐蔽。

## 7.5 修复

在任何远端连接前校验：

```python
requested = set(target_filter)
known = union(all repository config target names)
unknown = requested - known
if unknown:
    raise ConfigError(
        "unknown bootstrap target filter(s): "
        + ", ".join(sorted(unknown))
    )
```

Workspace 规则：

- Filter 只要在任一 Repository 中存在即可；
- 没有该 Target 的 Repository 不产生错误；
- 一个 Filter 在整个 Workspace 都不存在才拒绝。

## 7.6 测试

```text
Project unknown filter → exit 2, zero connect
Workspace unknown global filter → exit 2, zero connect
Mixed prod + typo → reject typo，不能部分执行
Empty filter → existing behavior
```

---

# 8. P2-01：非 Git 项目退化到伪 `.git` 目录

## 8.1 当前逻辑

Candidate 枚举：

```python
try:
    git_dir = repository.common_dir()
except PlanError:
    git_dir = project_root / ".git"
```

随后：

```python
StateStore(git_dir).base
```

可能变成：

```text
<project>/.git/git-deploy
```

即使 `<project>` 并不是 Git Repository。

## 8.2 影响

Bootstrap 可以：

- 创建 `.git/git-deploy`；
- 创建 Lock；
- 保存 Capability Profile；
- 连接并写远端 Probe；

但后续真正 Deploy 会因为不是 Git Worktree 而失败。

这会产生：

```text
Bootstrap 显示成功
Project 实际不可部署
```

并让 `.git` 成为一个并非 Git Metadata 的普通目录。

## 8.3 安全性

远端仍只写保护 Probe，不会删除业务内容，因此定级 P2。

## 8.4 修复

Project 模式：

```python
repository.validate()
git_dir = repository.common_dir()
```

失败：

```text
FAIL_PRECHECK
not a Git worktree
```

Workspace 模式建议将坏 Repository 转成独立失败行，并继续其他 Repository，而不是让整个 Workspace Abort。

## 8.5 测试

```text
Project non-git → zero connect / zero local .git creation
Workspace one non-git → that repo FAIL，later repo still probes
Broken worktree common-dir → FAIL
Linked worktree → uses common-dir
```

---

# 9. P2-02：Transport Factory 异常可能泄漏锁并中止 Batch

## 9.1 当前执行顺序

```python
lock.acquire()
transport = factory(item.target)
try:
    ...
except:
    ...
finally:
    lock.release()
```

`factory()` 位于保护 `try` 之前。

## 9.2 失败效果

如果 Factory 抛出：

```text
ConfigError
ImportError
Unexpected construction error
```

则：

- `lock.release()` 不执行；
- `execute_bootstrap_item()` 异常逃逸；
- `execute_bootstrap()` 的 Tuple Comprehension 中止；
- 后续 Target 不再执行；
- 当前进程退出前锁文件句柄仍持有；
- 违反“单目标失败后继续”。

内置 FTP Factory 对已验证 Target 一般不会抛出，因此不是常规路径，但结构不满足声明的不变量。

## 9.3 修复

```python
lock.acquire()
transport = None
try:
    transport = factory(item.target)
    ...
except Exception as exc:
    return BootstrapResult(...)
finally:
    if transport is not None:
        transport.close()
    lock.release()
```

Preflight 的 Factory Construction 也建议放进异常转换范围。

## 9.4 测试

```text
First factory raises
First result = FAIL
Second target still runs
Lock can immediately reacquire
Transport connect failure closes/release
```

---

# 10. P2-03：Preflight 与执行结果可能陈旧

## 10.1 READY

READY Item 在执行时直接返回 Success：

```text
不加锁
不连接
不重新检查 Profile
不重新检查 Pending
```

如果确认窗口中：

- Profile 被删除；
- Banner 变化；
- Pending 出现；
- Target Config 被外部修改；

Summary 仍显示 READY。

没有远端写入，因此安全，但结果可能不再真实。

## 10.2 Force / Invalid Profile Pending

Preflight 只在 Profile 为 VALID 且非 Force 时检查 Pending。

对于：

```text
--force
Profile Missing
Old Schema
Corrupt
Banner Drift
```

Plan 可能显示 REPROBE，而不显示 Pending。

执行阶段在 Lock 内会正确阻断，所以不会覆盖 Pending，但 Plan 不是完全准确。

## 10.3 修复选择

### 最简方案

保持执行 Gate，Plan 对非 VALID Profile 显示：

```text
REPROBE (Pending will be checked at execution)
```

READY 在执行时至少：

- 加 Target Lock；
- 重新连接；
- Inspect Profile；
- 检查 Pending；
- 返回 READY 或 FAIL。

### 更轻方案

接受 READY 为 Preflight Snapshot，但 Summary 改名：

```text
PLANNED READY
```

不推荐，因为用户期待最终结果。

## 10.4 定级

```text
P2
```

---

# 11. P2-04：Workspace 配置错误是全局 Fail-fast

Workspace 枚举：

```python
for repository in workspace.repositories:
    config = load_config(repository.config_path)
```

任何 Repository Config 错误会在 Plan 形成前终止整个命令。

安全上很好：

```text
Remote Mutation = 0
```

但与“一仓失败、其余继续”的用户预期有差异。

建议区分：

```text
Workspace Structure Invalid
    → 全局失败

Single Repository Config/Git Invalid
    → Synthetic FAIL_PRECHECK Row
    → 其他 Repository 继续
```

定级：

```text
P2 / UX
```

可与非 Git Gate 一起处理。

---

# 12. P3：输出与体验细节

## 12.1 固定列宽

长 Repository、Target、Endpoint 会挤压 Plan 表格。

不影响操作。

## 12.2 READY 与新 Profile 都显示 READY

Summary 中：

```text
READY frontend/prod existing profile valid
READY admin/prod profile saved
```

Detail 已能区分，状态名称可以保持。

## 12.3 Root 创建后 Probe 失败

Bootstrap 不回滚新创建的 Remote Root。

这是合理边界：

- Root 是用户确认创建的；
- 递归删除可能破坏并发新内容；
- 重跑可继续 Probe。

应在文档明确：

```text
Create Root 成功、Probe 失败时 Root 保留
```

---

# 13. 测试覆盖评价

## 13.1 已覆盖

- Profile Missing/Valid/Old/Corrupt/Banner Drift；
- FTP/SFTP/No Hybrid 枚举；
- Target Filter 正常路径；
- Missing Password；
- READY/PROBE/REPROBE；
- `--force`；
- Root Create；
- `--no-create-root`；
- One Confirmation；
- Non-TTY `--yes`；
- Two Targets；
- Continue-on-error for Probe Failure；
- Idempotent Second Run；
- Workspace Independent State；
- No Business Side Effects；
- Password Not Printed；
- CLI Flag Validation；
- Doctor Service Regression。

## 13.2 缺失

- Unknown Target Filter；
- Mixed Valid + Unknown Filter；
- Non-Git Project；
- Workspace Non-Git Repository；
- Workspace Broken Config Continue；
- Factory Exception after Lock；
- Lock Reacquire；
- READY Profile Deleted after Plan；
- READY Pending Appears after Plan；
- Force Plan with Pending；
- Tag/CI Release Gate。

---

# 14. v1.7.1 原子整改计划

## P1：Target Filter Contract

### TODO-001

- [ ] 收集 Project/Workspace Known Target Names；
- [ ] 计算 Unknown Filters；
- [ ] Unknown 非空时 ConfigError；
- [ ] 错误列出全部 Unknown Name；
- [ ] Remote Connect = 0。

---

## P2：Git Repository Gate

### TODO-101

- [ ] 删除 `.git` Fallback；
- [ ] 调用 `repository.validate()`；
- [ ] 使用真实 `common_dir()`；
- [ ] Project Invalid → Fail；
- [ ] Workspace Invalid Repo → FAIL Row；
- [ ] 不创建伪 `.git`。

---

## P2：异常隔离

### TODO-201

- [ ] Factory 放入 Try；
- [ ] Transport 可空；
- [ ] Close 放入 Finally；
- [ ] Lock 总是 Release；
- [ ] Unexpected Error 转 Result；
- [ ] Batch 继续。

---

## P2：Final Freshness

### TODO-301

- [ ] READY 执行阶段加锁；
- [ ] 重新 Inspect Profile；
- [ ] 重新 Pending Check；
- [ ] Banner Drift → FAIL；
- [ ] Missing Profile → FAIL 或 Probe（建议 FAIL，尊重已确认 Plan）。

### TODO-302

- [ ] Plan 标注 Pending 执行时检查；
- [ ] Force 不绕过 Pending；
- [ ] 测试确认窗口状态变化。

---

## Release Gate

### TODO-401

- [ ] 创建 `v1.7.0` 或修复后创建 `v1.7.1` Tag；
- [ ] Tag 与 Main Package Blob 一致；
- [ ] Python 3.11 CI；
- [ ] Python 3.12 CI；
- [ ] Lock Check；
- [ ] Ruff；
- [ ] ty；
- [ ] Build；
- [ ] Isolated Wheel；
- [ ] CLI Bootstrap Smoke。

---

# 15. 修复后验收标准

1. `bootstrap prdo --yes` 必须失败；
2. Unknown Filter 失败前 Remote Connect = 0；
3. Project 非 Git 时不创建 `.git`；
4. Workspace 一个非 Git Repo 不阻断其他 Repo；
5. Factory 抛错后 Lock 可重获；
6. Factory 抛错后后续 Target 继续；
7. READY 在执行时重新验证；
8. Pending 在确认窗口出现时阻断；
9. Bootstrap 永不写 State/Ownership/Pending；
10. Bootstrap 二次执行 Valid Profile 不 Probe；
11. `--force` 重新 Probe；
12. Root Missing 默认创建；
13. `--no-create-root` 零写入；
14. Python 3.11/3.12 CI 通过；
15. 正式 Tag 可解析。

---

# 16. 当前使用建议

## 可以使用

```bash
git-deploy bootstrap --yes
git-deploy bootstrap --force --yes
```

适合：

- 当前工作区确认全部 Repository 都是有效 Git Repo；
- 不使用拼写不确定的 Target Filter；
- 使用全部 Target 或已人工确认的准确名称；
- 单进程顺序初始化；
- 真实 Pure-FTPd；
- v1.6.x Profile 迁移。

## 暂时避免

```bash
git-deploy bootstrap prdo --yes
```

自动脚本中使用 Filter 前，应先确认名称来自配置。

不要在：

- 非 Git 目录；
- 部分 Workspace Repo 配置损坏；
- 另一个进程正在部署同一 Target；

情况下依赖当前 Summary 表示全部最终状态。

## 推荐首次流程

```bash
git-deploy bootstrap --yes
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

---

# 17. 最终结论

v1.7.0 成功实现了用户提出的核心目标：

> 用一条命令初始化单仓多 Target 或 Workspace 多仓的全部 FTP Hybrid 远端能力。

设计方面做对了：

- 没有污染 `init`；
- 没有复制 Doctor Probe；
- 有统一 Plan；
- 一次确认；
- 顺序执行；
- Target Lock；
- Pending Fail Closed；
- 幂等 Profile；
- Best-effort Summary；
- 不触碰业务内容与部署 State。

同时也关闭了上一轮 FTP OPTS 和 Banner P2。

本轮没有发现 P0，也没有 FTP Hybrid 删除安全回归。

但：

- Unknown Target Filter 静默返回成功；
- 非 Git Repo 仍可执行远端 Bootstrap；
- Factory 异常可能破坏 Lock 与 Batch Continue；
- Release Tag/CI 尚未确认。

因此：

> **git-deploy v1.7.0 有条件通过。**

建议：

> **保留当前 v1.7.0 设计，不扩展新功能；发布一个小型 v1.7.1，完成 Target Filter、Git Gate、异常隔离和最终 Freshness 收口，再将 Bootstrap 作为稳定工作流。**
