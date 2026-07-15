# git-deploy 最新主线深度代码审计报告

## 0. 审计元数据

| 项目 | 内容 |
|---|---|
| 仓库 | `howjc/git-deploy` |
| 分支 | `main` |
| 最新提交 | `b2d34f4dcafd44ff3548eb3a675e5c7e5fbb5fe2` |
| 当前包版本 | `0.3.1` |
| 上轮审计提交 | `4a801cd9b3cb6a0706beb932ad7366c434f8fd77` |
| 差异提交数 | 2 |
| 审计日期 | 2026-07-15 |
| 结论 | **有条件不通过（BLOCKED）** |

本轮重点审计上轮 P0 修复、application → domain 执行边界、latest rollback、FTPS、事务恢复、发布一致性和未解决 P1 问题。

---

## 1. 审计方式与限制

本轮通过已连接的 GitHub API 获取并核对：

- 最新 `main` 提交历史；
- `4a801cd9...` → `b2d34f4d...` 的完整文件差异；
- `state_guards.py`、`state_executor.py`、`state_rollback.py`；
- `application/deploy_service.py`、`application/rollback_service.py`；
- `cli.py`、`transport.py`、`config.py`；
- doctor、history、application contract；
- 新增及修改的 stale-plan、rollback drift、FTPS 测试；
- `README.md`、`pyproject.toml`、v0.3.1 release notes；
- 最新提交的 GitHub status / workflow run。

尝试使用本地 `git clone` 拉取完整私有仓库，但当前执行环境无法解析 `github.com`，且未安装 `gh` CLI，因此本轮无法独立执行：

```bash
uv lock --check
uv run pytest -q
uvx ruff check src tests
uvx ty check src
uv build --clear
```

因此本报告中的测试结论分为：

1. **代码逻辑验证**：依据最新完整源码和测试实现；
2. **仓库自报结果**：release notes 声明 `359+` 测试通过；
3. **未独立复跑**：本会话没有生成独立测试证明。

最新提交没有 GitHub connector 可见的 combined status，也没有关联的 workflow run。

---

# 2. 执行摘要

## 2.1 上轮三个 P0 的修复状态

| 上轮问题 | 最新状态 | 评价 |
|---|---|---|
| 领域层 stateful deploy 锁内 stale-plan | 已增加 `require_plan_matches_current()` | **领域层修复有效** |
| latest rollback 默认覆盖 drift、force 丢失 | 已增加 after-state 检查并贯通 `force` | **领域层修复有效，但 exact-plan 边界仍有漏洞** |
| FTPS 默认不验证证书/主机名 | 已使用 verified `SSLContext` | **实现方向正确，真实测试证据不足** |
| static no-op 绕过锁内检查 | 最新 `b2d34f4d` 补充 CLI 路径修复 | **当前 main 已修，已发布 v0.3.1 未必包含** |

## 2.2 当前主要判断

当前代码的领域层事务模型比 v0.3.0 更安全，但 application facade 与旧 CLI/domain adapter 仍是“两套计划”：

```text
Application Plan A
    ↓ 签名 / 展示 / 确认
execute_domain()
    ↓
重新调用 _run_plan_or_deploy()
    ↓
Domain Plan B
    ↓
TargetLock 内只验证 Plan B
```

HMAC token 验证的是计划 A，但实际执行的是重新生成的计划 B。只要 mutable state 在 application 的锁外预检之后、domain 重规划之前发生变化，领域层 stale guard 会认为计划 B 是新鲜的，从而执行一个没有被用户审阅和 token 绑定的计划。

latest rollback 存在更直接的同类问题：preview 绑定 deployment A，执行时重新选择“当前 latest deployment B”，最终结果仍可能报告 A。

---

# 3. P0 发布阻断问题

## P0-01：Application deploy 签名计划与实际领域计划不一致

### 证据链

`_run_application_deploy()`：

1. 读取 generation；
2. 使用 `RevisionPlanService.plan()` 生成 application plan；
3. 显示并确认该计划；
4. `DeployService` 验证该计划 token；
5. executor adapter 先做一次无锁 generation 复核；
6. 随后调用 `_run_plan_or_deploy()`。

