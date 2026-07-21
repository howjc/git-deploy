# git-deploy v1.6.3 最新代码审计与 FTP Hybrid 一键初始化方案

> 仓库：`howjc/git-deploy`  
> 当前分支：`main`  
> 最新提交：`f97b83bae06a527442b136777e799349d239ca75`  
> 当前版本：`v1.6.3`  
> 上一审计基线：`v1.6.1`  
> 方案目标版本：`v1.7.0`  
> 审计与方案日期：`2026-07-21`  
> 综合结论：**最新代码有条件通过；建议新增 `git-deploy bootstrap` 简化 Project/Workspace 的 FTP Hybrid 初始化**

---

# 1. 执行摘要

自 v1.6.1 后，主分支新增了两轮 FTP Hybrid 兼容修复：

```text
v1.6.2
    Pure-FTPd always-on UTF-8 兼容

v1.6.3
    Pure-FTPd 易变 Welcome Banner 规范化
```

最新 `main`：

```text
f97b83bae06a527442b136777e799349d239ca75
release v1.6.3: stabilize Pure-FTPd banner hash for FTP Hybrid
```

本轮代码审计没有发现新的 P0/P1。

v1.6.2 与 v1.6.3 的核心方向是正确的：

- 仍然要求 FTP Server 在 FEAT 中广告 `UTF8`；
- Pure-FTPd 对 `OPTS UTF8 ON` 返回永久 5xx 时，允许走 always-on UTF-8 兼容路径；
- 临时性、网络性 OPTS 错误仍然 Fail Closed；
- 实际路径能力仍由中文名、NFC/NFD、大小写、MLSD、RETR、Rename、Delete 等真实 Probe 证明；
- Sticky UTF-8 重连仍然重复检查 FEAT、Banner 和会话编码；
- Capability Profile 仍然绑定 Target Fingerprint 与 Server Banner；
- Pure-FTPd Welcome 中在线用户数和本地时间不再导致每次连接都要求重新 Probe；
- 稳定 Welcome 内容仍然参与 Banner Hash。

但是，本轮仍有两个 P2 加固建议：

1. v1.6.2 当前接受所有永久 `error_perm`，建议只接受明确表示命令不支持的 500/501/502/504；
2. v1.6.3 当前直接删除整个易变 Banner 行，极端情况下可能使规范化 Banner 过弱，建议改为字段脱敏替换，并拒绝空的稳定身份材料。

发布验证方面：

- v1.6.2、v1.6.3 是直接发布提交；
- 当前没有关联的 GitHub Actions Workflow Run；
- 审计环境仍无法解析 `github.com`，不能独立 Clone 和复跑测试。

因此：

```text
代码审计：
    通过

Release CI Gate：
    未独立闭合

综合：
    有条件通过
```

针对用户当前痛点，建议新增：

```bash
git-deploy bootstrap --yes
```

它负责：

- 自动识别当前是单 Project 还是 Workspace；
- 枚举所有配置中的 FTP Hybrid Target；
- 检查远端 Root 与本地 Capability Profile；
- 跳过已就绪目标；
- 对缺失、旧 Schema、Banner 变化或 `--force` 的目标执行 Probe；
- 必要时创建缺失的配置 Root；
- 为每个 Repository/Target 独立保存 Profile；
- 一个目标失败后继续初始化其他目标；
- 最终输出统一 Summary；
- 整个批次只确认一次。

它不负责：

- Build；
- 业务文件上传；
- Adoption；
- Ownership 创建；
- Deployment State；
- 首次正式部署。

---

# 2. 最新代码范围

## 2.1 v1.6.2

提交：

```text
698e9062df1270e80519ca55285b0b28fac78c2e
release v1.6.2: accept Pure-FTPd always-on UTF-8 for FTP Hybrid
```

核心变化：

```text
FEAT 包含 UTF8
    ↓
尝试 OPTS UTF8 ON
    ↓
2xx
    → 正常启用 UTF-8

永久 5xx
    → 视为 Pure-FTPd always-on UTF-8
    → 客户端 encoding = utf-8
    → 继续真实路径 Probe

临时错误 / 网络错误
    → Fail Closed
```

## 2.2 v1.6.3

提交：

```text
f97b83bae06a527442b136777e799349d239ca75
release v1.6.3: stabilize Pure-FTPd banner hash for FTP Hybrid
```

