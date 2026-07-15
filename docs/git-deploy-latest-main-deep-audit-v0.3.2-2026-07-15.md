# git-deploy v0.3.2 最新主线深度审计报告

## 0. 审计元数据

| 项目 | 内容 |
|---|---|
| 仓库 | `howjc/git-deploy` |
| 分支 | `main` |
| 最新提交 | `d4cd698924608308a27172f362840f9dd813f63e` |
| 上轮审计提交 | `b2d34f4dcafd44ff3548eb3a675e5c7e5fbb5fe2` |
| 增量提交 | 3 |
| 当前版本 | `v0.3.2` |
| 审计日期 | 2026-07-15 |
| 总体结论 | **有条件通过：source-only SFTP 主链路接近稳定；artifact 首次基线链路仍阻断** |

---

## 1. 审计方式与限制

本轮通过 GitHub 连接器读取并核对：

- 最新 `main` 提交历史；
- `b2d34f4d...` 到 `d4cd6989...` 的增量差异；
- `v0.3.2` release commit；
- application exact-plan、rollback exact-deployment 相关修改；
- `StateDeploymentExecutor`、`StateRollbackService` 的事务边界；
- CLI 的 pre-connect freshness gate；
- 新增的并发竞态测试；
- README、版本号与发布说明；
- 最新提交的 GitHub status / workflow run。

尝试在本地直接执行：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

但当前执行环境无法解析 `github.com`，因此未能独立运行：

```bash
uv lock --check
uv run pytest -q
uvx ruff check src tests
uvx ty check src
uv build --clear
```

仓库文档声明 v0.3.2 基线为 365+ 项自动测试；该结果属于仓库自报，本轮没有独立复跑证明。GitHub 连接器也没有返回 latest commit 的 combined status 或 workflow run。

---

# 2. 执行摘要

## 2.1 上轮阻断问题的修复状态

| 上轮问题 | 本轮状态 | 审计判断 |
|---|---|---|
| Application 签名 Plan A，领域执行重规划 Plan B | 已删除 post-confirm `plan_selectors`，执行冻结的 `domain_files` | **已修复** |
| 非 no-op stale plan 在连接远端后才发现 | 新增锁内、pre-connect freshness gate | **已修复** |
| Rollback preview A，执行时重新选择 latest B | 领域 rollback 接受 exact deployment/current 边界 | **已修复** |
| README 仍指向 v0.3.0 | 已更新到 v0.3.2 | **已修复** |
| v0.3.1 同版本不同代码风险 | README 明确禁止覆盖重发同版本 | **治理方向正确** |

v0.3.2 的核心改进是真正建立了：

```text
用户审阅的 Application Plan
        =
Token 绑定的 Plan
        =
领域层实际执行的 Plan
```

这比 v0.3.1 的“双计划适配器”明显可靠。

---

# 3. 本轮最高优先级发现

## P0-01：首次 artifact baseline 会使当前 reviewed plan 自己变 stale

### 涉及路径

在 artifact 部署分支中，流程大致为：

```text
Reviewed Plan generation=N
        ↓
pre-connect freshness 检查通过
        ↓
执行 target build
        ↓
artifact_plan == baseline_required
        ↓
commit_artifact_baseline()
        ↓
current generation 推进到 N+1
        ↓
继续使用原 Reviewed Plan（expected generation=N）
        ↓
StateDeploymentExecutor.deploy()
        ↓
锁内 require_plan_matches_current()
        ↓
stale_plan
```

`commit_artifact_baseline()` 本身是一次合法的 state-only CAS，会推进 generation。  
但最终 source/artifact 部署仍绑定最初审阅计划的：

- `before_state_id`
- `generation`
- `before_tree_id`
- `before_applied_transition_ids`

因此工具自身产生的 baseline transition 会让后续 exact-plan freshness 校验失败。

### 影响

首次需要 artifact baseline 的部署可能表现为：

1. 远端 baseline 被只读验证；
2. 本地 current 已推进一代；
3. 正式 artifact 部署没有发生；
4. 命令以 `stale_plan` 失败；
5. 用户必须重新 plan/deploy 才可能继续。

这不是数据破坏，但会形成“命令失败却已经改变 state”的不直观结果，也破坏“一次确认对应一次完整操作”的契约。

### 建议方案

不要在一个 reviewed deploy 内隐式插入独立 generation transition。

更简单可靠的两种选择：

#### 方案 A：显式两阶段，推荐

首次 artifact 项目必须先执行一个清晰命令：

```bash
git-deploy state bootstrap-artifacts PROJECT --yes
```

完成 baseline 后要求重新 plan，再部署。

优点：

- 流程简单；
- state transition 清晰；
- 每次 token 只对应一个 generation boundary；
- 更符合个人工具的可理解性。

