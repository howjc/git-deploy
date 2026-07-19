# git-deploy v1.6.0 最新代码深度审计报告

> 仓库：`howjc/git-deploy`
> 分支：`main`
> 最新提交：`6138bf1a62f106fff07c8abf484db24b90482c06`
> 功能提交：`55e92eb39d368f555eddf8ee29449b6e900029a3`
> PR：`#19 Add transfer rate visualization for v1.6.0`
> 版本：`v1.6.0`
> 审计日期：`2026-07-19`
> 审计结论：**有条件通过**
> 建议动作：发布一个最小 `v1.6.1`，修正 Native OpenSSH 测量口径、展示层 Fail-open 与统一计时起点；修复 GitHub Actions 账单/额度后，对确切 Merge Commit 或 Tag 重新执行完整 CI。

---

# 1. 执行摘要

v1.6.0 的实现方向正确。

本次版本把原来只显示百分比的 `ProgressReporter` 扩展为部署级传输统计器，完成了：

- 单文件 TTY 动态进度；
- 1.5 秒滑动速率；
- 250ms 最短刷新间隔；
- 非 TTY 行式输出；
- 单文件平均上传速率；
- 部署级 Payload；
- 部署级 Wire Bytes；
- Active Upload Time；
- Average Upload；
- Retry Counter；
- Workspace 每仓独立汇总；
- FTP、Paramiko、Native OpenSSH、SFTP Hybrid 与 FTP Hybrid 统一接入；
- FTP Hybrid Stage/Restage 统计；
- FTP RETR、Rename、Delete/RMD 不计入上传字节；
- 失败部署不显示成功 Summary；
- Delete-only/No-op 不显示 Summary；
- Zero-byte 文件安全处理。

代码没有修改：

- Planner；
- Remote Ownership；
- FTP Pending Schema；
- SFTP Recovery Schema；
- Local State Schema；
- 远端写入顺序；
- Ownership/State 提交顺序；
- Hybrid Adoption/Prune 边界。

因此本轮没有发现新的远端误删、Ownership 越权或 State 提前提交问题。

核心统计逻辑也基本成立：

```text
payload
    = 成功逻辑文件按最终路径去重

wire bytes
    = Upload Callback 报告的所有正向字节增量

active time
    = Reporter Attempt 的累计活动时间

average upload
    = wire bytes / active time
```

Retry 使用显式 Credit，能够避免：

```text
record_retry()
+
下一次 callback()
```

被统计成两次 Retry。

FTP Hybrid 中：

- Stage Upload 统计；
- Stage RETR 不统计；
- Final Rename 不统计；
- Final RETR 不统计；
- Restage Upload 统计。

这符合综合实施方案的主要设计。

但是，本轮发现两个需要优先整改的实现问题，以及一个发布验证缺口。

## P1-01：Native OpenSSH 并没有真实实时速率

Native OpenSSH 当前只执行：

```text
callback(0)
运行完整 sftp put + chmod
执行临时文件发布
callback(total)
```

因此它不能提供：

- 文件传输过程中的实时百分比；
- 1.5 秒滑动速率；
- 失败上传已经发送的部分字节；
- 精确的纯上传 Active Time。

Native 用户看到的实际效果接近：

```text
UPLOAD file 0% ... 0 B/s
长时间无更新
UPLOAD file 100% ... avg ...
```

最终平均还包含：

- SFTP Batch；
- chmod；
- 临时文件发布 Rename；

而 Release Notes 声称 Active Time 排除 Rename。

如果 Native 上传在发送部分数据后失败，因为没有中间 Callback：

```text
失败 Attempt Wire Bytes = 0
```

最终 Summary 会低估实际重传流量。

这对主要依赖：

```text
WSL2
OpenSSH
1Password SSH Agent
```

的使用路径尤其重要。

## P1-02：展示层异常可能改变部署结果

`ProgressReporter` 的所有 `print()` 都可能抛出：

- `BrokenPipeError`；
- `OSError`；
- `UnicodeEncodeError`。

这些异常没有被 Reporter 内部隔离。

在普通部署中：

```text
远端操作成功
Remote Command 成功
Local State 保存成功
    ↓
render_summary()
    ↓
输出异常
    ↓
命令向用户报告失败
```