规范化掉：

```text
You are user number N of M allowed.
Local time is now ...
```

继续保留：

```text
Pure-FTPd 产品信息
TLS/privsep 标识
Private system 提示
Inactivity 策略
其他稳定 Welcome 内容
```

## 2.3 变更规模

从 v1.6.1 到 v1.6.3，只修改：

```text
README / ADR / Migration / Release Notes
pyproject / __version__ / uv.lock
src/git_deploy/transports/ftp.py
tests/test_transports.py
```

没有修改：

```text
Planner
Ownership
Pending
Recovery
State
Deployer
Progress Reporter
Workspace
SFTP/Native Transport
```

---

# 3. v1.6.2 审计

## 3.1 已正确实现

### FEAT 仍是强制前提

服务器没有广告：

```text
UTF8
```

仍然拒绝。

这避免了在完全没有 UTF-8 能力声明的 FTP Server 上直接猜测。

### 临时错误仍然 Fail Closed

例如：

```text
421 Service not available
连接中断
控制通道临时错误
```

不会被当作 always-on UTF-8。

### 路径能力仍由真实 Probe 证明

即使 OPTS 被永久拒绝，Capability Profile 仍必须通过：

- 中文文件名；
- NFC 与 NFD 两个独立名称；
- 大小写变体；
- MLSD 精确返回；
- RETR；
- Rename；
- Delete；
- Binary Round-trip；
- Cross-directory Rename；
- Rename Replace；
- RMD。

因此系统不是仅凭：

```text
FEAT UTF8
```

就信任服务器路径语义。

### Sticky 重连仍然存在

一次 `enable_utf8()` 成功后，Transport 的后续新连接仍会：

- 校验 Banner；
- 重新读取 FEAT；
- 再次尝试 OPTS；
- 或再次走 always-on 兼容路径；
- 设置客户端 `encoding = utf-8`。

## 3.2 P2：永久 OPTS 拒绝范围过宽

当前逻辑相当于：

```python
except ftplib.error_perm:
    pass
```

也就是任何 5xx 都被解释为：

```text
命令不支持，但 UTF-8 永久启用
```

但 5xx 还可能表示：

```text
530 Not logged in
532 Need account
550 Permission denied
553 Policy / filename rejection
```

虽然 FEAT 和后续真实 Probe 会降低风险，但在已经存在 Profile 的重连路径上，不会每次重复完整 Unicode Probe。

### 建议

只接受明确的“命令未知或不实现”：

```text
500
501
502
504
```

其他永久错误：

```text
Fail Closed
```

建议函数：

```python
def _is_opts_utf8_unsupported(exc: ftplib.error_perm) -> bool:
    code = str(exc).partition(" ")[0]
    return code in {"500", "501", "502", "504"}
```

### 定级

```text
P2
```

不阻断当前宝塔 Pure-FTPd 使用。

---

# 4. v1.6.3 审计

## 4.1 已正确实现

原始 Banner 中：

```text
用户在线序号
当前本地时间
```

确实不是服务器身份，不应让 Capability Profile 每次连接失效。

规范化后仍保留稳定内容，正常情况下可以继续识别：

- FTP Server 产品变化；
- TLS/privsep 配置变化；
- Welcome Policy 变化；
- 服务器软件替换。

测试覆盖了：

```text
上午 / 晚上
用户 1 / 用户 12
```

Hash 保持一致。

## 4.2 P2：建议用脱敏替换代替整行删除

当前：

```text
Local time is now ...
```

整行删除。

如果服务器把稳定字段放在同一行：

```text
Local time is now 09:01. Server port: 21. Node: ftp-a.
```

则：

```text
Server port
Node identity
```

也会一起被删除。

更稳妥的输出：

```text
You are user number <n> of <n> allowed.
Local time is now <time>. Server port: 21. Node: ftp-a.
```

保留行结构和稳定后缀，只脱敏具体数字/时间。

## 4.3 P2：规范化身份可能为空

如果一个 FTP Server 的 Welcome 只有易变行，规范化结果可能是：

```text
""
```

此时 Hash 会变成固定的空字符串 SHA256。

同一 Target 地址后端被替换，但新旧 Welcome 都只包含被删除行时，Banner Hash 无法识别变化。