`_run_plan_or_deploy()` 又会：

1. 重新读取 current；
2. 重新执行 `StatePlanner.plan_selectors()`；
3. 给新的 `SourceDiffPlan` 塐入新的 state ID / generation；
4. 将新计划交给 `StateDeploymentExecutor.deploy()`；
5. 领域层在 `TargetLock` 内验证的也是这个新计划。

### 竞态窗口

```text
A: application plan 基于 generation N
A: 用户确认 plan A
A: execute_domain 无锁检查 current == N
                ─────── race window ───────
B: 另一个进程推进 current 到 N+1
A: _run_plan_or_deploy 读取 N+1
A: 重新生成 domain plan B
A: 锁内验证 plan B 与 N+1 匹配
A: 执行 plan B
```

此时：

- application token 仍然只绑定 plan A；
- 实际文件列表、before tree、applied transitions 或 target tree 可能属于 plan B；
- 用户没有审阅 plan B；
- `DeployService` 无法发现，因为它看不到 domain plan B。

### 严重性

这是安全与正确性边界漏洞，而不是单纯日志不一致。

可能后果：

- 执行与用户确认不同的文件集合；
- 合并一个刚刚由其他进程引入的 state；
- application contract 声称的 “exact reviewed plan” 不成立；
- token 防重放和 plan digest 只保护 UI/application 层，无法保护真正的 mutation。

### 推荐修复

不要再从 application executor 递归调用 CLI。

推荐结构：

```text
RevisionPlanService
    ↓
FrozenDomainDeployPlan
    - before_state_id
    - generation
    - before_tree_id
    - before_applied_transition_ids
    - after_tree_id
    - exact PlannedFile[]
    - artifact/build fingerprint
    - plan_digest
    ↓
DeployService
    ↓
StateDeploymentExecutor.deploy_frozen(plan)
    ↓
TargetLock
    ↓
对 reviewed plan 做锁内 freshness check
    ↓
执行 exact files
```

至少需要：

1. `RevisionPlanResult` 增加 `before_state_id` 和完整 before transition 集合；
2. plan digest 绑定这些边界；
3. application service 直接把 frozen domain plan 传给 executor；
4. executor 在锁内读取 current 并与 **同一个 reviewed plan** 比较；
5. 禁止 execution adapter 再解析 revision selector 或重新规划。

### 必须新增的回归测试

测试必须把并发推进放在：

```text
execute_domain generation precheck 之后
_run_plan_or_deploy 领域重规划之前
```

而不是当前测试中的“application plan 之后、execute precheck 之前”。

期望：

- 返回稳定 `stale_plan`；
- 零远端连接/写入；
- 零 transaction / manifest；
- current 保持并发 actor 的 N+1；
- 不能执行重规划后的 Plan B。

---

## P0-02：Latest rollback preview 与实际回滚 deployment 可被替换

### 证据链

`LatestRollbackService.preview()` 会：

- 读取 current generation；
- 选择 latest successful manifest；
- 把 `deployment_id`、文件 hash 和 generation 写入 digest/token。

但 `_run_application_latest_rollback()` 的 executor adapter：

```python
code = _run_rollback(config, _args)
```

没有把 preview 中的 `rollback_plan.deployment_id` 传入领域层。

`_run_rollback()` 又会在执行时重新选择 latest manifest；进入 `rollback_latest()` 后，锁内 `assert_rollback_eligible()` 再次无参数选择当时的 latest。

### 可发生的结果

```text
Preview: deployment A 是 latest，用户确认回滚 A
并发 deploy: deployment B 成为 latest
Execute: _run_rollback 选择 B
Lock: rollback_latest 再次选择 B
Result: application adapter 仍填写 rollback_plan.deployment_id == A
```

最终可能出现：

- 实际回滚 B；
- application result 声称回滚 A；
- token 和 UI 预览失去意义；
- history / operator 认知与真实远端 mutation 不一致。