在 Hybrid 中，Summary 同样位于：

```text
Ownership 已提交
State 已保存
Recovery/Pending 已推进
```

之后。

这违反了 v1.6.0 的核心定位：

> 纯可观测性功能不能改变部署成功语义。

进度行输出失败还可能在 Upload Callback 中传播，导致本来正常的上传进入 Retry 或终止。

## Release Gate：GitHub Actions 没有真正执行

PR #19 的 GitHub Actions Run 结论为 Failure，但两个 Matrix Job：

```text
steps = None
runner_id = 0
```

没有执行：

- Checkout；
- Tests；
- Ruff；
- ty；
- Build；
- Isolated Install。

PR 描述说明原因是账号付款失败或 Actions Spending Limit，而不是仓库测试失败。

PR 中记录：

```text
Python 3.11：302 passed locally
Python 3.12：302 passed locally
```

但本轮没有可用的独立 CI 证据。

当前审计环境再次尝试 Clone，也仍然因为：

```text
Could not resolve host: github.com
```

无法独立复跑。

因此 v1.6.0 不能被标记为“CI 已验证通过”。

综合结论：

```text
部署安全与状态机：
    通过

FTP / Paramiko 传输统计：
    主体通过

Native OpenSSH 传输统计：
    测量口径不满足完整功能承诺

展示层隔离：
    需要整改

发布验证：
    CI 未执行

整体：
    有条件通过
```

---

# 2. 发布状态

## 2.1 Main

```text
6138bf1a62f106fff07c8abf484db24b90482c06
Merge pull request #19 from howjc/agent/v1.6.0-transfer-rate-visualization
```

功能提交：

```text
55e92eb39d368f555eddf8ee29449b6e900029a3
add transfer rate visualization for v1.6.0
```

## 2.2 PR

```text
PR #19
title: Add transfer rate visualization for v1.6.0
merged: true
```

变更范围：

```text
README.md
docs/*
pyproject.toml
__init__.py
deployer.py
prepared.py
progress.py
workspace.py
tests/*
uv.lock
```

没有修改：

```text
planner.py
hybrid.py
ftp_hybrid.py
manifest.py
config.py
transport protocol implementations
```

## 2.3 Package / Tag

Main：

```toml
version = "1.6.0"
```

Tag `v1.6.0`：

```toml
version = "1.6.0"
```

`pyproject.toml` Blob：

```text
8ceb3f9390da57ac985557c3ce042f1db0ffba31
```

一致。

Main/Tag：

```python
__version__ = "1.6.0"
```

Blob：

```text
aeacaa78802a1ba1ace71d20b36961ec441bb2c5
```

一致。

Main/Tag `progress.py` Blob：

```text
af71025ca83c1e5d96febbc6020adaf6640e8607
```

一致。

Main/Tag `deployer.py` Blob：

```text
1cd7736412ba7270ba46e5fb2ea2878e1f59b5eb
```

一致。

## 2.4 CI

Workflow Run：

```text
29674829682
status: completed
conclusion: failure
```

Jobs：

```text
test (3.11)
    conclusion: failure
    steps: None

test (3.12)
    conclusion: failure
    steps: None
```

这不是代码测试红灯，而是 Job 没有获得 Runner。

但 Release Gate 仍然没有通过。

## 2.5 Review

PR 没有未解决 Review Thread。

---

# 3. 审计范围

## 3.1 传输数据模型

- TransferAttempt；
- TransferSummary；
- Logical File；
- Physical Attempt；
- Payload；
- Wire Bytes；
- Active Time；
- Retry Credits；
- Completed Paths；
- Zero-byte；
- Callback Rollback；
- Duplicate Callback；
- Oversized Callback。

## 3.2 实时展示

- TTY；
- 非 TTY；
- `\r`；
- 250ms Throttle；
- 1.5s Sliding Window；
- Completion Line；
- Small Sample；
- IEC Units；
- Mbps；
- Workspace Label。

## 3.3 Protocol

- FTP；
- Paramiko；
- Native OpenSSH；
- Ordinary Source；
- Incremental Output；
- SFTP Hybrid Stage；
- FTP Hybrid Stage；
- FTP Hybrid Restage；
- Retry；
- Reconnect。

## 3.4 状态机回归