### 建议

选择一个：

#### 方案 A：字段替换

不删除整行，因此通常不会为空。

#### 方案 B：空结果拒绝

```python
normalized = normalize_ftp_server_banner(raw)
if not normalized:
    raise DeployError("FTP server banner lacks stable identity material")
```

#### 方案 C：增加稳定能力身份

Profile 额外绑定：

```text
normalized banner
sorted FEAT set
```

首选：

```text
方案 A + 空结果拒绝
```

### 定级

```text
P2
```

---

# 5. 最新版本总体结论

## 5.1 没有发现

- 未知远端内容删除；
- Ownership 扩张；
- Pending 状态倒退；
- State 提前提交；
- UTF-8 临时错误被接受；
- Banner 的全部内容无条件忽略；
- SFTP/Native 回归；
- Deployment 顺序变化。

## 5.2 仍继承的非阻断项

### FTP Hybrid Retry Counter

极少数组合失败中，Retry 数可能比实际新增 Upload Attempt 多 1。

### FILES_PUBLISHED Plan / Executor

Planner 可能仍显示 Upload，而 Executor 从该阶段直接进入 Prune。

### Recovery Alias Cache 顺序

Recovery Alias Gate 的 Cache Refresh 顺序仍可进一步统一。

### Doctor UTF-8 / Alias 展示

Doctor 的诊断输出与正式部署门禁还可以进一步对齐。

这些问题均不阻断当前主流程。

---

# 6. 发布验证边界

v1.6.2 和 v1.6.3 当前没有关联 Workflow Run。

不能声称：

```text
v1.6.3 CI Verified
```

当前能确认的是：

- 代码变更范围小；
- 有针对性单元测试提交；
- 无状态机改动；
- 审计未发现 P0/P1。

当前审计环境无法独立 Clone：

```text
Could not resolve host: github.com
```

建议修复 Actions Billing/Runner 后，对：

```text
f97b83bae06a527442b136777e799349d239ca75
```

或 `v1.6.3` Tag 执行完整 CI。

---

# 7. 为什么不应复用 `git-deploy init`

当前 `git-deploy init` 的产品承诺是：

```text
生成本地配置模板
不连接服务器
不读取密码
不写远端
```

而 FTP Hybrid 初始化需要：

```text
读取凭据环境变量
连接远端
执行 FEAT / MLSD
创建临时 Probe
STOR / RETR / Rename / Delete / RMD
写入本地 Capability Profile
可能创建 Remote Root
```

直接把这些行为加入 `init` 会使一个原本纯本地、安全无副作用的命令突然拥有远端写能力。

这会导致：

- 用户对 `init` 的安全预期失效；
- 脚本中已有 `init` 行为被改变；
- Config Template 和 Remote Bootstrap 两种生命周期混在一起；
- 错误信息与参数复杂化。

因此建议：

```text
init
    = Local Config Initialization

bootstrap
    = Remote Runtime Initialization
```

---

# 8. 推荐命令

## 8.1 默认

```bash
git-deploy bootstrap
```

行为：

- 自动识别 Project 或 Workspace；
- 枚举全部符合条件的 FTP Hybrid Target；
- 做只读预检；
- 输出批次 Plan；
- 请求一次统一确认；
- 顺序执行所有需要的 Probe；
- 输出统一 Summary。

非交互：

```bash
git-deploy bootstrap --yes
```

## 8.2 只初始化指定 Target

```bash
git-deploy bootstrap prod staging
```

在 Workspace 中：

```text
只处理 Target 名称匹配 prod/staging 的 Repository
```

## 8.3 强制重新探测

```bash
git-deploy bootstrap --force --yes
```

用途：

- 升级 Capability Schema；
- Banner Normalization 迁移；
- 服务器配置变化；
- 人工重新验证。

## 8.4 Root 创建

推荐 Bootstrap 默认：

```text
缺失的配置 Remote Root
    → Plan 显示 CREATE ROOT
    → 统一确认后创建
```

因为命令本身就是显式 Remote Initialization。

保守模式：

```bash
git-deploy bootstrap --no-create-root
```

若希望首版参数更少，也可以固定：

```text
Bootstrap 默认创建缺失 Root
Doctor 默认不创建
```

---

# 9. Target 选择规则