#### 方案 B：一个锁内复合事务

将 artifact baseline 和正式部署作为一个 composite transaction：

- baseline 不单独 CAS current；
- baseline 只生成经过验证的 artifact before entries；
- 最终一次性构造 after state；
- 只推进一代 generation；
- rollback/transaction evidence 统一。

该方案体验更好，但实现复杂度更高。

### 必须新增的测试

```text
current source state 存在
artifact current baseline 缺失
artifact_plan = baseline_required
执行一次 application deploy
```

断言：

- 不出现工具自身造成的 `stale_plan`；
- 成功时只发生一次预期 generation 推进；
- 失败时 current 是否变化必须与文档一致；
- transaction/manifest 能表达完整过程；
- 不要求用户猜测为何重跑。

---

# 4. 高优先级问题

## P1-01：Durable Git object materialization 发生在 freshness gate 之前

非 no-op 路径当前顺序为：

```text
PersistentGitStore.ensure_layout()
StateComposer.compose() / 写入 durable Git objects
        ↓
TargetLock 下 freshness check
        ↓
open remote
```

这意味着 stale plan 虽然能做到：

- 零远端连接；
- 零远端写；
- 零 current CAS；
- 零 transaction/manifest；

但仍可能在 target state root 下留下：

- Git store layout；
- repository markers；
- durable Git objects。

### 风险

- “stale plan 零副作用”并不完全成立；
- 多进程可在没有 target lock 时同时写 shared Git store；
- 失败后可能出现无引用 object 增长。

### 建议

顺序调整为：

```text
TargetLock 下读取 current 并验证 frozen boundary
        ↓
释放锁或继续受控执行
        ↓
materialize reviewed objects
```

更严格的方案是把 object publication 纳入同一个 target lock。

如果考虑耗时，不应靠锁外写共享状态优化；可以先用 ephemeral object directory 计算，freshness 通过后再 publish。

---

## P1-02：Pre-connect lock 与 executor lock 之间仍存在连接窗口

当前有两次 freshness check：

1. pre-connect helper 获取锁，验证后释放；
2. 打开 SFTP/FTP/FTPS；
3. `StateDeploymentExecutor.deploy()` 再次获取锁并验证。

正确性上是安全的，因为第二次检查仍会阻止 mutation。  
但若其他进程在步骤 1 和 2 之间推进 state，当前进程会：

- 打开网络连接；
- 可能触发 SSH Agent / 1Password 生物认证；
- 然后在第二个锁内被判 stale。

### 建议

对于个人工具，可以接受短暂持锁：

```text
获取 target lock
验证 current
打开 remote
执行 transaction
释放 lock
```

这样语义最简单，也能真正保证 stale 时零连接。

若担心连接耗时，至少在文档中将保证准确表述为：

> stale plan 保证零远端 mutation；多数已知 stale 情况可在连接前拒绝。

不要宣称所有竞态 stale 都一定零 connect。

---

## P1-03：Remote drift 检查与 backup 捕获仍是两次独立读取

Deploy 流程：

```text
evaluate_drift() 读取 remote A
prepare() 再次读取 remote B 并保存 backup
mutate
```

Rollback 流程：

```text
_require_remote_after_state() 读取 A
再次读取 B 并保存 recovery backup
rollback
```

如果另一个设备、面板或人工操作在 A 与 B 之间修改文件：

- 默认 drift gate 已经通过；
- B 中第三种内容会被备份；
- 随后仍会被覆盖；
- 恢复能力存在，但“默认检测第三种内容并拒绝”不再严格成立。

### 建议

用一个 immutable `RemoteObservation` 贯穿：

```text
read + hash + mode + bytes/reference
        ↓
drift decision
        ↓
backup from same observation
        ↓
mutation 前最后 compare
```

对大文件可采用临时 spool + hash，而不是在内存中复制。

---

## P1-04：Rollback recovery backup 仍混入 deployment 目录

当前 rollback 现场备份使用类似：

```text
deployments/rb-<deployment-id>/backups/
```

但没有对应普通 deployment manifest。

Doctor/history 的历史扫描模型默认：

```text
deployments/<id>/manifest.json
```

因此成功 rollback 后仍可能产生：

- doctor 误报 corrupt record；
- history 报告缺失 manifest；
- 未来无法清楚区分 deploy evidence 与 rollback recovery object。

### 推荐布局

```text
transactions/<transaction-id>/backups/
rollback-events/<rollback-id>/manifest.json
```

Rollback event 至少记录：

- `rollback_id`
- `rollback_of`
- exact deployment ID
- before/current/after state ID
- generation N → N+1
- force 标记
- drift evidence
- restored paths
- transaction ID

---

## P1-05：Stateful rollback 仍缺少 post_commands 和 health_urls

Source/artifact deploy 会：

