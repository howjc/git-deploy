# git-deploy v1.8.0 最新代码深度审计报告

> 仓库：`howjc/git-deploy`  
> 分支：`main`  
> 最新提交 / Release Commit：`9ed48e749a31e857e1e6ab12e22ee2f1e6b403ac`  
> 主要安全收口提交：`6664dc4facdd49a79d31a1b33c8109bdaf260e9c`  
> Tag：`v1.8.0`，与 Release Commit 一致  
> 审计日期：`2026-07-30`  
> 审计结论：**不通过**  
> 阻断原因：**Bash 静态补全可被本地 Target 名称触发命令执行**  
> 建议版本：`v1.8.1` 安全修复版

---

# 1. 执行摘要

v1.8.0 对上一轮审计的主要整改响应是完整的：

1. FTP Pending Schema 2 的 Manifest Hash 恢复 v1.7.3 兼容语义；
2. Mirror 嵌套文件继续写入 Local State，但不重复进入 Pending Hash 的 `outputs` 部分；
3. `PREPARED`、`FILES_PUBLISHED`、`PRUNED` 三个旧阶段均有兼容测试；
4. `ftp_connections` 默认值从 4 恢复为 1；
5. 并行改为显式 Opt-in；
6. Stage/Publish 在开始 Worker 前先建立全部可用 Session；
7. Sibling 建连失败时，在远端业务工作开始前降级；
8. 普通 CLI 不再自动修改 `.bashrc/.zshrc`；
9. 显式 `completion install` 使用安装锁、临时文件、`fsync` 和 `os.replace`；
10. RC 中的路径使用 `shlex.quote`；
11. Zsh 脚本增加 `#compdef git-deploy`；
12. Mirror 增量与强 Mirror 变成显式配置契约；
13. Remote Plan 明示是否验证远端内容；
14. 最终路径 RETR 变成可选 `ftp_verify_final`；
15. 非 TTY 阶段进度改为约 2 秒采样；
16. Cleanup 失败使用 `CLEANUP PENDING`，不再显示虚假的 100%；
17. Package、Runtime、Changelog 和 Tag 均升级到 `1.8.0`。

FTP Hybrid 的核心状态机没有发现新的未知内容删除、Ownership 越权、Prune 提前或 State 提前提交问题。

但是，新补全机制存在一个本地命令执行漏洞：

> Bash 静态补全把从 `deploy.toml` 读取的 Target 名称直接传给 `compgen -W`。Bash 会再次展开 Word List 中的命令替换，而当前 Target 名称校验仍允许 `$()`、反引号、空格和 Shell 元字符。

因此，一个本地配置可以定义：

```toml
[targets."$(touch completion-proof)"]
protocol = "sftp"
host = "example.invalid"
username = "deploy"
remote_root = "/srv/app"
```

用户在该目录执行：

```text
git-deploy <Tab>
```

时，静态 Bash Completion 可能执行：

```text
touch completion-proof
```

本轮在隔离临时目录中使用与脚本一致的：

```bash
targets='$(touch completion-proof)'
compgen -W "$targets" -- ''
```

进行了独立验证，Marker 文件被实际创建。

这属于本地任意命令执行边界，因此本报告将其定为 **P0**。

另外发现一个 P1：

> FTP 并行 Session 建立阶段用 `except BaseException` 做降级，会把 `KeyboardInterrupt` 和 `SystemExit` 一并吞掉。用户在远端工作开始前按下 Ctrl+C，程序可能输出降级 Warning 后继续执行 Stage/Publish。

因此，v1.8.0 当前不能作为稳定版本通过。

---

# 2. 版本与发布状态

## 2.1 Main

```text
9ed48e749a31e857e1e6ab12e22ee2f1e6b403ac
feat: release v1.8.0 FTP Hybrid contract, progress UX, and gate fixes
```

## 2.2 主要修复提交

```text
6664dc4facdd49a79d31a1b33c8109bdaf260e9c
fix: harden FTP Hybrid pending, parallelism, and shell completion
```