### 推荐修复

`StateRollbackService.rollback_latest()` 应改成类似：

```python
rollback_latest(
    *,
    expected_deployment_id: str,
    expected_current_state_id: str,
    expected_generation: int,
    force: bool = False,
)
```

在 `TargetLock` 内：

1. 读取 current；
2. 加载 `expected_deployment_id`；
3. 验证它仍是 latest successful；
4. 验证 manifest after_state 正是 current；
5. 验证 generation / state ID；
6. 对 exact manifest 做 remote after-state 检查；
7. 执行 exact manifest。

不要在锁内或 adapter 中重新选择不受 token 约束的 latest。

### 必须新增的回归测试

- preview A 后创建 deployment B；
- 执行 A 的 token；
- 必须 `stale_plan`；
- B 不得被回滚；
- 零远端写；
- application result 不得伪报 A。

---

## P0-03：版本、发布提交和 README 安装指引不一致

### 当前状态

提交顺序：

```text
c6ca5468  release v0.3.1
b2d34f4d  fix static no-op shipped CLI path
```

最新 `main` 的 `pyproject.toml` 仍是：

```toml
version = "0.3.1"
```

这意味着：

- release commit 之后又加入了安全/正确性修复；
- 当前 main 与“已发布 v0.3.1”很可能代码不同；
- 从当前 main 再构建 wheel，仍会得到 `git_deploy-0.3.1-...whl`；
- 同一版本号可能对应不同源码和 hash。

更严重的是 README 仍然推荐安装：

```text
v0.3.0/git_deploy-0.3.0-py3-none-any.whl
```

而 v0.3.0 正是上一轮存在 stale-plan、rollback drift 和 FTPS 未验证问题的版本。

### 风险

- 新用户按首页指引安装已知存在阻断问题的旧版本；
- 0.3.1 工件不可复现；
- release notes 宣称 static no-op 已修，但 release commit 后又专门修复 shipped CLI path；
- 版本号无法准确表达安全修复边界。

### 修复要求

立即：

1. README 改为当前安全版本；
2. 不再向同一 `0.3.1` 版本名发布不同内容；
3. 当前 main 升级为 `0.3.2.dev0`，或完成本报告 P0 后发布 `v0.3.2`；
4. release notes 明确 `v0.3.1` 是否包含 `b2d34f4d`；
5. 对 wheel/sdist 生成 SHA256SUMS；
6. tag 必须指向构建源码；
7. isolated install smoke 校验 `__version__`、commit metadata 和文件 hash。

---

# 4. P1 高优先级问题

## P1-01：`StalePlanError` 是裸 `ValueError`，CLI 不会稳定捕获

`StalePlanError` 继承 `ValueError`。

CLI 顶层只捕获：

- `ApplicationError`
- `GitDeployError`
- `KeyboardInterrupt`

因此以下竞态可能产生 traceback：

- `current_generation()` 后、`RevisionPlanService.plan()` 内 state 发生变化；
- rollback request 后、preview 内 generation 发生变化；
- plan token mismatch；
- public application service 调用出现 stale token。

最新 static no-op 修复为了避免这一点，反而在 adapter 中把 stale 改成字符串形式的 `PolicyError("stale_plan: ...")`，说明错误类型边界本身尚未统一。

### 修复建议

- 让 `StalePlanError` 成为结构化 application/domain error；
- 或由 CLI 显式捕获并映射稳定 code、category、exit code；
- 不要依赖 message 中包含 `stale_plan` 来表达错误类型。

---

## P1-02：Remote drift 检查与 backup 捕获之间仍有二次读取竞态

### Deploy

```text
evaluate_drift() 读取 remote
prepare() 再次读取 remote 并备份
mutate_remote() 写入
```

第二次读取没有与第一次 drift observation 比较。

若其他控制器或人工操作在两次读取之间修改文件：

- 默认非-force deploy 仍可能覆盖第三种内容；
- backup 会保存第三种内容，因此恢复能力存在；
- 但“默认发现 drift 即拒绝”的语义不成立。

