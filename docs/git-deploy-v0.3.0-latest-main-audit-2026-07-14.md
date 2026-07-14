# git-deploy v0.3.0 最新主线代码审计

- 仓库：`howjc/git-deploy`
- 分支：`main`
- 审计提交：`4a801cd9b3cb6a0706beb932ad7366c434f8fd77`
- 项目版本：`0.3.0`
- 审计日期：`2026-07-14`
- 对比基线：`929162398e6a3bb3ddd63af1ffa3c99a75e90caf`
- 结论：**有条件不通过（BLOCKED）**

## 1. 审计方法与限制

本次通过已连接的 GitHub API 读取最新 `main` 提交、提交差异、核心源码、测试、README、北极星、发布说明和审计文档，重点检查：

1. current → HEAD 计划冻结和 stale-plan 防护；
2. target lock、expected state、generation 与 CAS；
3. 部署备份、事务日志、自动恢复；
4. latest rollback、remote drift、hook 与 health；
5. SFTP、FTP、FTPS 安全边界；
6. doctor、history、verify 与 CLI 兼容；
7. 自动测试与真实协议门禁。

本地执行环境无法解析 `github.com`，因此没有在本次会话中重新 clone 并执行测试。仓库文档声明 v0.3.0 基线为 347 个自动测试；该数字属于仓库发布证据，不是本次独立复跑结果。

## 2. 总体评价

v0.3.0 的产品收敛方向正确。current → HEAD 默认规划、doctor、latest rollback 默认选择和真实 OpenSSH/SFTP 门禁，都符合“个人开发者可靠部署工具”的定位。

核心事务模型也有明显优点：

- plan 将移动的 `HEAD` 冻结成完整 SHA；
- mutation 前先持久化 after state、CAS 内容、before backup 和 prepared journal；
- 每个远端文件修改后立即回读校验；
- 部署失败时从 durable backup 恢复，并保持原 generation；
- target lock 使用内核 `flock`，进程退出后不会留下真正的死锁；
- stateful rollback 会在远端 I/O 前验证本地 lineage 和 before backup hash。

但最新主线仍存在会破坏 state 谱系、覆盖未知远端修改或削弱传输身份认证的问题，因此暂不建议把当前提交作为“无需条件的稳定基线”。

---

# 3. P0：发布阻断项

## P0-01：stateful deploy 在锁内没有复核计划的 before state

### 现象

CLI 在执行领域部署前检查一次 generation，随后重新读取 current 并规划；真正进入 `StateDeploymentExecutor.deploy()` 后才获取 `TargetLock`。executor 在锁内重新读取了 current，却没有验证该 current 的：

- state ID；
- generation；
- source tree；
- applied transition 集合；

是否仍与计划的 before boundary 一致。

### 风险

存在典型 TOCTOU：

1. 进程 A 基于 generation N 生成计划；
2. 进程 B 先取得锁并把 current 推进到 N+1；
3. A 随后取得锁；
4. A 的文件变化若与 B 不重叠，remote drift 可能全部通过；
5. A 使用旧 plan 和新 current 构造 after state；
6. 新 state 可能回退 `source_tree_id`，或丢失 B 已加入的 transition lineage。

target lock 只能串行化 mutation，不能自动证明锁内执行的 plan 仍然新鲜。

### 修复要求

给领域执行边界传入并验证：

- `expected_before_state_id`；
- `expected_generation`；
- `expected_before_tree_id`；
- 必要时 `expected_applied_transition_digest`。

在取得 `TargetLock` 后、任何远端读取/写入、journal/CAS/manifest 写入之前，读取 current 并逐项比较。不一致立即抛出稳定的 `stale_plan` 错误。

### 必须新增的测试

构造两个执行者和同步屏障：

- A 在 generation N 完成 plan；
- B 修改不相交路径并提交 generation N+1；
- A 再执行旧 plan；
- 断言 A 被拒绝；
- 远端写入为 0；
- transaction/manifest 为 0；
- current 保持 N+1；
- B 的 tree 和 transition 不丢失。

---

## P0-02：stateful latest rollback 会默认覆盖 remote drift，且 `--force` 被丢弃

### 现象