## 2.3 Tag

```text
v1.8.0
```

Tag 与 Release Commit 比较结果：

```text
identical
ahead = 0
behind = 0
```

## 2.4 Package

```toml
version = "1.8.0"
```

## 2.5 Runtime

```python
__version__ = "1.8.0"
```

## 2.6 变更规模

相对 `v1.7.3`：

```text
9 commits
FTP Hybrid Mirror / Verify / Parallel / Progress
Shell Completion
Open-source Metadata / Documentation Cleanup
Release Gate 修复
```

---

# 3. CI 与独立验证

## 3.1 GitHub Actions

Release Commit 当前：

```text
workflow_runs: []
combined statuses: []
```

不能声称：

```text
v1.8.0 CI Verified
```

## 3.2 仓库内记录

Release Commit 和实施文档记录：

```text
pytest: 401 passed
Ruff: passed
ty: passed
Wheel/Sdist: passed
Wheel Completion Resources: passed
```

这些属于提交者本地验证记录，不是独立 Runner 证据。

## 3.3 本轮独立 Clone

本轮尝试：

```bash
git clone --depth 1 https://github.com/howjc/git-deploy.git
```

仍失败于：

```text
Could not resolve host: github.com
```

因此无法在独立 Checkout 中完整复跑测试。

## 3.4 当前动态证据

本轮能够独立执行的最小验证包括：

- TOML 允许包含 `$()` 和空格的 Target Key；
- Bash `compgen -W` 会执行 Word List 中的命令替换；
- `os.replace` 会把 RC 符号链接替换为普通文件。

---

# 4. 上一轮 P1-01：Pending Hash 兼容

## 4.1 v1.7.3 问题

新 Mirror State 把：

```text
assets/app.js
assets/css/app.css
```

加入 `plan.output_manifest`。

但 `local_manifest_hash()` 已经通过 Hybrid Directory Tree 序列化 Mirror 文件；若再把 Mirror State Key 放入 `outputs`，同一本地构建会产生新的 Hash，使 v1.7.3 Pending 无法恢复。

## 4.2 v1.8.0 修复

新增：

```python
pending_manifest_outputs()
pending_local_manifest_hash()
```

规则：

```text
Local State：
    保留完整 Root + Mirror Nested Hash

Pending local_manifest_hash：
    排除 Mirror Nested State Key
    保持 v1.7.3 Schema 2 输入形状
```

## 4.3 调用点

Pending 写入：

```python
pending_local_manifest_hash(hybrid.local, plan.output_manifest)
```

Pending 校验：

```python
manifest_snapshot =
    pending_local_manifest_hash(hybrid.local, plan.output_manifest)
```

没有残留直接使用 Full State Outputs 的 Pending 调用点。

## 4.4 兼容测试

新增：

- 完整 Mirror State Hash 与 v1.7.3 Hash 一致；
- 未过滤 Full State 会产生不同 Hash；
- `PREPARED` 可验证；
- `FILES_PUBLISHED` 可验证；
- `PRUNED` 可验证。

## 4.5 结论

> **上一轮 Pending Hash P1 已关闭。**

## 4.6 兼容后的状态结果

真实 v1.7.3 Pending 中的 `next_state.outputs` 可能仍不含 Mirror Nested Key。

恢复完成时会先保存旧形状的 Frozen State；下一次普通部署会因为 Previous Mirror Entry 缺失而重传一次，并保存完整新 State。

这是安全且可收敛的迁移结果。

---

# 5. 上一轮 P1-02：默认 FTP 并行

## 5.1 默认值

当前：

```python
ftp_connections = 1
```

配置范围：

```text
1–16
```

旧项目不配置时保持单连接。

## 5.2 Pool 建立顺序

当前顺序：

```text
Primary 已存在
尝试建立全部 Sibling
计算 Effective Pool
    ↓
全部建连流程结束后
    ↓
创建并启动 Worker
```

所以普通 Sibling 建连失败不会在 Pool 尚未确定时启动远端 Stage/Publish。