一个 Target 只有同时满足以下条件才进入 Bootstrap：

```text
Repository Config 存在 Hybrid Output
Target Protocol = ftp
Target 配置有效
Target 凭据环境变量已配置
```

跳过：

```text
SFTP Target
普通 FTP Incremental Target
没有 Hybrid Output 的 Repository
被 Target Filter 排除的 Target
```

输出必须说明原因：

```text
SKIP sftp backend
SKIP no hybrid output
SKIP filtered
```

---

# 10. Read-only Preflight

在任何远端写入前，对全部候选执行：

1. 加载 Project/Workspace 配置；
2. 验证 Git Repository；
3. 解析 Target；
4. 检查密码环境变量是否存在；
5. 连接 FTP；
6. 读取并规范化 Banner；
7. 检查 Remote Root；
8. 尝试加载当前 Capability Profile；
9. 判断动作；
10. 关闭连接。

动作：

```text
READY
    当前 Schema、Target Fingerprint、Banner 均有效

PROBE
    Profile 缺失

REPROBE
    Schema 旧
    Profile 损坏
    Banner 变化
    Target Fingerprint 变化
    --force

CREATE_ROOT_AND_PROBE
    Remote Root 缺失

FAIL_PRECHECK
    凭据缺失
    无法连接
    配置错误
```

---

# 11. 统一 Plan

示例：

```text
FTP HYBRID BOOTSTRAP PLAN

REPOSITORY     TARGET     ENDPOINT                         ACTION
frontend       prod       ftp-a.example:/public_html       REPROBE
frontend       staging    ftp-b.example:/staging           READY
admin          prod       ftp-c.example:/admin             CREATE ROOT + PROBE
api            prod       sftp backend                     SKIP
legacy         prod       no hybrid output                 SKIP

Remote mutations:
  create root: 1
  capability probe: 2
  existing valid profiles: 1
  skipped: 2
```

只确认一次：

```text
Proceed with 3 FTP Hybrid initialization action(s)? [y/N]
```

`--yes` 跳过确认。

---

# 12. 执行语义

## 12.1 顺序执行

首版不要并发。

原因：

- 多服务器日志容易交错；
- 多个密码环境错误难以阅读；
- FTP Server 可能有限制；
- Remote Probe 本身较重；
- 失败恢复更容易理解；
- 个人工具不需要并发复杂度。

## 12.2 Best-effort All

一个 Target 失败：

```text
记录失败
清理本次 Probe
继续下一个 Target
```

最终：

```text
存在任一失败
    → Exit Non-zero
```

但已经成功的 Profile 保留。

## 12.3 独立提交

每个 Target：

- Probe Root 独立 UUID；
- Profile 独立原子写；
- 不存在跨 Target Transaction；
- 不回滚其他 Target 成功结果。

## 12.4 幂等

再次运行：

```text
有效 Profile
    → READY / SKIP

失败或旧 Profile
    → 重新 Probe
```

因此 Bootstrap 天然可重跑。

---

# 13. Bootstrap 不做什么

Bootstrap 必须严格禁止：

- Build；
- Freeze；
- Source Upload；
- Incremental Output Upload；
- Hybrid Business Upload；
- Prune；
- Adoption；
- Ownership 创建；
- Pending 创建；
- Deployment State 保存；
- after_deploy；
- 首次正式部署。

Bootstrap 完成后，用户仍需：

```bash
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

完成首次 Adoption 和业务部署。

---

# 14. Project 模式

Project Config：

```toml
[[outputs]]
name = "frontend-root"
mode = "hybrid"
local = ".deploy/frontend-root"
remote = "."

[targets.prod]
protocol = "ftp"

[targets.staging]
protocol = "ftp"
```

命令：

```bash
git-deploy bootstrap --yes
```

等价于自动完成：

```bash
git-deploy doctor prod --probe-ftp-hybrid --yes
git-deploy doctor staging --probe-ftp-hybrid --yes
```

但增加：

- Profile Valid Skip；
- 统一 Plan；
- 一次确认；
- 统一 Summary；
- Continue-on-error。

---

# 15. Workspace 模式

Workspace 中存在：

```text
frontend/
admin/
api/
activity/
```

每个 Repository 有独立：

- Git Common Dir；
- State Store；
- Capability Profile 路径；
- Config；
- Target；
- Remote Root。

Bootstrap：

```bash
git-deploy bootstrap --yes
```

自动使用当前已有的 Project/Workspace Config 识别逻辑。

流程：

```text
Workspace Load
    ↓