stateful rollback 只验证本地 current 是否等于 deployment 的 after state，并验证 before backup。取得锁后，它读取远端当前 bytes 作为“回滚失败时的恢复备份”，但没有先比较远端实际内容与 manifest 的：

- `after_exists`；
- `after_sha256`；
- `after_executable`。

随后直接写入 before bytes。

CLI 暴露了 `rollback --force`，但 stateful 路径调用 `rollback_latest()` 时没有传入 `force`。

### 风险

线上存在人工热修复、面板修改或其他发布器写入时，普通 rollback 会静默覆盖第三种内容。用户无法通过默认安全门禁发现 drift，`--force` 也没有真实语义。

### 修复要求

将接口改为：

```python
rollback_latest(*, force: bool = False, ...)
```

在锁内、journal 与第一笔远端 mutation 之前，对每个 snapshot 做 after-state 校验：

- 默认发现第三种内容即拒绝；
- 拒绝时远端写、journal、state CAS 均为 0；
- `force=True` 才允许继续；
- force 路径必须把真实第三种内容持久化为 rollback recovery backup；
- CLI 与 application request 的 `force` 必须传到底层。

### 必须新增的测试

- after bytes 匹配：允许回滚；
- 文件缺失、额外存在、hash drift、mode drift：默认拒绝；
- 默认拒绝时零 mutation；
- `--force` 明确允许；
- force 后若回滚中途失败，恢复到 rollback 前的第三种内容，而不是 manifest after bytes。

---

## P0-03：FTPS 默认未验证服务器证书和主机名

### 现象

`FtpTransport` 在 FTPS 模式直接构造：

```python
ftplib.FTP_TLS()
```

没有传入经过验证的 `ssl.SSLContext`，也没有 CA、hostname-check、client certificate 等配置。

CPython 3.11 的 `FTP_TLS` 在 `context=None` 时使用 `_create_stdlib_context`；该名称是 `_create_unverified_context` 的别名，默认 `CERT_NONE` 且 `check_hostname=False`。

### 风险

FTPS 虽然加密控制和数据通道，但没有认证服务器身份。中间人可以冒充服务器，获取部署凭据和代码内容，或注入远端响应。

### 修复要求

默认使用：

```python
context = ssl.create_default_context(cafile=ca_file_or_none)
context.verify_mode = ssl.CERT_REQUIRED
context.check_hostname = True
ftplib.FTP_TLS(context=context)
```

配置建议：

- `tls_ca_file`；
- 可选 `tls_cert_file` / `tls_key_file`；
- 默认必须验证；
- 只有显式 `tls_verify = false` 才允许兼容旧服务器，并输出明显安全警告；
- identity/policy fingerprint 应包含会改变信任边界的 TLS 配置摘要，但不能包含私钥或密码。

### 必须新增的测试

真实 FTPS 容器或本地 TLS fixture：

- 受信 CA + 正确 hostname 成功；
- 未受信证书失败；
- hostname mismatch 失败；
- 过期证书失败；
- 显式 insecure opt-out 才能连接，并产生风险提示。

---

# 4. P1：高优先级修复项

## P1-01：成功 stateful rollback 会制造“损坏 deployment”误报

stateful rollback 把回滚前现场备份写到：

```text
deployments/rb-<deployment-id>/backups/
```

但没有写对应 `manifest.json`。

doctor 和 history 的损坏扫描会遍历 `deployments/` 下每一个目录，并要求存在合法 `manifest.json`。因此一次成功 rollback 后：

- `doctor` 会变成 `NOT READY`；
- history 会报告 corrupt record；
- rollback 的 recovery backup 也没有完整生命周期元数据。

### 修复建议

优先选择以下之一：

1. 为 rollback 创建完整、不可变的 rollback manifest；
2. 将 transaction recovery backup 移到独立的 `transactions/<id>/backups/`；
3. 明确定义 deployment 目录 schema，并让 scanner 只处理带类型 marker 的记录。

推荐采用“新增 rollback event manifest + transaction backup 独立存放”，不要伪装成普通 deployment。

---

## P1-02：stateful rollback 没有执行 `post_commands` 和 `health_urls`

legacy rollback 恢复文件后会运行 post steps 和 health check；stateful rollback 只做文件恢复、回读和 CAS。