## 5.3 降级

如果请求：

```text
4 connections
```

但只能打开：

```text
2
```

则：

```text
WARNING
effective connections = 2
使用 2 条连接继续
```

若第一个 Sibling 就失败：

```text
降级到 Primary 串行
```

## 5.4 UTF-8 与 Banner

Sibling 在 Connect 前继承：

```text
_require_utf8
_required_server_banner_hash
```

每条 Session 仍需通过：

- Banner；
- FEAT UTF8；
- OPTS / Pure-FTPd always-on；
- Client Encoding。

## 5.5 测试

覆盖：

- `ftp_connections=1`；
- 多 Worker 分发；
- Pool 建立完成后才开始 Job；
- 第一个 Sibling 失败后降级到 1；
- Warning 与 Effective Connection Count。

## 5.6 结论

> **默认并行兼容 P1 主问题已关闭。**

但 Ctrl+C 被吞问题见 P1-01。

---

# 6. 上一轮 P1-03：Completion RC 安装安全

## 6.1 普通 CLI

普通 CLI 仍可能在用户数据目录安装缺失的 Completion Script，但：

```text
不会修改 .bashrc
不会修改 .zshrc
```

并提示：

```text
git-deploy completion install
```

RC 修改需要显式执行。

## 6.2 显式安装

```bash
git-deploy completion install
```

当前：

- 使用用户级 `flock`；
- 同目录创建临时文件；
- UTF-8 完整写；
- `flush`；
- `fsync`；
- 保留已有文件 Mode；
- `os.replace`；
- 异常时删除临时文件；
- OSError/UnicodeError 转换为 CLI ConfigError。

## 6.3 Shell Quote

RC Script Path 使用：

```python
shlex.quote()
```

避免 Home Path 中的：

```text
space
quote
$
backtick
```

进入 Shell Expansion。

## 6.4 Zsh

静态脚本已增加：

```zsh
#compdef git-deploy
```

## 6.5 结论

> **上一轮“静默、非原子改写 RC”的 P1 主问题已关闭。**

但 Bash Completion Command Injection 与 Symlink RC 问题仍存在，见后文。

---

# 7. FTP Mirror 与校验契约

## 7.1 Local State

成功 State 当前保存：

```text
Root File
Mirror Nested File
```

对应：

```text
SHA256
Size
```

## 7.2 增量模式

默认：

```toml
ftp_incremental_mirror = true
```

跳过条件：

```text
无 Pending
非 --full
Local State Entry 相同
Remote Path 仍存在
非 Adoption
```

明确不证明：

```text
Remote Existing Content == Local State Hash
```

## 7.3 强模式

以下任一条件会上传所有当前 Hybrid 文件：

```text
ftp_incremental_mirror = false
--full
Pending Resume
```

## 7.4 Remote Plan

Remote Plan 明确显示：

```text
FTP MIRROR MODE: LOCAL-STATE INCREMENTAL
REMOTE CONTENT HASH: NOT VERIFIED
```

或：

```text
FTP MIRROR MODE: STRONG
REMOTE CONTENT HASH: REPUBLISH ALL CURRENT FILES
```

## 7.5 Final Verify

默认：

```toml
ftp_verify_final = false
```

证明：

```text
Stage STOR
Stage RETR SHA256
Rename Replace
Stage Missing
Final Type = File
```

可选：

```toml
ftp_verify_final = true
```

增加：

```text
Final RETR SHA256
```

## 7.6 元数据

Pending / Ownership 继续使用：

```text
Stage RETR
Rename
Final RETR
SHA256
```

## 7.7 结论

> **Mirror 与 Content Proof 已从隐式优化变成清晰、可审阅的产品契约。**

默认增量模式仍应在发生面板修改、服务器回滚或手工 FTP 后通过 `--full` 重新收敛。

---

# 8. 阶段进度

## 8.1 阶段