Repository Order
    ↓
Repository Config Load
    ↓
Eligible FTP Hybrid Targets
    ↓
Global Plan
    ↓
One Confirmation
    ↓
Sequential Bootstrap
```

每个 Repository 的 Profile 仍保存到自己的：

```text
<git-common-dir>/git-deploy/ftp-capabilities/
```

不能保存到 Workspace 公共目录后让所有仓库共享，因为 Profile 绑定：

- Target Fingerprint；
- Repository Runtime；
- 当前配置身份。

---

# 16. 重复远端处理

首版建议：

```text
一个 Repository/Target
    = 一个 Bootstrap Item
```

即使两个 Repository 指向同一个 FTP Server，也分别 Probe。

原因：

- Target Fingerprint 可能不同；
- Remote Root 可能不同；
- Local Profile 路径不同；
- Profile 身份模型当前是 Target 级；
- 跨仓 Probe Dedup 会引入复制和身份适配逻辑。

用户主要需要的是：

```text
一次命令
```

而不是：

```text
每个物理 FTP Server 只 Probe 一次
```

未来可安全优化：

```text
完全相同 Target Fingerprint + Remote Root
    → Probe 一次
    → 原子保存到多个相同 Profile 位置
```

不应放进 v1.7.0 首版。

---

# 17. 安全边界

## 17.1 凭据

Bootstrap：

- 只读取 `password_env`；
- 不打印密码；
- 不写配置明文；
- 不写日志；
- 不复制环境变量值到 Summary。

## 17.2 Remote Write

只允许：

```text
配置的 Remote Root
.git-deploy/ftp-probe/<uuid>
```

以及必要的：

```text
Remote Root 创建
.git-deploy 父目录创建
```

不允许业务路径写入。

## 17.3 Alias Gate

任何创建 `.git-deploy` 前继续运行：

```text
Remote Root Alias Gate
```

例如已有：

```text
.GIT-DEPLOY
```

则 Fail Closed。

## 17.4 锁

每个 Repository/Target 应取得与 Deploy 相同的本地锁。

避免：

```text
bootstrap
与
deploy
```

在同一 Target 同时运行。

## 17.5 Pending

Bootstrap 不修改 Pending。

建议发现 Pending 时：

```text
显示 WARNING
仍可执行 Capability Probe
```

如果希望更保守：

```text
Pending PRE-COMMIT
    → SKIP / FAIL
```

首版推荐 Fail Closed：

```text
检测到 Pending
    → 要求先完成 deploy/recover
```

但读取 Pending 依赖可用 Profile。对于没有 Profile 的首次 Bootstrap，可以在 Probe 成功后再次检查。

---

# 18. 数据模型

```python
class BootstrapAction(str, Enum):
    READY = "ready"
    PROBE = "probe"
    REPROBE = "reprobe"
    CREATE_ROOT_AND_PROBE = "create-root-and-probe"
    SKIP = "skip"
    FAIL_PRECHECK = "fail-precheck"
```

```python
@dataclass(frozen=True, slots=True)
class BootstrapItem:
    repository_name: str
    repository_root: Path
    config_path: Path
    target_name: str
    target: TargetConfig
    state_base: Path
    action: BootstrapAction
    reason: str
```

```python
@dataclass(frozen=True, slots=True)
class BootstrapResult:
    item: BootstrapItem
    success: bool
    profile_path: Path | None
    error: str | None
```

---

# 19. 模块设计

建议新增：

```text
src/git_deploy/bootstrap.py
```

职责：

- Project/Workspace Item 枚举；
- Read-only Preflight；
- Bootstrap Plan；
- Batch Confirmation；
- Sequential Execution；
- Summary；
- Exit Result。

不要把编排逻辑继续堆到：

```text
doctor.py
```

但应复用 Doctor/FTP Hybrid 已有服务：

```python
probe_ftp_hybrid_capabilities()
save_capability_profile()
load_capability_profile()
validate_remote_root_aliases()
```

建议抽取一个无 UI 的 Service：

```python
def bootstrap_ftp_hybrid_target(
    target: TargetConfig,
    state_base: Path,
    *,
    create_root: bool,
    force: bool,
) -> Path:
    ...