这会导致同一个项目在建立 current state 前后出现不同语义，例如：

- PHP/ThinkPHP/Laravel 缓存没有清理；
- PHP-FPM 或应用 reload hook 没有执行；
- 回滚后的关键健康 URL 没有验证；
- 文件已恢复但服务仍运行旧缓存。

### 修复建议

抽取 deploy/rollback 共用 lifecycle runner。stateful rollback 应在远端文件恢复并回读成功之后、state CAS 之前执行 hook 和 health。失败时：

- 恢复 rollback 前真实 bytes；
- current generation 不推进；
- journal 保持可恢复证据；
- 清楚说明 hook 的外部副作用不在文件事务可逆范围内。

---

## P1-03：SFTP 原子替换 fallback 捕获范围过宽，并先删除线上文件

SFTP 上传先调用 `posix_rename(temp, target)`。当前代码捕获任意 `OSError/IOError` 后：

1. 删除 target；
2. 再执行普通 rename。

如果 `posix_rename` 因权限、瞬时网络故障或其他非“不支持扩展”原因失败，代码仍可能删除正常线上文件；第二次 rename 再失败时，目标会暂时或持续缺失，直到 recovery 成功。

### 修复建议

- 只在可证明是 `posix-rename` extension unsupported 时进入兼容路径；
- 其他错误直接失败，保留原 target；
- 兼容服务器上采用可恢复的 target→backup、temp→target 两步方案，或明确拒绝非原子替换；
- 增加“posix rename 权限失败”“网络中断”“fallback rename 失败”故障注入测试。

---

## P1-04：`history all` 和 `verify all --latest` 的 CLI 契约已回归

parser 和 README 都宣称 history/verify 支持 `all`。实际实现把字符串 `"all"` 直接传给只接受精确项目名的 `StateInspectService.current_generation()`，没有像 deploy/rollback 那样展开项目列表。

### 修复建议

- 共用 `_application_project_names()`；
- history 对每个项目独立读取并渲染；
- verify `all` 只允许 `--latest`，显式 deployment ID 与 all 组合应在远端连接前拒绝；
- 聚合退出码，任一项目 drift 时整体非零；
- 增加 named remote + all 回归测试。

---

## P1-05：静态 no-op 快速路径没有 fresh-state guard

CLI 和 `DeployService` 都能在 static no-op 时提前返回，不进入 target lock，也不重新读取 current。

计划产生后如果另一进程推进 generation，本次命令仍可能输出：

```text
No changes
```

尽管目标已经变化。

### 修复建议

no-op 仍保持“零远端、零 transaction、零 manifest、零 current 写”，但必须：

- 取得本地 target lock；
- 重新读取 current；
- 验证 state ID/generation/tree/transition boundary；
- stale 时要求重新 plan。

该修复可与 P0-01 使用同一个 domain guard。

---

## P1-06：doctor 存在多项契约和分类问题

### 具体问题

1. 合法 legacy 状态 `auto_rolled_back` 不在 manifest status 白名单内，会被误报损坏；
2. 任意 check 抛异常时统一降级为 `LOCAL/local_read`，远端连接故障也会被错误分类；
3. `DoctorRequest/Result/Service` 已作为 public application API 导出，但冻结的 application contract 文档没有登记；
4. DoctorRequest 没有采用其他 application request 的共同 identity/generation boundary。

### 修复建议

- manifest status 使用单一枚举或由 model 提供合法值；
- doctor check 对象携带稳定 `check_id/category/side_effect` 元数据，异常转换时保留；
- 更新并测试 application contract；
- 明确 doctor 是否属于冻结 application API；若属于，应定义 target/identity 快照语义。

---

# 5. P2：维护性与体验项

## P2-01：`doctor --check-remote` 实际递归遍历整个 remote root

该命令文档描述为远端连接/目录检查，但实现调用 `list_files(remote_root)`：

- SFTP 递归遍历全部子目录；
- FTP/FTPS 强依赖 MLSD；
- 大型 uploads、storage 或未纳管目录也会被扫描；
- 某个无权限的无关子目录可令 doctor 失败。

建议增加轻量 `stat_path` / shallow-list capability；完整 managed path 校验继续交给 `state verify --check-remote`。

## P2-02：真实 SFTP 测试对 Docker 的要求没有在开发门禁中说清楚