### Rollback

```text
_require_remote_after_state() 第一次读取
capture recovery backup 第二次读取
执行 rollback
```

同样没有比较两次 observation。

### 修复建议

- drift check 返回包含 bytes/hash/mode 的 observation；
- backup 阶段使用同一 observation，或第二次读取后严格比较；
- 每个 mutation 前做一次最后的 expected observation 复核；
- 文档明确 local `TargetLock` 不是远端分布式锁，无法阻止其他机器/面板修改。

---

## P1-03：Rollback recovery backup 仍伪装成 deployment 目录

rollback 将现场备份写入：

```text
deployments/rb-<deployment-id>/backups/
```

但没有写对应 manifest。

doctor 的 manifest scanner 遍历 `deployments/*`，对每个目录都读取 `manifest.json`。因此成功 rollback 后仍可能出现：

- doctor `NOT READY`；
- history corrupt record；
- rollback backup 没有独立类型与生命周期；
- 未来 GC 无法区分 deployment evidence 与 transaction recovery object。

### 修复建议

推荐布局：

```text
transactions/<transaction-id>/backups/
rollback-events/<rollback-id>/manifest.json
```

并写独立 rollback event：

- rollback ID；
- rollback_of；
- before/current/derived state ID；
- generation N → N+1；
- force；
- remote drift evidence；
- restored paths；
- transaction ID。

---

## P1-04：Stateful rollback 仍未执行 `post_commands` 和 `health_urls`

stateful deploy 在远端写入后执行 hook 和 health，再提交 state。

stateful rollback 当前只：

1. 写 before bytes；
2. read-back；
3. remote_verified；
4. state CAS。

这会导致 PHP 项目常见问题：

- 缓存未清理；
- PHP-FPM / worker 未 reload；
- opcache 仍持有旧代码；
- 回滚后健康状态未验证；
- legacy rollback 与 stateful rollback 行为不一致。

### 修复建议

抽取 deploy/rollback 共用 lifecycle runner。

顺序：

```text
restore files
→ read-back verify
→ rollback post_commands
→ health_urls
→ remote_verified
→ state CAS
```

hook/health 失败时恢复 rollback 前真实 remote bytes，不推进 generation。

---

## P1-05：SFTP `posix_rename` fallback 仍可能先删除正常线上文件

当前逻辑：

```python
try:
    posix_rename(temp, target)
except (OSError, IOError):
    remove(target)
    rename(temp, target)
```

任意 `OSError` 都会进入 destructive fallback，包括：

- 权限错误；
- 网络瞬断；
- server failure；
- session 问题；
- 非 extension unsupported 错误。

如果删除 target 后第二次 rename 失败，线上文件会缺失。

### 修复建议

- 只识别明确的 “operation unsupported / extension unavailable”；
- 其他错误直接失败并保留 target；
- 非原子服务器采用 target→rollback-temp、temp→target 的可恢复协议；
- 或明确拒绝在不支持 atomic replace 的 SFTP server 上执行生产部署。

---

## P1-06：FTPS 测试并没有真正证明完整证书验证链

### “Untrusted certificate” 测试问题

测试 server 在 accept 后立即进行 TLS handshake。

但 explicit FTPS 的真实协议是：

```text
TCP connect
→ plaintext FTP welcome
→ AUTH TLS
→ TLS handshake
```

测试 server 没有发送 FTP welcome，也没有处理 `AUTH TLS`。客户端很可能因为：

- 等待 welcome timeout；
- plaintext/TLS 协议不匹配；
- 连接提前关闭；

而失败。

测试断言又接受通用：

```text
FTP connection failed
```

因此即使没有发生证书验证，该测试也可能通过。

### “Hostname mismatch” 测试问题

测试只检查：

```python
context.check_hostname is True
context.verify_mode == CERT_REQUIRED
```

没有建立实际 hostname mismatch TLS 会话。

### 缺失门禁

- trusted CA + matching hostname 成功连接；
- matching CA + hostname mismatch 失败；
- expired certificate 失败；
- custom CA relative path；
- mTLS client cert；
- actual FTP welcome / AUTH TLS / PBSZ / PROT P；
- data-channel TLS。