- Build；
- Freeze；
- Remote Plan；
- Upload；
- Prune；
- Ownership；
- Remote Command；
- State；
- Recovery；
- Pending；
- Cleanup。

## 3.5 发布门禁

- Main；
- Tag；
- PR；
- CI；
- Tests；
- Ruff；
- ty；
- Build；
- Isolated Install。

---

# 4. 已正确实现的能力

## 4.1 部署级 Reporter

每个项目执行只创建一个：

```python
ProgressReporter(...)
```

普通上传与 Hybrid 上传共享同一个 Reporter。

Workspace 传入：

```text
progress_label = repository name
```

因此输出：

```text
[frontend] TRANSFER SUMMARY
[backend] TRANSFER SUMMARY
```

不会错误合并不同 Target 或不同服务器。

## 4.2 Logical Payload 去重

Reporter 使用：

```python
_totals[path]
_completed
```

最终 Payload：

```text
sum(total for completed unique path)
```

同一路径的 Retry 不会重复增加 Logical File Count 或 Payload。

## 4.3 Physical Attempt 字节

Callback 使用累计值转换为正向 Delta：

```text
delta = current - previous
```

以下情况不会重复计数：

- 相同累计值；
- 超过 Total 的值；
- 重复 100% Callback。

Callback 回退：

```text
current < previous
```

会创建隐式新 Attempt，避免负数。

## 4.4 Explicit Retry Credit

外层 Retry Hook：

```python
record_retry(path)
```

会：

- 关闭旧 Attempt；
- Retry +1；
- 保存 Credit。

下一次注册 Callback 时消费 Credit，不会重复加 Retry。

## 4.5 Sliding Window

每个 Attempt 保留：

```text
(timestamp, cumulative attempt bytes)
```

并使用最近窗口计算速率。

TTY 最短刷新间隔：

```text
0.25s
```

非 TTY 不输出中间动态行。

## 4.6 Small Sample

当：

```text
payload < 1 MiB
or
active time < 1s
```

显示：

```text
sample too small
```

避免输出看似精确的 Mbps 网络判断。

## 4.7 Zero-byte

Zero-byte Callback：

```text
callback(0, 0)
```

可正确：

- 标记文件完成；
- Files +1；
- Payload = 0；
- Wire = 0；
- 不除零。

## 4.8 FTP Hybrid

Stage：

```text
STOR
Callback
RETR Verify
```

Reporter 在 Callback 完成时关闭上传计时。

RETR Verify 不增加 Wire Bytes，也不增加 Active Upload Time。

如果 RETR Verify 失败，Retry Hook 会重开 Attempt，下一次 STOR 正确累计 Wire Bytes。

Publish 阶段仅当 Stage 缺失需要 Restage 时才创建新的 Upload Callback。

## 4.9 SFTP Hybrid 逻辑路径

SFTP Stage 的内部路径：

```text
.git-deploy/stage/<id>/assets/app.js
```

通过 `display_path` 显示为：

```text
assets/app.js
```

避免用户看到内部 Stage 路径，并保证同一逻辑文件 Retry 去重。

## 4.10 失败部署

Summary 只在成功路径调用。

以下失败不显示成功 Summary：

- Upload Exhausted；
- Remote Command Failure；
- Ownership Failure；
- State Failure；
- Recovery Failure。

---

# 5. P1-01：Native OpenSSH 测量契约不成立

## 5.1 当前实现

`OpenSSHSFTPTransport.upload()`：

```text
创建父目录
callback(0, size)
sftp batch:
    put local temporary
    chmod temporary
publish temporary → final
callback(size, size)
```

没有任何中间 Callback。

## 5.2 实时速率

TTY 第一次 Callback：

```text
0%
0 B/s
```

随后整个 SFTP Batch 阻塞。

完成后：

```text
100%
avg ...
```

因此 Native 实际没有：

- 实时百分比；
- 滑动速率；
- 250ms 动态更新。

## 5.3 Active Time

Reporter 在 `callback(0)` 时开始计时，在最终 `callback(size)` 时关闭。

最终 Callback 位于：

```text
put
chmod
publish rename
```

全部完成之后。

因此 Native 的 Active Time 实际是：

```text
Upload + chmod + publish
```

Release Notes 则声明 Active Time 排除 Rename。

