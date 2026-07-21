# git-deploy v1.7.2 最新代码深度审计报告

> 仓库：`howjc/git-deploy`  
> 分支：`main`  
> 最新提交：`b414fafdd2fd174bf78fed5566f3e7cec583efd3`  
> 功能修复提交：`291dea5e683f9ea6d28602d1d856add067078317`  
> Tag：`v1.7.2`  
> 审计日期：`2026-07-21`  
> 审计结论：**有条件通过**  
> 建议动作：发布最小 `v1.7.3`，修复真实 stdout 缓冲与 Broken Pipe 退出语义；不要扩展其他功能。

---

# 1. 执行摘要

v1.7.2 针对 v1.7.1 审计剩余的两个 P2 进行收口：

1. Target Lock Acquire 的 PermissionError/OSError 转成单条目 FAIL；
2. Target Lock Release 异常不再覆盖成功结果；
3. `execute_bootstrap_item()` 意外异常由 Batch 外层转换成 FAIL；
4. 后续 Target 继续执行；
5. Plan 输出失败时拒绝确认和远端写；
6. Summary 输出失败时保留已计算 Exit Code；
7. Main 与 `v1.7.2` Tag 完全一致；
8. Package、Runtime Version 均为 `1.7.2`。

Lock/Batch 修复主链成立。

本轮没有发现：

```text
P0
FTP Hybrid 删除安全回归
Ownership / Pending / State 回归
业务路径扩大
```

但是，输出修复在真实 CLI 管道下没有完全成立。

## 新 P1：未 Flush 的 stdout 破坏 Plan Fail-closed 与 Summary Fail-open

当前 `_emit_bootstrap_output()`：

```python
try:
    print_fn(text)
    return True
except (...):
    return False
```

默认 `print_fn=print`，没有：

```text
flush=True
sys.stdout.flush()
```

在非 TTY 管道中，stdout 是块缓冲。

因此：

```text
print(Plan)
    → 只写入 Python 用户态缓冲区
    → 没有真正送达下游
    → 函数返回 True
    → Bootstrap 执行远端写
```

如果下游管道已经关闭，BrokenPipe 可能直到解释器退出 Flush 时才出现。

后果：

1. Plan 实际未可见，但 Remote Mutation 已经发生；
2. `_emit_bootstrap_output()` 没有捕获到错误；
3. Python 解释器退出时报告 BrokenPipe；
4. 成功 Bootstrap 可能以退出码 `120` 结束。

本轮在审计环境用最小真实 Python 子进程模型复现：

```text
stdout → head -c 0
remote mutation marker = created
process exit code = 120
```

所以 Release Notes 声明的：

```text
Plan output fail-closed
Summary output fail-open
```

只在注入式 `print_fn` 单元测试中成立，在真实缓冲 stdout 中不完整。

此外还有一个 P2：

- TargetLock 在已经成功 flock 后、Owner Metadata 写入或 fsync 失败时，没有显式 Close/Unlock 清理；当前依赖 Python 栈展开释放本地 File Object，建议在 Lock 类内部确定性清理。

CI 方面：

- `v1.7.2` Tag 已存在且与 Main Commit 完全一致；
- 但 Release Commit 没有关联 Workflow Run；
- Combined Status 为空；
- 审计环境无法解析 `github.com`，不能独立 Clone 和复跑测试。

综合判断：

```text
Lock I/O Isolation：
    通过

Batch Continue：
    通过

Injected Output Tests：
    通过

Real stdout Pipe Contract：
    未通过

Tag：
    通过

CI：
    未闭合

整体：
    有条件通过
```

---

# 2. 版本与发布状态

## 2.1 Main

```text
b414fafdd2fd174bf78fed5566f3e7cec583efd3
release v1.7.2: close remaining bootstrap batch and output P2 gaps
```

## 2.2 功能提交

```text
291dea5e683f9ea6d28602d1d856add067078317
fix(bootstrap): isolate lock I/O and fail-open summary output
```

## 2.3 Tag

```text
v1.7.2
```

与 Main：

```text
status: identical
ahead: 0
behind: 0
```

## 2.4 Package

```toml
version = "1.7.2"
```

## 2.5 Runtime

```python
__version__ = "1.7.2"
```

## 2.6 核心 Blob

Main 与 Tag：

```text
bootstrap.py:
3fe88da0583fbe747ce4de9447aa599953b73489

pyproject.toml:
a320824c07ce546d3a8fd23c833c46cec3b687b4
```

一致。

---

# 3. CI 与独立验证

Release Commit：

```text
workflow_runs: []
combined statuses: []
```

本轮尝试：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

仍失败：

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

仓库增加了针对性测试，但没有独立 CI 执行证据。

---

# 4. v1.7.1 P2：Lock I/O Batch Isolation

## 4.1 Acquire

现在：

```python
try:
    lock.acquire()
except Exception as exc:
    return BootstrapResult(item, False, None, str(exc))
```

覆盖：