```text
STAGE
VERIFY
MKDIR
PUBLISH
PENDING
PRUNE
OWNERSHIP
STATE
CLEANUP
```

## 8.2 非 TTY

高频 Detail 更新：

```text
约 2 秒采样
```

`force=True` 在非 TTY 中不会退化成每文件一行。

## 8.3 Cleanup

成功：

```text
CLEANUP 1/1 100%
```

失败：

```text
CLEANUP PENDING
```

并保留 Pending Marker。

## 8.4 DELETE/RMD

通过 Reporter `note()` 输出，避免与 TTY 动态行交错。

## 8.5 结论

> **上一轮 Progress UX P2 已关闭。**

---

# 9. P0：Bash Completion Target Command Injection

## 9.1 影响

```text
严重性：P0
类型：Local Arbitrary Command Execution
触发：加载静态 Bash Completion 后，在含恶意 deploy.toml 的目录按 Tab
远端访问：不需要
密码：不需要
配置通过完整校验：不需要
```

## 9.2 Target 名称校验

当前 Target Name 只拒绝：

- 空名称；
- `/`；
- `\`；
- `.`；
- `..`；
- CLI 保留字。

仍允许：

```text
$
()
`
space
;
>
<
*
?
leading -
control characters
```

## 9.3 Completion 绕过完整配置校验

Target Completion 不调用 `load_config()`。

它直接：

```text
tomllib.load
raw["targets"].keys()
```

并返回所有非空字符串 Key。

所以即使未来完整 Config Parser 拒绝某个名称，Completion Helper 若未同步过滤，仍然会返回它。

## 9.4 Bash Script

静态脚本：

```bash
targets=$(
  git-deploy completion targets |
  tr '\n' ' '
)
```

随后：

```bash
compgen -W "$actions $targets"
```

或：

```bash
compgen -W "$targets"
```

## 9.5 Bash 行为

`compgen -W` 会对 Word List 进行 Shell Expansion。

隔离验证：

```bash
targets='$(touch completion-proof)'
compgen -W "$targets" -- ''
```

结果：

```text
completion-proof 文件被创建
```

## 9.6 可触发配置

TOML 允许：

```toml
[targets."$(touch completion-proof)"]
```

该名称也能通过当前 `_parse_targets()`。

## 9.7 触发路径

```text
用户进入项目目录
    ↓
按 git-deploy <Tab>
    ↓
静态 Completion 调用 completion targets
    ↓
读取原始 TOML Key
    ↓
compgen -W 再展开
    ↓
本地命令执行
```

## 9.8 为什么 Argcomplete 不能兜底

项目同时提供：

- Argcomplete；
- 静态 Bash Script。

显式 `completion install` 安装并 Source 的是静态 Script，漏洞路径实际可达。

## 9.9 必须修复

### A. Bash 不使用 `compgen -W` 处理动态 Target

推荐：

```bash
local -a targets=()
mapfile -t targets < <(
  "${target_cmd[@]}" completion targets 2>/dev/null
)

local candidate
for candidate in "${targets[@]}"; do
  if [[ "$candidate" == "$cur"* ]]; then
    COMPREPLY+=("$candidate")
  fi
done
```

固定 Actions/Flags 可以继续使用静态常量，但动态数据不得进入会再展开的 Word List。

### B. 收紧 Target Name

统一：

```text
^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$
```

并继续拒绝 CLI Reserved Names。

### C. Completion Raw Reader 同步过滤

`_target_keys_from_project()` 不能只判断 `key.strip()`。

必须使用与 Config Parser 完全相同的：

```python
validate_target_name()
```

### D. Zsh 改为真正数组

避免：

```zsh
targets="..."
t=(${=targets})
```

推荐：

```zsh
targets=("${(@f)$("${target_cmd[@]}" completion targets 2>/dev/null)}")
```

并直接把数组交给 `_describe`。

### E. 安全回归测试

使用无害 Marker：

```text
$(touch completion-proof)
`touch completion-proof-2`
```

运行真实 Bash Completion Function，确认：