## 5.4 Wire Bytes

Native 失败场景：

```text
callback(0)
put 发送了一部分
连接中断
没有中间 Callback
```

Reporter 看到：

```text
Attempt Wire Bytes = 0
```

Retry 成功后：

```text
Wire Bytes = 最终完整文件大小
Retries = 1
```

实际网络可能已经发送：

```text
部分失败字节 + 完整重传
```

因此 Native `wire bytes` 不是所有 Attempt 字节。

## 5.5 影响

用户可能得出错误结论：

```text
wire bytes == payload
    → 没有重传流量
```

但 Native 其实已经发生部分失败上传。

或者看到一个较低 Average，误认为网络慢，而实际耗时包含 chmod/rename。

## 5.6 为什么测试没有发现

v1.6.0 新增的 Reporter 测试使用：

- Fake Callback；
- Fake Clock；
- FTP Callback；
- Generic Retry。

`tests/test_transports.py` 对 Native 没有新增分块、失败部分字节或实时速率测试。

原有 Native Transport 只证明上传成功，不证明 Telemetry Contract。

## 5.7 最小修复建议

不推荐为了一个小功能引入：

- 后台 Remote Stat Polling；
- 解析 OpenSSH 非稳定进度文本；
- 远端 rsync 依赖；
- 并发 Shell Probe。

推荐引入明确的测量能力：

```python
class TransferMeasurementMode:
    STREAMING
    COARSE
```

### FTP / Paramiko

```text
mode = STREAMING
current speed = available
partial retry bytes = available
```

### Native OpenSSH

```text
mode = COARSE
current speed = unavailable
wire bytes = reported successful bytes / lower bound
average = coarse publish throughput
```

Native 输出建议：

```text
UPLOAD assets/app.js ... transferring (Native batch)
UPLOAD assets/app.js 100% 6.0 MiB avg publish 2.36 MiB/s (coarse)

TRANSFER SUMMARY
  measurement:    coarse Native batch
  payload:        6.0 MiB
  reported bytes: >= 6.0 MiB
  retries:        1
```

必须避免继续把它显示成与 FTP/Paramiko 相同精度的：

```text
wire bytes
average upload
real-time rate
```

---

# 6. P1-02：Reporter 输出不是 Fail-open

## 6.1 当前行为

动态行、完成行和 Summary 直接调用：

```python
print(..., flush=True)
```

没有捕获输出异常。

## 6.2 上传阶段

如果 Callback 内输出抛出异常：

```text
UnicodeEncodeError
BrokenPipeError
OSError
```

异常会传播到 Transport Upload。

Deployer 会将其当成：

```text
Upload Failure
```

触发：

- Retry；
- Reconnect；
- 重传；
- 最终部署失败。

可观测性错误因此改变了网络操作。

## 6.3 State 提交后

普通部署：

```text
Remote 成功
Command 成功
Transport Close
State Save
render_summary
```

Summary 输出异常时：

```text
State 已经是新值
命令却报告失败
```

Hybrid：

```text
Remote Ownership 已提交
State 已保存
Recovery/Pending 已推进
render_summary
```

外层会把 Summary 异常包装成 Deployment Failure。

## 6.4 典型触发

- `stderr` 被关闭；
- `2>&1 | head` 下游提前退出；
- 日志采集器关闭 Pipe；
- Windows/旧 Locale 无法编码 Unicode Path；
- 测试或嵌入调用传入故障 Stream。

## 6.5 修复

Reporter 增加：

```python
_render_disabled = False
```

所有输出通过：

```python
_safe_print()
```

捕获：

```text
BrokenPipeError
OSError
UnicodeError
```

首次失败后：

```text
禁用后续显示
继续统计
绝不影响 Upload / State / Recovery
```

`render_summary()` 也必须 Fail-open。

测试：

```text
Stream.write raises OSError
Upload must still succeed
State must commit
Reporter must stop rendering
```

---

# 7. Release Gate：CI 未执行

## 7.1 当前事实

GitHub Actions Run 是 Failure。

两个 Job 都没有 Step。

这意味着：

```text
Python 3.11 未在 CI 执行
Python 3.12 未在 CI 执行
Ruff 未在 CI 执行
ty 未在 CI 执行
Build 未在 CI 执行
Wheel Smoke 未在 CI 执行
```