```text
写文件
→ read-back
→ post_commands
→ health_urls
→ state commit
```

Stateful rollback 目前主要是：

```text
恢复文件
→ read-back
→ state commit
```

对于用户实际维护的 PHP 项目，这可能导致：

- ThinkPHP/Laravel/runtime 缓存未清理；
- OPcache 或 PHP-FPM 未 reload；
- worker 继续运行旧代码；
- 文件已恢复，但应用健康状态未确认。

### 建议

抽取同一个 lifecycle runner：

```text
restore
→ verify bytes/mode
→ rollback post_commands
→ health
→ remote_verified
→ state CAS
```

hook/health 失败时：

- 恢复 rollback 前真实 bytes；
- generation 不推进；
- journal 保留恢复证据。

---

## P1-06：SFTP `posix_rename` fallback 仍过于破坏性

当前兼容逻辑仍类似：

```python
try:
    posix_rename(temp, target)
except OSError:
    remove(target)
    rename(temp, target)
```

任何 `OSError` 都可能进入删除线上 target 的 fallback，包括：

- permission denied；
- 网络瞬断；
- session failure；
- 服务端内部错误；
- 并非“不支持 posix rename”。

如果第二次 rename 再失败，线上文件会缺失。

### 建议

只在明确识别 extension unsupported 时进入 fallback。

否则：

```text
保留 target
删除 temp
失败退出
```

对于不支持原子替换的服务器：

- 使用 target → backup-temp → temp → target 的可恢复协议；
- 或在 production 模式直接拒绝非原子替换。

---

## P1-07：FTPS 实现已加固，但真实协议测试仍不足

Verified SSLContext 的实现方向正确：

- `CERT_REQUIRED`
- `check_hostname = True`
- custom CA
- optional client cert
- explicit insecure opt-out

但现有测试证据仍不足以证明完整 explicit FTPS：

```text
plaintext FTP welcome
AUTH TLS
TLS handshake
login
PBSZ
PROT P
data-channel TLS
```

“untrusted certificate” fixture 若直接在 TCP accept 后进行 TLS handshake，并不等价于 explicit FTPS。  
hostname mismatch 测试如果只检查 context 属性，也没有实际证明握手拒绝。

### 需要补齐

- trusted CA + matching SAN 成功；
- untrusted CA 失败；
- hostname mismatch 失败；
- expired cert 失败；
- `tls_verify=false` 成功并显示风险；
- data channel 确实使用 TLS；
- client certificate / mTLS；
- 完整文件 roundtrip。

---

## P1-08：FTPS 文件路径配置没有统一 config-relative 解析

新增字段：

- `tls_ca_file`
- `tls_cert_file`
- `tls_key_file`

应遵守 README 的统一规则：

> 配置中的相对路径以 `deploy.toml` 所在目录为基准。

目前这些字段仍有在连接时按进程 cwd 解释的风险。

同时，以下错误应在 config/doctor 阶段转成结构化配置错误：

- CA 文件不存在；
- PEM 损坏；
- cert/key 不匹配；
- key 缺失；
- 无读取权限。

---

## P1-09：`StalePlanError` 错误类型仍需统一

历史上 `StalePlanError` 继承裸 `ValueError`，CLI 顶层不稳定捕获。  
当前部分路径通过：

```text
PolicyError("stale_plan: ...")
```

规避 traceback，但这依赖字符串，不是稳定错误契约。

### 建议

建立统一类型：

```python
class StalePlanError(GitDeployError):
    code = "operation.stale-plan"
    exit_code = 2
```

Application 和 CLI 都映射同一个结构化错误，不靠 message 搜索。

---

## P1-10：Application transaction event 仍可能与真实 journal 脱节

Application service 提供 transaction emitter，但 CLI/domain 适配层需要确保真实 journal 每次 stage 变化都发出：

- prepared
- remote_mutating
- remote_verified
- state_committed
- recovered
- manual_recovery_required

否则 application contract 虽然存在，实际 CLI 事件流仍无法用于：

- 准确日志；
- cancellation；
- 状态展示；
- 自动恢复提示。

本轮 exact-plan 重构是消除旧递归 CLI adapter 的好机会，应同时完成真实事件接线。

---

## P1-11：History/verify/doctor 的旧问题没有明确完成证据

本轮变更重点集中于 exact plan / rollback binding。以下问题仍需专项验证和修复：

- `history all`
- `verify all --latest`
- `all + --deployment` 应在连接前拒绝
- doctor 对 `auto_rolled_back` 的合法状态识别
- doctor 对 `rb-*` 目录的误报
- remote doctor 异常被错误归为 LOCAL
- doctor public application contract 未完整登记
- doctor remote check 递归扫描整个 remote root

这些不属于企业级能力，而是个人长期使用时非常实际的诊断可靠性问题。