```text
Marker 不存在
Completion 不执行任何命令
```

## 9.10 发布建议

如果 v1.8.0 尚未分发：

```text
阻止 Release Asset 发布
修复后重新打 Tag
```

如果已经分发：

```text
尽快发布 v1.8.1
并建议已启用 Bash Completion 的用户更新脚本
```

---

# 10. P1：并行 Pool 建立会吞掉 Ctrl+C

## 10.1 当前代码

Sibling Session 建立：

```python
try:
    sibling = _open_ftp_hybrid_worker(primary)
except BaseException as exc:
    print("WARNING ...")
    break
```

## 10.2 问题

`BaseException` 包含：

```text
KeyboardInterrupt
SystemExit
GeneratorExit
```

所以用户在追加 FTP Session 建连期间按 Ctrl+C：

```text
KeyboardInterrupt
    ↓
被解释为普通 Session Connect Failure
    ↓
输出降级 Warning
    ↓
继续启动现有 Pool
    ↓
执行 Stage/Publish
```

## 10.3 边界

Pool 预建立本来保证：

```text
Ctrl+C 前 Remote Job = 0
```

但吞掉 KeyboardInterrupt 后，程序主动越过该边界开始远端工作。

## 10.4 修复

最小：

```python
except Exception as exc:
```

更推荐只捕获预期连接错误：

```python
except DeployError as exc:
```

然后在任意 `BaseException` 下：

```text
关闭已经打开的 Sibling
重新抛出
Remote Job = 0
```

## 10.5 测试

### First Sibling

```text
_open_ftp_hybrid_worker → KeyboardInterrupt
Job Count = 0
KeyboardInterrupt 向外传播
```

### Later Sibling

```text
Sibling 1 已打开
Sibling 2 → KeyboardInterrupt
Sibling 1 被关闭
Job Count = 0
KeyboardInterrupt 向外传播
```

---

# 11. P2：显式 Completion Install 会替换 RC 符号链接

## 11.1 当前 Atomic Write

```text
mkstemp
write
fsync
chmod
os.replace(tmp, ~/.bashrc)
```

## 11.2 符号链接行为

如果：

```text
~/.bashrc -> ~/dotfiles/bashrc
```

`path.is_file()` 会跟随 Symlink 读取真实内容。

但：

```python
os.replace(tmp_path, rc_path)
```

会替换 `~/.bashrc` 这个 Symlink 本身。

结果：

```text
~/.bashrc 变成普通文件
~/dotfiles/bashrc 保持原样
```

用户的 Dotfiles Symlink 被静默断开。

## 11.3 独立验证

本轮使用最小 `os.replace` 模型验证：

```text
before: .bashrc is symlink
after:  .bashrc is regular file
target: 原文件未修改
```

## 11.4 修复选择

### A. 显式跟随 Symlink

```python
destination = path.resolve(strict=True) if path.is_symlink() else path
```

对真实 Target 执行 Atomic Write，同时保留 Symlink。

### B. 保守拒绝

检测到 Symlink：

```text
ConfigError:
RC is a symlink; add the completion block manually
```

对于个人工具，B 更简单、风险更低。

## 11.5 测试

- Bash RC Symlink；
- Zsh RC Symlink；
- Dangling Symlink；
- Symlink 指向 Home 外；
- Mode 保留；
- Target 内容更新但 Link 不变。

---

# 12. P2：FILES_PUBLISHED 的 Plan 与 Executor 仍不一致

## 12.1 Planner

`FILES_PUBLISHED` 不属于：

```text
PRUNED
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

因此：

```text
publish_needed = true
pending != None
force_hybrid_publish = true
```

Remote Plan 会显示全部 Hybrid Upload。

## 12.2 Executor

只有：

```text
phase == PREPARED
```

才执行 Stage/Publish。

`FILES_PUBLISHED` 会直接进入 Prune。

## 12.3 结果

用户审阅的 Plan 包含实际不会执行的 Upload。

在单发布器、远端未被修改的边界内，Marker 已证明文件发布完成，因此不会造成正常部署错误。

但如果文件在 Pending 期间消失：

```text
Plan 显示会 Upload
Executor 实际不 Upload
继续 Prune / Ownership
```

## 12.4 修复选择

### A. 信任 Marker

```text
FILES_PUBLISHED:
    Plan 不显示 Upload
    验证当前文件/目录存在