## 7.2 PR 记录

PR 描述声明本地执行：

```text
302 passed on Python 3.11
302 passed on Python 3.12
Ruff passed
ty passed
Lock passed
Build passed
Wheel smoke passed
```

这些记录有参考价值，但不是独立 Runner 证据。

## 7.3 要求

修复：

- GitHub 账单；
- Actions Spending Limit；
- Runner 可用性。

然后对以下任一精确对象重新运行：

```text
PR Head 55e92eb...
Merge Commit 6138bf...
Tag v1.6.0
```

推荐验证 Merge Commit 或 Tag。

在 CI 全绿前：

```text
不要把 README 或 Release Page 标记为 CI Verified
```

---

# 8. P2-01：FTP / Paramiko Active Time 包含父目录准备

## 8.1 Timer 起点

Reporter 在：

```python
progress.callback(path, total)
```

被创建时立即启动 Attempt。

Deployer 在进入：

```python
transport.upload(...)
```

之前创建 Callback。

## 8.2 FTP

FTP Transport 在真正 `STOR` 前执行：

```text
_mkdirs(parent)
```

因此 Active Time 包含：

- MKD；
- Existing Directory 550；
- CWD Probe；
- Parent Traversal。

大量小文件时，这部分可能远大于数据传输时间。

## 8.3 Paramiko

Paramiko 在 `sftp.put()` 前同样执行：

```text
_mkdirs(parent)
```

Active Time 也包含远端目录准备。

## 8.4 Native

Native 则在 `_mkdirs()` 完成后才调用：

```text
callback(0)
```

因此三个 Backend 的计时起点不一致。

## 8.5 影响

最终 Average 不能被严格解释为：

```text
上传字节 / 纯上传时间
```

尤其在：

```text
大量小文件
深层目录
高 RTT FTP
```

时，数值更接近：

```text
上传 + 每文件目录确认吞吐量
```

## 8.6 修复

把 Attempt 分为：

```text
registered
active
completed
```

Reporter 注册 Callback 时不开始计时。

所有 Built-in Transport 在：

```text
父目录处理完成
即将发送第一个字节
```

时主动调用：

```python
callback(0, total)
```

然后 Reporter 在第一次 Callback 时开始 Active Time。

修改：

### FTP

```text
_mkdirs
callback(0, total)
storbinary
```

### Paramiko

```text
_mkdirs
callback(0, total)
sftp.put
```

### Native

已有：

```text
_mkdirs
callback(0, total)
```

这样三个 Backend 的起点一致。

---

# 9. P2-02：`wire bytes` 名称过度承诺

当前统计的是：

```text
Transport Callback 报告的应用层上传 Payload Bytes
```

不是物理网络 Wire Bytes。

它不包含：

- FTP Command；
- SFTP Packet Header；
- SSH Encryption Overhead；
- TCP/IP；
- TLS；
- ACK；
- Retransmission below application callback。

即使 FTP/Paramiko，也可能少计：

```text
sendall 中途失败但 Callback 尚未执行的最后一个 Block
```

推荐改名：

```text
attempt bytes
```

或：

```text
reported upload bytes
```

如果保留 `wire bytes`，文档至少声明：

```text
application-reported lower bound
```

---

# 10. P3：显示精度与终端细节

## 10.1 Rate Double Rounding

`format_rate()` 先调用：

```text
format_bytes()
```

后者先四舍五入到 1 位小数。

随后再格式化成 2 位小数。

例如：

```text
真实：3.25 MiB/s
中间：3.2 MiB
显示：3.20 MiB/s
```

显示了两位小数，但精度只有一位。

应直接按选定单位格式化原始 Rate。

## 10.2 CJK 宽度

TTY 清行 Padding 使用：

```python
len(line)
```

中文、日文和韩文通常占两个终端列。

Unicode Path 可能留下尾部残影。

可选：

- 使用 `wcwidth`；
- 或固定 ANSI `\x1b[K` 清除到行尾；
- 无 ANSI 环境保持当前 Padding。

## 10.3 `verbose` 未参与 Reporter 行为

`ProgressReporter.verbose` 当前没有被读取。

Release Notes 声明：

```text
--verbose 也遵守同样节流
```