---

# 5. 发布与版本审计

## 5.1 已改善

README 已明确：

- 推荐 v0.3.2；
- 不再推荐已知有阻断问题的 v0.3.0；
- 禁止用新内容覆盖同一版本号；
- release 文件名、tag 示例和 notes 已同步到 v0.3.2。

## 5.2 仍需核验 v0.3.2 tag 的真实边界

提交历史中存在：

```text
46e0ea4f  release v0.3.2
5f04c3d7  fix(v0.3.2): pre-connect freshness / 禁止 post-confirm plan_selectors
d4cd6989  merge PR #4
```

因此必须核验：

- `v0.3.2` tag 指向哪一个 commit；
- GitHub Release wheel 是否包含 `5f04c3d7`；
- `SHA256SUMS` 是否对应最终源码；
- 当前 README 下载的 wheel 是否包含 non-noop pre-connect 修复。

若 tag/release 停在 `46e0ea4f`，则最终修复没有进入已发布工件。  
这种情况下应发布 `v0.3.3`，不要覆盖 v0.3.2。

---

# 6. 自动化门禁情况

GitHub 连接器对最新 `d4cd6989` 返回：

- combined statuses：空；
- workflow runs：空。

这不等于测试失败，但远程提交缺少可见、可追溯的自动门禁证明。

建议最低 GitHub Actions：

```text
Python 3.11
Python 3.12
uv lock --check
pytest unit
pytest integration
Ruff
ty
uv build
isolated wheel install
real OpenSSH/SFTP
real explicit FTPS
```

对于私有个人仓库，不需要复杂流水线；一个简洁的 `ci.yml` 足够。

---

# 7. 已确认值得保留的设计

## 7.1 Exact reviewed plan

v0.3.2 不再在用户确认后重新调用 `plan_selectors` 生成另一个文件计划，方向正确。

## 7.2 Pre-connect freshness gate

非 no-op 路径增加了 target lock 下的 before-boundary 校验，并在远端 transport factory 之前执行，显著缩小竞态窗口。

## 7.3 Domain executor 二次校验

正式 mutation 前，领域 executor 仍在自己的 target lock 内再次校验 frozen plan。双重检查在 correctness 上是安全的。

## 7.4 Exact rollback binding

Rollback 不再允许“preview A，执行 latest B”的替换，符合 token 和人工确认语义。

## 7.5 保持产品收敛

本轮没有恢复：

- TUI
- 历史版本自动回滚
- GC
- RBAC
- 审批流
- Kubernetes

符合个人部署工具的实际定位。

---

# 8. 分场景结论

| 使用路径 | 判断 |
|---|---|
| Source-only + SFTP + 单控制器 | **接近可用，条件通过** |
| Source-only + FTP | **不推荐，协议能力和原子性弱** |
| Source-only + FTPS | **实现加固，但需真实 FTPS 门禁后通过** |
| Artifact 已建立 baseline 的日常部署 | **需实测，架构上较可行** |
| Artifact 首次 baseline_required | **阻断，存在自触发 stale-plan 风险** |
| Latest rollback（无 hook/health 需求） | **核心 exact binding 已改善** |
| PHP 项目依赖清缓存/reload 的 rollback | **暂不通过，缺 lifecycle** |
| 多电脑同时部署同一远端 | **不保证互斥；TargetLock 仅本地 state filesystem** |

---

# 9. 推荐下一版本：v0.3.3 稳定收口

只处理可靠性，不新增功能。

## 必须完成

1. 修复 artifact baseline 自失效；
2. 核验或重新发布包含最终修复的 immutable release；
3. rollback lifecycle hooks/health；
4. rollback backup/event 布局；
5. SFTP fallback 安全收窄；
6. remote observation 单次贯穿；
7. real FTPS 测试；
8. stale error 类型统一；
9. 简洁 GitHub Actions 门禁。

## 可以随后完成

- history/verify all；
- doctor schema/分类；
- state 容量报告；
- health User-Agent；
- streaming backup。

---

# 10. 最终结论

与上轮相比，`d4cd6989` 的核心正确性有明显提升：

- Application Plan B 替换问题已经按正确架构修复；
- latest rollback exact deployment 已绑定；
- non-noop stale plan 可在远端连接前发现；
- README 与主版本已更新到 v0.3.2。

当前最大新问题集中在 artifact 首次 baseline 流程，而不是 source-only 日常部署主链路。

因此本轮结论是：

> **source-only SFTP 个人部署主链路可以进入真实 dev 环境验收；整个项目作为包含 artifact/FTPS/rollback lifecycle 的完整稳定版本，仍需完成 v0.3.3 稳定收口后再判定全面通过。**

不要继续扩功能。下一步只修正确性、测试与发布可追溯性。