```

Doctor 和 Bootstrap 共同调用。

---

# 20. CLI 设计

## 20.1 基础

```bash
git-deploy bootstrap
```

## 20.2 非交互

```bash
git-deploy bootstrap --yes
```

## 20.3 Target Filter

```bash
git-deploy bootstrap prod staging
```

## 20.4 Force

```bash
git-deploy bootstrap --force --yes
```

## 20.5 Root Policy

```bash
git-deploy bootstrap --no-create-root
```

## 20.6 不建议的参数

首版不要增加：

- `--parallel`；
- `--workers`；
- `--deduplicate-remotes`；
- `--deploy-after-bootstrap`；
- `--adopt`；
- `--save-password`；
- `--ignore-failures`；
- `--unsafe`。

---

# 21. 输出 Summary

```text
FTP HYBRID BOOTSTRAP SUMMARY

READY   frontend/prod       profile saved
READY   frontend/staging    existing profile valid
READY   admin/prod          root created; profile saved
FAIL    activity/prod       MLSD unsupported
SKIP    api/prod            SFTP backend

ready:   3
skipped: 1
failed:  1
```

Exit：

```text
failed = 0
    → 0

failed > 0
    → 非 0
```

---

# 22. 原子 TODO

## Phase 1：服务抽取

### TODO-001

- [ ] 抽取 FTP Hybrid Probe Service；
- [ ] Doctor 继续调用；
- [ ] 行为不回归；
- [ ] Profile 路径保持不变。

### TODO-002

- [ ] Profile Status 检查；
- [ ] Missing；
- [ ] Valid；
- [ ] Old Schema；
- [ ] Corrupt；
- [ ] Target Drift；
- [ ] Banner Drift。

---

## Phase 2：Bootstrap Model

### TODO-101

- [ ] BootstrapAction；
- [ ] BootstrapItem；
- [ ] BootstrapResult；
- [ ] Stable Sort。

### TODO-102

- [ ] Project Target Enumerator；
- [ ] FTP Filter；
- [ ] Hybrid Filter；
- [ ] Target Filter。

### TODO-103

- [ ] Workspace Repository Enumerator；
- [ ] Repository Config Load；
- [ ] Repository Name；
- [ ] Independent State Base。

---

## Phase 3：Preflight

### TODO-201

- [ ] Credential Env Presence；
- [ ] Connect；
- [ ] Banner；
- [ ] Root Exists；
- [ ] Current Profile；
- [ ] Action Resolve；
- [ ] Close。

### TODO-202

- [ ] Missing Root；
- [ ] Create-root Plan；
- [ ] `--no-create-root` Failure。

---

## Phase 4：Plan 与确认

### TODO-301

- [ ] Global Table；
- [ ] Mutation Counts；
- [ ] Skip Reasons；
- [ ] One Confirmation；
- [ ] Non-TTY Requires `--yes`。

---

## Phase 5：执行

### TODO-401

- [ ] Sequential；
- [ ] Local Lock；
- [ ] Reconnect；
- [ ] Create Root；
- [ ] Probe；
- [ ] Atomic Profile Save；
- [ ] Close；
- [ ] Continue-on-error。

### TODO-402

- [ ] Cleanup Probe；
- [ ] Cleanup Error；
- [ ] No Business Mutation；
- [ ] No State Save。

---

## Phase 6：CLI

### TODO-501

- [ ] `bootstrap` Command；
- [ ] Positional Target Filter；
- [ ] `--yes`；
- [ ] `--force`；
- [ ] `--no-create-root`；
- [ ] Help Text。

### TODO-502

- [ ] Existing `init` unchanged；
- [ ] Existing `doctor` unchanged；
- [ ] Deploy Target Parsing unchanged。

---

## Phase 7：Tests

### TODO-601：Project

- [ ] Two FTP Targets；
- [ ] One Valid / One Missing；
- [ ] Single Confirmation；
- [ ] Correct Profiles。

### TODO-602：Workspace

- [ ] Multiple Repositories；
- [ ] Multiple Targets；
- [ ] SFTP Skip；
- [ ] Non-Hybrid Skip；
- [ ] Independent State Base。

### TODO-603：Failure

- [ ] First Target Failure；
- [ ] Later Target Still Runs；
- [ ] Non-zero Exit；
- [ ] Successful Profile Preserved。

### TODO-604：Safety

- [ ] No Build；
- [ ] No Source Upload；
- [ ] No Ownership；
- [ ] No Pending；
- [ ] No State；
- [ ] Alias Fail Closed；
- [ ] Password Not Printed。

### TODO-605：Idempotence

- [ ] Second Run READY；
- [ ] `--force` Reprobe；
- [ ] Old Schema Reprobe；
- [ ] Banner Drift Reprobe；
- [ ] v1.6.3 Migration。

### TODO-606：Root

- [ ] Missing Root Create；
- [ ] `--no-create-root`；
- [ ] Incorrect Root Error；
- [ ] Partial Parent Cleanup。

---

# 23. 测试矩阵

| 场景 | 动作 | 预期 |
|---|---|---|
| Valid Schema 3 Profile | READY | 不写远端 |
| Profile Missing | PROBE | 保存 Profile |
| Schema 2 Profile | REPROBE | 替换 Profile |
| Banner Drift | REPROBE | 替换 Profile |
| `--force` | REPROBE | 总是 Probe |
| Root Missing | CREATE+PROBE | 一次确认 |
| SFTP Target | SKIP | 不连接 FTP Probe |
| No Hybrid | SKIP | 不连接 |
| Password Missing | FAIL_PRECHECK | 其他 Target 继续 |
| Probe Failure | FAIL | 其他 Target 继续 |
| Workspace 5 Repos | Batch | 一条命令完成 |
| Second Run | READY | 幂等 |
| Non-TTY no `--yes` | Refuse | 零写入 |

---

# 24. 人工验收

## 24.1 单 Project 多 Target

```bash
git-deploy bootstrap
```

确认只出现一次 Prompt。

完成后：

```bash
git-deploy doctor prod
git-deploy doctor staging
```

均显示 Valid Schema 3 Profile。

## 24.2 Workspace

运行一条命令。

确认：

- 每个 Repository 有独立结果；
- 一个失败不阻止后续；
- Summary 完整；
- 没有执行 Build；
- 没有业务文件变化。

## 24.3 幂等

连续执行两次：

```bash
git-deploy bootstrap --yes
git-deploy bootstrap --yes
```

第二次：

```text
全部 READY
Remote Probe Write = 0
```

## 24.4 Force

```bash
git-deploy bootstrap --force --yes
```

确认所有 eligible Target 都重新 Probe。

## 24.5 v1.6.3 Migration

删除或保留旧 Profile，然后运行 Bootstrap。

确认不再需要手工逐个：

```bash
git-deploy doctor TARGET --probe-ftp-hybrid --yes
```

---

# 25. 版本建议

这是新的用户可见命令和 Workspace 编排能力。

推荐：

```text
v1.7.0
FTP Hybrid Bootstrap
```

不推荐：

```text
v1.6.4
```

因为它不是兼容补丁，而是新的工作流能力。

---

# 26. 最终结论

## 最新代码

v1.6.2 和 v1.6.3 正确解决了宝塔/Pure-FTPd 下：

- FEAT UTF8 但 OPTS 不支持；
- Welcome 中时间和用户数导致 Profile 每次失效；

这两个真实兼容问题。

本轮没有发现新的 P0/P1。

建议后续加固：

1. 只接受明确的 500/501/502/504 OPTS 不支持错误；
2. Banner 使用字段替换而非整行删除；
3. 规范化结果不得为空；
4. 恢复有效 CI。

## 初始化流程

建议新增：

```bash
git-deploy bootstrap --yes
```

统一解决：

- 一个 Project 多个远程；
- Workspace 多个 Repository；
- 每个 Repository 多个 FTP Target；
- v1.6.3 Profile 迁移；
- 缺失 Root；
- Profile 缺失/过期；
- 一次确认；
- 失败继续；
- 统一 Summary。

保持清晰职责：

```text
init
    → 生成本地配置

bootstrap
    → 初始化远端 FTP Hybrid 能力

deploy
    → 审阅并发布业务内容
```

这是当前最符合“个人使用、简单、稳定、低认知负担”目标的实现方式。