- Busy Lock PlanError；
- Parent mkdir PermissionError；
- Lock File open OSError；
- fsync/ENOSPC；
- 其他普通 Exception。

Acquire 失败发生在 Transport Factory 前：

```text
Remote Connect = 0
Remote Mutation = 0
```

## 4.2 Factory / Connect / Probe

均在：

```python
try:
    ...
except Exception:
    FAIL Result
finally:
    transport close
    lock release
```

范围内。

## 4.3 Release

Release Error 被保护：

```python
try:
    lock.release()
except Exception:
    pass
```

成功 Probe 不会被 Release Display/I/O Error 改写成进程崩溃。

## 4.4 Outer Batch

`execute_bootstrap()` 改为显式循环：

```python
for item in items:
    try:
        execute_bootstrap_item(...)
    except Exception:
        append FAIL
```

不会因一个意外异常停止后续 Target。

`KeyboardInterrupt` 和 `SystemExit` 不属于 Exception，仍正确向外传播。

## 4.5 测试

覆盖：

- Acquire PermissionError；
- Acquire ENOSPC；
- Release OSError；
- Unexpected Item Escape；
- Later Target Continues；
- Correct Exit Code。

## 4.6 结论

> **上一轮 Lock/Batch P2 主问题已关闭。**

---

# 5. P1：真实 stdout 缓冲使 Output Contract 失效

## 5.1 当前实现

```python
def _emit_bootstrap_output(print_fn, text):
    try:
        print_fn(text)
        return True
    except (...):
        return False
```

## 5.2 默认 CLI

默认：

```python
print_fn = print
```

没有：

```python
flush=True
```

## 5.3 TTY 与 Pipe 差异

TTY 通常行缓冲，因此问题较难触发。

Pipe/File 通常块缓冲：

```text
Plan Print
    → 进入 TextIOWrapper Buffer
    → 未触发 OS Write
    → 返回成功
```

## 5.4 Plan Fail-closed 失效

设计要求：

```text
Plan 未成功显示
    → 不得远端变更
```

实际可能：

```text
下游已关闭
Plan 只进入本地缓冲
_emit 返回 True
Confirm --yes
Root / Probe Remote Mutation
进程退出时才 BrokenPipe
```

这意味着 Plan Gate 只验证：

```text
Python print() 没有立即报错
```

并没有验证：

```text
Plan 已成功送达输出流
```

## 5.5 Summary Fail-open 失效

Summary 较小时也可能只进入缓冲。

`_emit` 返回 True 后，解释器 Shutdown Flush 才发现 BrokenPipe。

Python 此时可能：

```text
Exception ignored on flushing sys.stdout
BrokenPipeError
Exit Code 120
```

因此已经计算出的：

```text
0 / 1
```

可能被改写成：

```text
120
```

## 5.6 独立复现

最小模型：

```python
try:
    print(plan)
except BrokenPipeError:
    ...

perform_remote_mutation()

try:
    print(summary)
except BrokenPipeError:
    ...

raise SystemExit(0)
```

运行：

```bash
python model.py | head -c 0
```

本轮实测：

```text
remote mutation marker: yes
python exit: 120
```

## 5.7 为什么当前测试没发现

测试注入：

```python
def broken_print(...):
    raise BrokenPipeError
```

异常发生在函数调用内部，所以 `_emit` 能捕获。

真实 stdout 的关键场景是：

```text
print 不抛
Shutdown Flush 才抛
```

现有测试没有启动真实子进程和真实关闭 Pipe。

## 5.8 严重性

```text
P1
```

原因：

- 明确违反 Plan Fail-closed 安全承诺；
- Remote Mutation 后可能返回错误 Exit Code；
- 自动化容易误判；
- 发生在 `--yes` 非交互使用路径。

它不删除业务内容，但破坏了用户确认和命令成功语义。

---

# 6. 建议修复

## 6.1 输出接口改成 Stream

推荐：

```python
def run_bootstrap(..., output: TextIO = sys.stdout):
    ...
```

而不是抽象 `print_fn`。

## 6.2 强制 Flush

Plan：

```python
output.write(render_bootstrap_plan(plan) + "\n")
output.flush()
```

Flush 失败：

```text
ConfigError
Remote Mutation = 0
```

Summary：

```python
try:
    output.write("\n")
    output.write(render_bootstrap_summary(results) + "\n")
    output.flush()
except (BrokenPipeError, OSError, UnicodeError, ValueError):
    silence_broken_stdout()
return exit_code
```

## 6.3 Broken Pipe 后静音 stdout

仅捕获 BrokenPipe 不足以阻止解释器 Shutdown Flush。

参考实现：

```python
def _silence_stdout() -> None:
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull_fd, sys.stdout.fileno())
    finally:
        try:
            os.close(devnull_fd)
        except OSError:
            pass
```

或替换到一个已经安全打开的 `/dev/null` Stream，并确保旧 stdout 不再在 Shutdown 触发 Broken Pipe。