### 结论

FTPS implementation 的 context 方向是正确的，但 release notes 中的“连接级验证”证据不足。

---

## P1-07：FTPS TLS 路径没有按配置文件目录解析，配置错误可能冒泡 traceback

README 声明相对路径基于 `deploy.toml` 所在目录。

但 `load_config()` 只对 project repository / local state 等路径做 base resolution；remote 中新增的：

- `tls_ca_file`
- `tls_cert_file`
- `tls_key_file`

仍保留原字符串。

`build_ftps_ssl_context()` 在连接时直接使用这些路径。

另外 SSLContext 创建发生在 FTP connect `try` 之前，以下错误可能直接冒泡为未捕获异常：

- CA 文件不存在；
- PEM 损坏；
- client cert/key 不匹配；
- key 路径类型错误。

### 修复建议

在 `load_config()`：

- 将 remote TLS 文件路径解析为相对 config directory 的绝对路径；
- 校验字段类型；
- 校验 cert/key 成对关系；
- 将 SSL/IO 错误包装为 `ConfigurationError`；
- doctor 本地检查应在连接前报告。

---

## P1-08：`history all` 和 `verify all` 仍是声明支持、实际失败

CLI parser 明确写：

```text
history target: project name or all
verify target: project name or all
```

但 `_run_history()` 和 `_run_verify()` 都直接把 `args.target` 传给只接受精确项目名的 `StateInspectService.current_generation()`。

### 修复建议

- 使用 `_application_project_names()` 展开；
- history 每个项目独立分页；
- verify `all` 只允许 `--latest`；
- `all + --deployment ID` 在连接远端前拒绝；
- 聚合 exit code。

---

## P1-09：Application CLI adapter 没有发送真实 transaction stage event

`DeployService` 和 `LatestRollbackService` 向 executor 提供 transaction emitter。

但 CLI adapter 的：

```python
def execute_domain(_request, _plan, _emit):
```

完全忽略 `_emit`，然后调用旧 CLI domain function。

结果：

- application event stream没有真实 transaction ID / stage；
- result fields 通常也没有 transaction_id；
- `_validate_result()` 因 transaction_ids 为空而无法验证；
- cancellation / observability contract 与真实 CLI mutation 脱节。

### 修复建议

直接调用领域 executor，并把 journal stage 映射到 `_emit`。这也能与 P0-01/P0-02 的“去掉递归 CLI adapter”一起解决。

---

## P1-10：Latest rollback plan digest 没有绑定文件 mode

`RollbackPathPlan` 只包含：

- action；
- path；
- expected current hash；
- target hash。

没有：

- expected current executable；
- target executable；
- before/after exists 的显式字段。

但领域 rollback 会修改 executable mode，也会按 mode drift 阻断。

因此：

- UI/token 没有完整绑定实际 mutation；
- 纯 mode rollback 不能被完整展示；
- preview digest 不是 exact rollback plan。

### 修复建议

增加并写入 digest：

- `expected_current_exists`
- `expected_current_executable`
- `target_exists`
- `target_executable`
- `before_state_id`
- `after_state_id`

---

## P1-11：Doctor 仍有 schema 与状态误判问题

1. manifest 白名单缺少 legacy 合法状态 `auto_rolled_back`；
2. rollback 的 `rb-*` backup 目录会被当成缺失 manifest；
3. 任意 check 抛异常后统一转成：
   - `doctor.check-failed`
   - category=`LOCAL`
   - side_effect=`LOCAL_READ`
4. 远端 check 异常也可能被错误归类；
5. Doctor 类型已从 `application.__init__` 公开导出，但冻结 contract 文档没有登记。

---

## P1-12：最新提交没有可见的自动状态检查

GitHub connector 返回：

- combined statuses：空；
- commit workflow runs：空。

这不证明测试失败，但意味着当前无法从远程提交获得自动化门禁证明。

建议至少建立：