```

### B. 重新发布 Hybrid

```text
不重放普通 Source/Incremental
重新 Stage/Publish 当前 Hybrid
再 Prune
```

此前产品方向更适合 B：重跑自然向前收敛。

---

# 13. P2：Completion Script 更新策略

普通 CLI 的 Script-only Ensure 当前逻辑：

```text
如果 Script 已存在
    → 不检查内容
    → 不按 Package Version 更新
```

因此未来升级后，旧静态 Script 可能长期保留。

当前 v1.7.3 正式版本没有 Completion，因此 v1.8.0 首次稳定迁移影响有限；但后续版本会出现。

建议：

- Script Body Hash 比较；
- 内容不同则原子更新 Script；
- 仍然不自动改 RC；
- 更新时打印一次简短 Note。

---

# 14. P3：Completion 与平台边界

`completion.py` 顶层导入：

```python
fcntl
```

CLI 又无条件导入 Completion 模块。

这意味着 Native Windows Python 无法导入 CLI。

当前用户主环境是 WSL2/Linux，TargetLock 也已有 Unix `flock` 语义，因此不是本轮阻断；但文档应明确：

```text
Supported local runtime: Linux / WSL / macOS-like Unix
```

---

# 15. 上一轮整改关闭矩阵

| 上一轮问题 | v1.8.0 状态 |
|---|---|
| Pending Schema 2 Hash 漂移 | 已关闭 |
| Mirror Nested State 保存 | 已实现 |
| PREPARED 兼容 | 已覆盖 |
| FILES_PUBLISHED 兼容 | 已覆盖 |
| PRUNED 兼容 | 已覆盖 |
| 默认 4 条连接 | 已关闭，默认 1 |
| Pool 启动前完整建连 | 已实现 |
| Sibling Connect 降级 | 已实现 |
| 自动修改 RC | 已关闭 |
| RC 非原子写 | 已关闭 |
| Shell Path Quote | 已关闭 |
| Zsh `#compdef` | 已关闭 |
| Mirror 契约不透明 | 已关闭 |
| Final Verify 不可选 | 已关闭 |
| 非 TTY 阶段日志膨胀 | 已关闭 |
| Cleanup 失败显示 100% | 已关闭 |
| Version 仍为 1.7.3 | 已关闭 |
| Bash Dynamic Completion 安全 | **未关闭，新 P0** |
| Ctrl+C Pool Setup | **未关闭，新 P1** |

---

# 16. v1.8.1 原子整改计划

## P0：Bash Completion

### TODO-001

- [ ] 动态 Target 不再进入 `compgen -W`；
- [ ] 使用 `mapfile` / 安全数组；
- [ ] 不执行 Target 文本中的任何 Shell Expansion；
- [ ] Zsh 同步使用换行数组；
- [ ] 真实 Bash 子进程测试。

### TODO-002

- [ ] 建立统一 `validate_target_name()`；
- [ ] Config Parser 使用；
- [ ] Completion Raw Reader 使用；
- [ ] Bootstrap/Workspace 继续兼容；
- [ ] 拒绝以 `-` 开头；
- [ ] 拒绝空白和控制字符；
- [ ] 拒绝 `$()`、反引号和 Shell 元字符。

### TODO-003

- [ ] 对恶意名称做无害 Marker 测试；
- [ ] `completion targets` 不输出非法 Key；
- [ ] Argcomplete 不输出非法 Key；
- [ ] Bash 静态 Completion 不执行 Marker；
- [ ] Zsh 静态 Completion 不执行 Marker。

---

## P1：Interrupt

### TODO-101