所以功能结果没有错，但字段属于死状态。

可以：

- 删除；
- 或让 Verbose 决定是否保留完成行历史。

---

# 11. 继承自 v1.5.3 的未整改项

本次未修改相关模块，以下 P2/P3 仍存在。

## 11.1 FILES_PUBLISHED Plan / Executor

Planner 仍可能在 `FILES_PUBLISHED` 显示 Upload，但 Executor 直接 Prune。

当前文件在 Pending 期间被外部删除时，可能不重传。

在单发布器边界内不阻断。

## 11.2 FTP Recovery Alias Freshness

Recovery Alias Gate 仍位于 Cache Refresh 之前。

确认窗口中的新 Alias 可能不被本次 Gate 重新读取。

Recovery 只处理精确内部路径，风险有限。

## 11.3 Doctor UTF-8 / Alias

Doctor 使用 Schema 3 Profile 后没有统一执行 UTF-8 Activation 与 Alias Report。

可能出现：

```text
部署正常
Doctor 误报
```

## 11.4 FTP Root Metadata Cache

普通 FTP Upload/Delete 没有统一调用：

```text
_clear_remote_caches()
```

Hybrid Freshness 主动刷新，因此当前不阻断。

---

# 12. 测试覆盖评价

## 12.1 已覆盖良好

- Sliding Window；
- TTY Throttle；
- Non-TTY；
- Completion；
- Summary；
- Payload Dedup；
- Retry Credit；
- Callback Rollback；
- Duplicate Callback；
- Oversized Callback；
- Multiple Files；
- Zero-byte；
- Small Sample；
- IEC Units；
- Workspace Labels；
- Failed Deployment No Summary；
- FTP Callback。

## 12.2 缺失关键测试

- Native OpenSSH 中间速率；
- Native 部分失败 Wire Bytes；
- Native Active Time 是否包含 Rename；
- Paramiko Parent Setup 是否计时；
- FTP Parent Setup 是否计时；
- Reporter Stream Failure；
- State Commit 后 Summary Failure；
- Unicode Console Encoding Failure；
- Real TTY CJK Width；
- GitHub Actions Matrix 实际执行；
- 实际外部 FTP/SFTP 100MiB Canary。

---

# 13. v1.6.1 最小整改计划

## P1：Measurement Quality

### TODO-001：定义模式

```python
STREAMING
COARSE
```

### TODO-002：Backend 绑定

```text
FTP              → STREAMING
Paramiko         → STREAMING
Native OpenSSH   → COARSE
```

### TODO-003：Native 输出

- [x] 不显示虚假的实时速率；
- [x] 显示 `transferring`；
- [x] Average 标注 `coarse publish rate`；
- [x] Bytes 标注 Lower Bound；
- [x] Retry 说明部分失败字节不可见。

---

## P1：Fail-open Rendering

### TODO-101：Safe Output

- [x] 捕获 BrokenPipeError；
- [x] 捕获 OSError；
- [x] 捕获 UnicodeError；
- [x] 禁用后续渲染；
- [x] 不影响统计；
- [x] 不影响部署结果。

### TODO-102：Post-state Test

- [x] State 已保存；
- [x] Summary Stream Failure；
- [x] Command Exit Success；
- [x] 不触发 Retry。

---

## P2：Timer Boundary

### TODO-201：Deferred Attempt Start

- [x] Callback 注册不启动计时；
- [x] 第一次 Callback 启动；
- [x] Retry Before First Byte；
- [x] Zero-byte。

### TODO-202：Transport Start Signal

- [x] FTP `_mkdirs` 后 Callback(0)；
- [x] Paramiko `_mkdirs` 后 Callback(0)；
- [x] Native 保持 Callback(0)。

---

## P2：Naming

### TODO-301

将：

```text
wire bytes
```

改为：

```text
attempt bytes
```

或者显示：

```text
reported wire bytes (lower bound)
```

> **状态：已完成。** Streaming Summary 使用 `attempt bytes`；Native Coarse Summary 使用 `reported bytes: >=` 并明确失败部分字节可能不可见。

---

## Release Gate

### TODO-401