- Linux Python 3.11 / 3.12；
- unit / integration 分组；
- Docker SFTP；
- real FTPS fixture；
- Ruff；
- ty；
- uv lock check；
- wheel build/install smoke；
- release tag artifact hash。

---

# 5. P2 维护性与产品风险

## P2-01：多文件部署不是整版本原子切换

单文件使用 temp + rename，但多个文件之间仍逐个生效。PHP 请求可能在部署中看到混合版本。

建议保留简单主线，同时增加可选：

- `pre_commands` 进入 maintenance；
- 或 release directory + symlink swap；
- FTP 无法支持时明确降级语义。

## P2-02：Remote backup 仍把单文件完整读入内存

SFTP/FTP `read_file()` 聚合完整 bytes；prepare 和 rollback backup 也使用完整 bytes。

大文件 artifact 可能造成显著内存峰值。建议增加：

- stream-to-backup；
- stream hash；
- size limit / warning；
- 大文件测试。

## P2-03：Doctor remote check 会递归扫描整个 remote root

`doctor --check-remote` 调用 `list_files(remote_root)`，SFTP/FTP 都递归遍历。

问题：

- 大 uploads/storage 目录很慢；
- 非受管子目录权限错误会让 doctor 失败；
- FTP server 必须支持 MLSD。

建议 doctor 只做 connect + stat/shallow list；完整 managed path 校验交给 `state verify --check-remote`。

## P2-04：FTPS insecure opt-out 只依赖 Python warning

`tls_verify=false` 使用 `warnings.warn()`。

warning 可被过滤，且 local plan/doctor 不一定突出显示该风险。建议：

- configuration doctor WARN；
- plan risk summary；
- text/json 稳定字段；
- production remote 可选拒绝 insecure FTPS。

## P2-05：`ftps_tls_trust_digest` 没有进入 identity/policy 或 doctor

函数生成 digest，但本轮差异中没有 target identity、policy 或 doctor 的集成修改。

另外 digest只包含路径，不包含 CA/cert 文件内容 hash；原地替换证书文件不会改变 digest。

应明确其用途，否则属于“看似有安全 fingerprint、实际上没有门禁”的死代码。

## P2-06：Health User-Agent 仍写死为 `git-deploy/0.1.4`

当前 0.3.1 的 health request 仍发送旧 UA，影响服务器日志和排障。应从 `__version__` 生成。

## P2-07：无自动 GC 是明确产品决策，但需要容量告警

v0.3 冻结 GC 是合理收敛，但 doctor 应至少报告：

- state 总大小；
- CAS 大小；
- deployment/backup 数；
- 最大单项；
- 建议人工归档阈值。

---

# 6. 确认有效的改进

以下修复在最新源码层面是明确有效的。

## 6.1 领域层锁内 before-boundary guard

`require_plan_matches_current()` 比较：

- state ID；
- generation；
- source tree；
- applied transition IDs。

并在 `StateDeploymentExecutor.deploy()` 取得 `TargetLock` 后、remote read 前执行。

这解决了“直接调用 frozen `SourceDiffPlan` 的领域执行路径”中的 stale plan。

## 6.2 Static no-op 不再由 CLI 提前返回

最新 `b2d34f4d` 取消 application CLI 的早期 `continue`，让 static no-op 进入 domain executor，具备锁内 freshness check。

## 6.3 Rollback after-state drift 检查

领域 `rollback_latest(force=False)` 已在 journal/remote write 前检查：

- exists；
- hash；
- executable mode。

force 路径会再次读取并保存真实现场作为 recovery backup。

## 6.4 FTPS 默认 verified context

实现已改成：

- `ssl.create_default_context()`；
- `CERT_REQUIRED`；
- `check_hostname=True`；
- 支持 custom CA 和 client cert；
- `tls_verify=false` 明确 opt-out。

## 6.5 原有事务安全能力仍保持

- after state / CAS / backup / prepared journal 在远端 mutation 前；
- 每个文件 mutation 后 read-back；
- hook/health 后才 remote_verified；
- state CAS 后再 terminal journal；
- deploy 失败从 durable backup 恢复；
- `TargetLock` 使用内核 `flock`，进程退出释放。