## 6.4 最小兼容方案

如果保留 `print_fn`：

```python
def _emit_bootstrap_output(
    print_fn,
    text,
    *,
    flush_fn=None,
) -> bool:
    try:
        print_fn(text)
        if flush_fn is not None:
            flush_fn()
        return True
    except (...):
        return False
```

CLI 传入：

```python
print_fn=print
flush_fn=sys.stdout.flush
```

测试可注入独立 `flush_fn`。

但 Stream 设计更清晰。

## 6.5 必须新增测试

### Actual Subprocess Plan Pipe

```text
git-deploy bootstrap --yes | head -c 0
```

验证：

```text
Remote Probe = 0
```

### Actual Subprocess Summary Pipe

让消费端完整读取 Plan，远端执行后在 Summary 前关闭。

验证：

```text
Profile Saved = True
Process Exit = Computed Exit Code
不是 120
```

### Buffered Fake Stream

`write()` 成功、`flush()` 抛 BrokenPipe。

验证：

```text
Plan → Zero Mutation
Summary → Preserve Exit
```

---

# 7. P2：TargetLock Partial Acquire Cleanup 不够显式

## 7.1 当前 TargetLock Acquire

流程：

```text
open handle
flock LOCK_EX
write owner metadata
flush
fsync
self._handle = handle
```

如果：

```text
flock 已成功
fsync/metadata write 失败
```

`self._handle` 尚未赋值。

外层只能拿到异常，无法显式：

```text
unlock / close handle
```

CPython 通常会在栈展开时释放 Local File Object，但这是隐式资源释放。

## 7.2 建议

在 `TargetLock.acquire()` 内：

```python
handle = ...
try:
    flock(...)
    write_metadata(...)
    self._handle = handle
except BaseException:
    try:
        flock(handle.fileno(), LOCK_UN)
    except Exception:
        pass
    handle.close()
    raise
```

使用 `BaseException` 仅用于资源清理后重新抛出，确保 KeyboardInterrupt 时也不留下锁。

## 7.3 定级

```text
P2
```

不会影响正常路径，但能让 Lock I/O 加固真正具备确定性。

---

# 8. 继承的低优先级问题

## 8.1 Workspace Config 双重加载

仍然先加载收集 Known Target，随后再次加载生成 Candidate。

定级：

```text
P3
```

## 8.2 FTP Hybrid Retry Counter

极少数 Restage/Publish 组合失败中，Retry 数可能略高于实际额外 Upload Attempt。

定级：

```text
P2 Telemetry
```

## 8.3 FILES_PUBLISHED Plan/Executor

外部修改远端当前文件时，恢复路径存在展示/验证差异。

在单发布器边界内不阻断。

---

# 9. v1.7.3 最小整改范围

只修复：

1. Plan 输出强制 Flush；
2. Summary 输出强制 Flush；
3. Broken Pipe 后防止 Shutdown 二次 Flush；
4. 真实子进程 Pipe 测试；
5. TargetLock Partial Acquire 确定性 Close/Unlock。

不要加入：

- 并行 Bootstrap；
- 自动 Deploy；
- 自动 Adoption；
- 历史 Metrics；
- 新 Profile Schema；
- 新状态机。

---

# 10. 修复后验收标准

1. Plan Flush 失败时 Remote Mutation = 0；
2. Summary Flush 失败时 Profile/Root 保留；
3. Summary Flush 失败时 Exit Code 保持 0/1；
4. 不出现 Exit 120；
5. `head -c 0` 实际子进程测试；
6. Lock fsync 失败后可立即重新获取锁；
7. KeyboardInterrupt during Lock metadata 不残留锁；
8. 后续 Target 继续；
9. Main/Tag 一致；
10. Python 3.11/3.12 CI 通过。

---

# 11. 当前使用建议

v1.7.2 在直接终端使用时可以继续使用：

```bash
git-deploy bootstrap --yes
```

暂时不要依赖以下模式的退出码：

```bash
git-deploy bootstrap --yes | head
git-deploy bootstrap --yes | consumer-that-closes-early
```

输出到普通完整文件通常没有提前关闭问题：

```bash
git-deploy bootstrap --yes > bootstrap.log
```

但正式自动化仍建议等待 Flush 修复。

---

# 12. 最终结论

v1.7.2 已关闭：

```text
Lock Acquire Error Batch Isolation
Lock Release Result Protection
Outer Batch Continue
Injected Print Failure Handling
```

Tag 和 Main 也已经一致。

但是，输出实现没有处理真实 stdout 缓冲与解释器 Shutdown Flush。

所以：

> **git-deploy v1.7.2 有条件通过。**

当前不应宣称：

```text
Plan output fully fail-closed
Summary output fully fail-open
```

建议：

> **发布一个极小 v1.7.3，完成 Flush + Broken Pipe Shutdown 处理和真实子进程测试；完成后 Bootstrap 工作流可以正式结束连续补丁阶段。**