- [ ] 修复 Actions Billing；
- [ ] 修复 Spending Limit；
- [ ] 重新运行 Python 3.11；
- [ ] 重新运行 Python 3.12；
- [ ] Ruff；
- [ ] ty；
- [ ] Build；
- [ ] Isolated Wheel；
- [ ] Tag/Main 一致性。

> **v1.6.1 落实记录（2026-07-19）**：代码整改项 TODO-001 至 TODO-301 已完成并通过定向自动测试。TODO-401 中本地 Python、Ruff、ty、Build、Isolated Wheel 与 Tag/Main 项在发布门禁执行后逐项更新；Actions Billing/Spending Limit 属于仓库外部账号设置，不通过代码或读取真实凭据绕过。实际外部 100MiB/小文件 Canary 保持独立可选人工增强，不反向阻塞 Mock、pyftpdlib 与容器化 OpenSSH 主线。

### v1.6.1 本地自动门禁

- [x] Python 3.11：314 passed；
- [x] Python 3.12：314 passed；
- [x] Ruff；
- [x] ty；
- [x] `uv lock --check`；
- [x] wheel/sdist Build；
- [x] Isolated Wheel Install、`--version` 与 `--help`。

> 上述为本地证据，不替代 TODO-401 的独立 GitHub Actions Runner 证据；Actions 与 Tag/Main 状态在远端发布流程中继续核验。

---

# 14. 修复后验收标准

1. FTP 大文件有稳定实时速率；
2. Paramiko 大文件有稳定实时速率；
3. Native 明确显示 Coarse；
4. Native 不再宣称失败部分 Wire Bytes 已精确统计；
5. FTP/Paramiko Active Time 不包含 Parent MKD；
6. Reporter 输出异常不会触发 Retry；
7. Reporter 输出异常不会让已提交 State 的部署报失败；
8. Summary 格式精度真实；
9. Workspace 每仓汇总；
10. Delete-only 无 Summary；
11. FTP Hybrid RETR 不计入 Upload；
12. Python 3.11 CI 真正执行并通过；
13. Python 3.12 CI 真正执行并通过；
14. Ruff/ty/Build/Wheel CI 通过；
15. 实际目标进行 100MiB Canary；
16. 实际目标进行小文件集 Canary。

---

# 15. 当前使用建议

## 可以使用

```text
FTP 实时速率
Paramiko SFTP 实时速率
FTP Hybrid Stage/Restage 速率
Payload/Retry 去重
Workspace 每仓 Summary
```

## 使用时需要注意

### Native OpenSSH

当前显示应理解为：

```text
粗粒度文件发布平均
```

而不是：

```text
实时网络上传速率
精确 Wire Bytes
```

不要仅根据 Native Summary 判断：

- 是否发生部分重传；
- 当前链路瞬时速率；
- SFTP Rename/Publish 开销与网络的区别。

### CI

当前 Release 未经过有效 GitHub Actions Matrix。

在生产关键环境启用前，至少应：

- 本机运行 3.11/3.12；
- 修复 CI；
- 进行目标服务器 Canary。

### 大量小文件

当前 Active Time 包含 FTP/Paramiko Parent Setup。

小文件场景的 Average 会反映：

```text
网络 + FTP/SFTP RTT + 目录确认
```

这可以反映实际部署体验，但不能等价为纯带宽。

---

# 16. 最终结论

v1.6.0 没有破坏 v1.5.3 的部署安全基线。

它正确实现了：

```text
Deployment-scoped Reporter
Payload Dedup
Retry Attempt Accounting
Sliding Window
TTY / Non-TTY
FTP Hybrid Upload-only Metrics
Workspace Summary
```

FTP 与 Paramiko 路径已经具备较实用的网络观察价值。

但是：

- Native OpenSSH 没有真实分块进度；
- Native Wire Bytes 会漏掉失败尝试的部分流量；
- Native Average 包含 Publish；
- Reporter 输出可能改变部署结果；
- FTP/Paramiko Timer 起点包含 Parent Setup；
- GitHub Actions 没有真正执行。

因此：

> **git-deploy v1.6.0 有条件通过。**

建议：

> **不要回滚 v1.6.0；先将 FTP/Paramiko 作为可信 Streaming Telemetry 使用。发布一个最小 v1.6.1，完成 Native 诚实标注、Reporter Fail-open、统一计时起点，并在修复 Actions 额度后重新执行完整 CI。**