README 前部说 Docker 只在 Docker build runner 时需要，但完整 pytest 门禁中的 OpenSSH fixture 会无条件执行 Docker build/run。

建议：

- 开发文档明确完整集成测试需要 Docker；
- 提供 `integration` marker；
- CI 的发布门禁强制执行；
- 普通快速单测可在无 Docker 环境运行并明确显示跳过原因。

## P2-03：health check User-Agent 仍写死为 `git-deploy/0.1.4`

v0.3.0 发送旧版本 User-Agent，影响服务器日志和问题定位。应从包的 `__version__` 生成。

---

# 6. 已确认的优秀设计

## 6.1 隐式 current → HEAD 规则是 fail-closed 的

- 没有可信 current 时拒绝隐式推断；
- HEAD 在 plan 阶段冻结成完整 SHA；
- detached HEAD 可用；
- working tree 未提交内容不进入部署 bytes；
- shallow history 或对象缺失时拒绝，而不是猜测。

## 6.2 事务写入顺序正确

stateful deploy 在远端第一笔写入前已经完成：

- after state durable publish；
- CAS 内容持久化；
- before bytes backup；
- prepared journal。

并显式断言 prepared 阶段不能发生 transport write。

## 6.3 逐文件回读与恢复边界清晰

每个 upload/delete 后立即读取远端并核对 hash/absence/mode，再进入 hook/health 和 state commit。失败时保持 before generation，并从 journal 中的 backup mapping 恢复。

## 6.4 target lock 实现简单可靠

`fcntl.flock` 由内核持有；PID 文件只是诊断信息。异常或进程退出关闭 fd 后锁自动释放。对于个人开发者的单控制器使用模型是合适的。

需要继续明确：它不是跨机器分布式锁。如果两台电脑共享同一服务器但不共享 state filesystem，双方不会互斥。

## 6.5 真实 SFTP 集成门禁价值很高

当前真实 OpenSSH 容器测试覆盖：

- SFTP 上传和原子扩展；
- add/modify/delete；
- owner/group/mode；
- remote drift；
- latest rollback；
- 权限拒绝。

后续应保留，并增加真实 FTPS 验证。

---

# 7. 推荐修复批次

## Patch A：state boundary correctness

包含：

- P0-01 锁内 stale-plan guard；
- P1-05 no-op fresh-state guard；
- 并发非重叠路径回归测试。

完成后才能继续依赖 generation/target lock 作为正确性边界。

## Patch B：rollback safety and auditability

包含：

- P0-02 remote after-state drift check；
- `--force` 贯通；
- P1-02 hook/health；
- P1-01 rollback manifest/backup 布局；
- rollback history event；
- 成功、drift、force、hook failure、health failure、partial recovery 测试。

## Patch C：transport hardening

包含：

- P0-03 verified FTPS context；
- P1-03 SFTP rename fallback；
- 真实 FTPS CA/hostname 测试；
- 明确 FTP 非原子替换限制。

## Patch D：CLI 与 doctor 收口

包含：

- `history all`；
- `verify all --latest`；
- `auto_rolled_back` 状态；
- doctor 异常分类；
- application contract 更新；
- remote doctor 轻量检查。

## Patch E：发布门禁

执行并保存结果：

```bash
uv lock --check
uv run pytest -q
uvx ruff check src tests
uvx ty check src
uv build --clear
```

另外强制：

- real SFTP container；
- real FTPS trusted/untrusted/hostname matrix；
- wheel isolated install smoke；
- 两进程 stale-plan race；
- rollback drift and recovery matrix。

---

# 8. 最终发布判断

当前 `4a801cd9...` 的设计方向优于 v0.2.1，且大部分事务基础扎实，但以下三项属于稳定版发布阻断：

1. 锁内不校验 plan before state，可能破坏 expected-state 谱系；
2. stateful rollback 默认覆盖 remote drift；
3. FTPS 未认证服务器证书和主机名。

建议在完成 Patch A、Patch B 的 drift 部分和 Patch C 的 FTPS 验证后，发布 `v0.3.1`；随后在同一小版本周期内完成其余 P1 项。不要在这些问题修复前继续扩展 TUI、历史回滚或 GC。