- [ ] Pool 降级只捕获 `DeployError`；
- [ ] `KeyboardInterrupt` 向外传播；
- [ ] `SystemExit` 向外传播；
- [ ] 已打开 Sibling 在异常时关闭；
- [ ] Job Count 保持 0。

---

## P2：RC Symlink

### TODO-201

- [ ] 检测 RC Symlink；
- [ ] 选择 Follow 或 Refuse 策略；
- [ ] 不替换 Link 本身；
- [ ] Dangling Link 明确报错；
- [ ] 测试 Dotfiles 场景。

---

## P2：Pending Plan

### TODO-301

- [ ] `FILES_PUBLISHED` Plan 与 Executor 对齐；
- [ ] 选择 Trust+Verify 或 Republish；
- [ ] 缺失当前文件时 Fail Closed / 重传；
- [ ] Plan 不显示不会执行的动作。

---

## Release Gate

### TODO-401

- [ ] Version `1.8.1`；
- [ ] Release Notes；
- [ ] Tag；
- [ ] Python 3.11；
- [ ] Python 3.12；
- [ ] Full Pytest；
- [ ] Ruff；
- [ ] ty；
- [ ] Wheel/Sdist；
- [ ] Isolated Wheel；
- [ ] Bash Completion Security Test；
- [ ] Zsh Completion Test；
- [ ] GitHub Actions Run。

---

# 17. 修复后验收标准

1. `$(...)` Target 不出现在 Completion；
2. 即使通过 Mock 强制输入，Bash Completion 也不执行命令；
3. 反引号 Target 不执行；
4. Target 空格/控制字符在 Config Load 前拒绝；
5. Completion Raw TOML Reader 同样过滤；
6. Ctrl+C during first sibling connect 向外传播；
7. Ctrl+C during later sibling connect 关闭已开连接；
8. Ctrl+C 后 Remote Stage/Publish Job = 0；
9. RC Symlink 保持 Symlink；
10. Pending `FILES_PUBLISHED` Plan/Execute 一致；
11. v1.7.3 三类 Pending 继续恢复；
12. 默认 FTP Session = 1；
13. Incremental/Strong Mirror Plan 明示；
14. Cleanup Failure 显示 PENDING；
15. GitHub Actions 完整通过。

---

# 18. 当前使用建议

在 v1.8.1 前：

## 不启用静态 Bash Completion

不要执行：

```bash
git-deploy completion install
```

已启用时，可暂时从 `.bashrc` 移除：

```text
# >>> git-deploy shell completion >>>
...
# <<< git-deploy shell completion <<<
```

或将安装的 Bash Script 暂时移走。

Argcomplete 也应只在确认 Target 名称安全时使用。

## FTP 并行

继续建议：

```toml
[deploy]
ftp_connections = 1
```

实际服务器 Canary 后再增加。

## Mirror

有外部修改可能时：

```bash
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

或：

```toml
ftp_incremental_mirror = false
```

## Final Verify

关键文件：

```toml
ftp_verify_final = true
```

---

# 19. 最终结论

v1.8.0 对上一轮 FTP Hybrid 审计的整改质量总体较高：

```text
Pending 兼容
默认串行
Pool 预建立
安全降级
Completion 显式 RC 安装
Atomic Write
Mirror Contract
Final Verify
Progress Sampling
Version / Tag
```

均已形成真实代码闭环。

但 Bash 静态 Completion 将未经安全约束的本地 TOML Target 名称传入 `compgen -W`，能够在按 Tab 时执行命令；同时 FTP Pool Setup 会吞掉 Ctrl+C。

因此：

> **git-deploy v1.8.0 本轮代码审计不通过。**

建议：

> **停止分发当前 Completion Script，发布最小 v1.8.1：优先修复 Bash Completion 命令执行与 Pool Interrupt，再处理 RC Symlink 和 FILES_PUBLISHED Plan 对齐。**

FTP In-place Hybrid 的 Pending、Ownership、Prune 和 State 安全主链本轮未发现新的阻断回归。