---

# 7. 推荐修复批次

## Patch 0：立即修复发布指引与版本

- README 不再指向 v0.3.0；
- 当前 main 改为 `0.3.2.dev0`；
- 记录 v0.3.1 tag 是否包含 `b2d34f4d`；
- 禁止同版本不同 wheel；
- 增加 release CI。

## Patch 1：统一 exact application/domain plan

同时解决：

- P0-01 deploy plan substitution；
- P0-02 rollback deployment substitution；
- P1-09 transaction event 缺失；
- P1-10 rollback mode digest。

核心原则：

> application preview 所签名的对象，就是领域 executor 在锁内执行的对象。

禁止 recursive CLI replan/reselect。

## Patch 2：Remote mutation safety

- second-read observation revalidation；
- SFTP fallback 收窄；
- rollback lifecycle hooks/health；
- rollback event manifest / backup layout。

## Patch 3：FTPS 与配置完整门禁

- real explicit FTPS server fixture；
- success / untrusted / mismatch / expired / mTLS；
- TLS path config-relative resolution；
- SSL error wrapping；
- doctor insecure warning。

## Patch 4：CLI 和 doctor 收口

- history all；
- verify all；
- stale error type；
- doctor status enum；
- doctor contract；
- remote lightweight check；
- capacity report；
- health UA。

---

# 8. 必须新增的自动测试矩阵

## 8.1 Application deploy exact plan

| 场景 | 期望 |
|---|---|
| state 在 application plan 后、execute precheck 前推进 | stale |
| state 在 execute precheck 后、domain planning 前推进 | stale |
| HEAD 在 plan 后移动 | 执行冻结 SHA 或 stale，不重解释 HEAD |
| concurrent non-overlap transition | 不得重规划并合并 |
| static no-op generation race | stale，零 remote |
| token plan A + domain plan B | 必须拒绝 |

## 8.2 Latest rollback exact plan

| 场景 | 期望 |
|---|---|
| preview A 后新增 successful B | A token stale，不回滚 B |
| preview A 后 current generation 变化 | stale |
| exact A after-state drift | 默认拒绝 |
| force A drift | 允许且 recovery 保存现场 |
| result deployment ID | 必须等于实际 rollback_of |

## 8.3 FTPS

| 场景 | 期望 |
|---|---|
| trusted CA + matching SAN | 成功完成 login + PROT P + file roundtrip |
| untrusted CA | certificate verify fail |
| hostname mismatch | fail |
| expired cert | fail |
| tls_verify=false | 成功但稳定安全 warning |
| relative CA path | 以 config directory 解析 |
| bad PEM/key | ConfigurationError，无 traceback |

## 8.4 SFTP rename

| 场景 | 期望 |
|---|---|
| posix rename unsupported | 进入受控兼容路径 |
| permission denied | 保留原 target |
| network failure | 保留原 target |
| fallback rename failure | 可恢复且不丢原文件 |

---

# 9. 最终结论

最新 `main` 比上轮 v0.3.0 有实质进步，领域层三个 P0 的修复方向基本正确；尤其 lock-held state boundary、rollback drift 和 FTPS verified context 值得保留。

但当前仍不适合作为无条件稳定发布基线，原因不是底层事务完全失效，而是：

1. application 签名/确认的 deploy plan 不是领域层实际执行的 plan；
2. latest rollback preview 的 deployment 不是领域层强制执行的 exact deployment；
3. README、release commit、main 代码和包版本不一致；
4. v0.3.1 的关键安全测试和远程 CI 证明不足；
5. 上轮 P1 中 rollback lifecycle、backup schema、SFTP fallback、all 命令和 doctor 仍未修复。

建议将当前目标改为 **v0.3.2 correctness release**，只修复上述问题，不增加 TUI、历史回滚、GC 或新平台能力。

完成 Patch 0–3 和关键测试后，项目可以进入“个人长期使用可接受”的稳定状态。
