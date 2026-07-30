# git-deploy 最新代码深度审计报告

> 仓库：`howjc/git-deploy`  
> 分支：`main`  
> 最新提交：`25426abb283ebd008b5b597b933bd91d5f22cf00`  
> 上一稳定版本：`v1.7.3 / 442615a71f5cfd84027bd72c1c9b53970e8dbaf3`  
> 审计重点：FTP In-place Hybrid 优化、并行传输、阶段进度、Shell 自动补全  
> 审计日期：`2026-07-30`  
> 审计结论：**不通过——暂不建议将当前 main 作为稳定版本发布**  
> 建议目标版本：`v1.8.0`

---

# 1. 执行摘要

当前 `main` 在 `v1.7.3` 之后增加了多项用户可见能力：

```text
3426980  Shell Tab completion + 首次运行自动安装
e162095  FTP Hybrid Mirror 文件增量上传 + 移除业务文件 Final RETR
bafca30  FTP Hybrid Stage/Publish 多会话并行
25426ab  FTP Hybrid STAGE/VERIFY/PUBLISH/PRUNE 等阶段进度
```

FTP In-place Hybrid 的优化方向总体合理：

- Mirror 内文件 Hash 进入 Local State；
- 未变化且远端路径仍存在的文件可跳过上传；
- 孤儿文件和目录仍然依据 MLSD Typed Scan 清理；
- Stage STOR 后仍执行完整 RETR SHA256；
- Pending、Ownership 等关键元数据仍保留 Stage + Final 双校验；
- Stage/Publish 并行使用独立 FTP 控制会话；
- ProgressReporter 增加 RLock，支持并发回调；
- 新增阶段进度，解决上传完成后 RETR/Prune 阶段无反馈的问题；
- Shell Target 补全只读取本地 TOML，不连接远端、不读取密码。

但是，本轮发现三个发布阻断级问题：

## P1-01：旧版 FTP Pending 无法继续恢复

新版本把 Mirror 嵌套文件加入 `plan.output_manifest`，但：

```text
FTP_PENDING_SCHEMA 仍是 2
local_manifest_hash 算法标识没有升级
```

`local_manifest_hash()` 本来已经单独序列化了 Hybrid Root File 和 Mirror 文件树，现在又把新增的 Mirror State Entry 放入 `outputs`，导致同一份本地构建结果产生新的 Hash。

因此，使用 v1.7.3 创建的以下 Pending：

```text
PREPARED
FILES_PUBLISHED
PRUNED
```

在升级到当前代码后会触发：

```text
Pending Manifest does not match current local deployment view
```

而这些阶段又不能使用 `--recover`，只能依赖普通部署继续，因此可能被永久卡住，除非重新安装旧版本完成恢复或人工处理远端 Marker。

## P1-02：`ftp_connections` 默认值 4 是破坏性兼容变更

旧项目没有配置该字段时，当前代码会自动使用：

```toml
[deploy]
ftp_connections = 4
```

这意味着所有已通过单连接 Capability Probe 的 FTP Server，会在升级后默认同时建立 4 条控制连接。

很多共享主机、Pure-FTPd、面板托管 FTP 会限制：

```text
同账号并发连接数
同 IP 并发连接数
数据连接数
连接频率
```

当前 Capability Profile 没有证明多会话能力，代码也没有自适应降级。

更重要的是，Worker 0 在其他连接全部成功建立之前已经开始处理队列。若后续 Session 创建失败：

- Stage 阶段可能已部分上传；
- Publish 阶段可能已部分更新在线文件；
- Pending 仍保持 PREPARED；
- 下一次使用默认 4 路仍会重复失败。

状态机仍可恢复，不会直接 Prune，但这是明显的默认行为回归。

## P1-03：首次运行会静默、非原子地改写 `.bashrc/.zshrc`

每次真实 CLI 启动、参数解析之前都会执行：

```python
ensure_shell_completion_installed()
```

因此以下命令第一次运行时也可能修改用户 Shell 配置：

```bash
git-deploy --version
git-deploy --help
git-deploy prod --dry-run
git-deploy invalid-command
```

RC 文件当前使用：

```python
Path.write_text(...)
```

直接覆盖，没有：

- 同目录临时文件；
- `fsync`；
- 原子 `replace`；
- 文件锁；
- 备份；
- 原权限显式保留；
- 并发首次启动保护。

如果发生：

- 进程中断；
- 磁盘写满；
- 两个 git-deploy 同时首次运行；
- 用户正好同时编辑 RC；
- 文件系统 I/O 错误；

可能截断或覆盖用户的 `.bashrc/.zshrc`。自动安装函数会吞掉异常，用户甚至可能不知道 RC 已被部分修改。

---

# 2. 最新提交范围

## 2.1 Shell 自动补全

```text
3426980f637b7f2d7870957892c6fca52e0c348d
feat: add shell Tab completion with auto-install
```

实现：

- `argcomplete` 集成；
- 静态 Bash/Zsh 脚本；
- `git-deploy completion bash`；
- `git-deploy completion zsh`；
- `git-deploy completion targets`；
- `git-deploy completion install`；
- 首次真实 CLI 调用自动安装；
- 用户级 Script 与 RC Marker；
- 按 Package Version 记录安装状态。

## 2.2 Mirror 增量与校验优化

```text
e162095cfe227e2909683f3fdcdec1eb837282c6
perf(ftp-hybrid): incremental Mirror uploads and stage-only content verify
```

实现：

- Mirror 嵌套文件写入 Local State；
- Local State Hash 相同且远端路径存在时跳过；
- Stage RETR SHA256 保留；
- 业务文件 Final RETR 移除；
- Final 只验证 Stage 被消费、目标类型为 File。

## 2.3 FTP 多会话并行

```text
bafca3002894f31fab02321f905f1ac8d24769cb
perf(ftp-hybrid): parallel Stage/Publish over configurable FTP sessions
```

实现：

```toml
[deploy]
ftp_connections = 4
```

范围：

```text
1–16
```

并行阶段：

```text
STAGE
PUBLISH
```

仍然串行：

```text
普通 Source / Incremental
目录创建
Prune
Pending
Ownership
State
Cleanup
```

## 2.4 阶段进度

```text
25426abb283ebd008b5b597b933bd91d5f22cf00
feat(ftp-hybrid): show STAGE and wrap-up phase progress
```

增加：

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

---

# 3. 发布状态

## 3.1 当前版本仍为 v1.7.3

最新 `pyproject.toml`：

```toml
version = "1.7.3"
```

但代码已经增加：

- 新 CLI Action；
- 新依赖 `argcomplete`；
- 自动修改 Shell RC；
- 新配置项；
- 默认部署并行语义；
- FTP Mirror 行为变化；
- 校验策略变化。

当前代码不能继续以 `1.7.3` 发布或构建 Wheel。

否则会造成：

- 同版本不同内容；
- Wheel/uv/pip 缓存混乱；
- Release Asset 不可区分；
- `completion-install.version` 无法识别新实现；
- Changelog 仍指向 v1.7.3；
- 用户无法判断行为边界。

## 3.2 当前 main 比 v1.7.3 多 7 个提交

当前并不是 v1.7.3 Tag 的纯文档分支，而是包含完整功能开发。

建议正式版本：

```text
v1.8.0
```

不建议：

```text
v1.7.4
```

因为这不是补丁，而是显著的用户工作流和传输模型变化。

## 3.3 CI

以下新功能提交没有关联 GitHub Actions Workflow Run：

```text
3426980
e162095
bafca30
25426ab
```

Combined Status 也为空。

本轮审计环境尝试 Clone：

```bash
git clone https://github.com/howjc/git-deploy.git
```

仍然失败：

```text
Could not resolve host: github.com
```

因此本轮无法独立运行完整测试。

---

# 4. 已正确实现的部分

## 4.1 Mirror State 内容模型

新 `hybrid_content_manifest()` 将：

```text
Root File
Mirror Directory 内每个文件
```

都映射成：

```text
path → SHA256 + Size
```

例如：

```text
index.html
assets/app.js
assets/css/app.css
```

新的成功 State 能够描述完整 FTP Hybrid 文件内容。

## 4.2 Missing Remote 仍强制上传

即使 Local State Hash 未变，只要 MLSD Tree 中不存在对应文件：

```text
remote missing
    → upload
```

因此不会因为本地 State 存在而忽略明确缺失的文件。

## 4.3 Orphan 清理不依赖增量上传

无论文件是否需要上传，Planner 仍会递归扫描当前及历史 Ownership Directories，计算：

```text
Remote Files - Local Files
Remote Directories - Local Directories
```

所以：

- 老孤儿文件仍会删除；
- 空目录仍会清理；
- 整个历史目录仍可删除；
- 未知根目录仍不处理。

## 4.4 Pending 下仍强制完整发布

只要存在提交前 Pending：

```text
pending is not None
    → 所有当前 Root / Mirror 文件进入 uploads
```

因此一次中断不会因为 Local State Hash 相同而跳过需要恢复的文件。

## 4.5 Stage 内容证明仍保留

业务文件：

```text
STOR Stage
RETR Stage
Length
SHA256
```

仍然完整执行。

若 Publish 时 Stage 丢失，则：

```text
Restage
RETR
SHA256
Rename
```

## 4.6 元数据仍双重验证

以下关键事实仍通过：

```text
Stage RETR
Rename
Final RETR
Hash
```

发布：

- Pending；
- Ownership；
- Capability/内部小记录。

因此状态机事实没有因为业务文件优化而削弱。

## 4.7 并行 Session 隔离

每个 Worker 使用独立 `FTPTransport`：

- 独立 Control Connection；
- 独立 Passive Data Connection；
- 独立 Listing Cache；
- 独立 Retry/Reconnect；
- 独立 Close。

主连接不会由 Worker Close。

## 4.8 UTF-8/Banner 契约继承

Sibling Session 在连接前继承：

```text
_require_utf8
_required_server_banner_hash
```

新 Session 会继续执行：

- Banner 复核；
- FEAT UTF8；
- OPTS 或 Pure-FTPd always-on；
- 客户端 UTF-8 Encoding。

## 4.9 ProgressReporter 并发保护

上传统计、Retry、TTY 渲染和阶段计数都由：

```python
threading.RLock
```

保护。

当前没有发现明显的 Counter Data Race。

## 4.10 Completion Target 发现不接触远端

Target 补全只读取：

```text
deploy.toml
deploy.workspace.toml
成员仓 deploy.toml
```

不：

- 连接 FTP/SFTP；
- 读取密码环境变量；
- 执行 Build；
- 读取 Remote Ownership。

---

# 5. P1-01：Pending Manifest Hash 兼容性破坏

## 5.1 根因

`local_manifest_hash()` 当前已经独立包含：

```text
root_files
directories.files
directories.directories
```

同时又把 `plan.output_manifest` 放进：

```text
outputs
```

v1.7.3 的 `output_manifest` 只加入 Hybrid Root File。

新代码改为加入：

```text
Root File
Mirror 嵌套文件
```

因此对于同一份本地聚合目录：

```text
Hybrid Tree 部分不变
outputs 部分新增所有 Mirror 文件
Hash 改变
```

## 5.2 Schema 没有升级

当前仍是：

```python
FTP_PENDING_SCHEMA = 2
```

Pending 内没有：

```text
manifest_hash_version
```

也没有双算法迁移。

## 5.3 失败流程

v1.7.3 创建：

```text
Pending Phase = PREPARED
local_manifest_hash = old_hash
```

升级当前代码后：

```text
当前本地内容未变
HEAD 未变
Previous State 未变
Ownership 未变
```

但新 Planner 计算：

```text
manifest_hash = new_hash
```

随后：

```text
old_hash != new_hash
    → Pending Manifest does not match current local deployment view
```

同样影响：

```text
FILES_PUBLISHED
PRUNED
```

## 5.4 为什么无法用 `--recover`

显式 FTP Recovery 只支持：

```text
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

提交前三个 Phase 仍要求普通部署和冻结本地文件。

所以旧 Pending 会被卡住。

## 5.5 必须修复

推荐不改变现有 Pending Schema 2 的 Hash 语义。

将：

```python
local_manifest_hash(hybrid.local, plan.output_manifest)
```

改为传入：

```text
Incremental Output
+
历史算法原本包含的 Hybrid Root File
```

排除新增的 Mirror 嵌套 State Key。

这样：

- Pending Hash 与 v1.7.3 兼容；
- `next_state.outputs` 仍可保存完整 Mirror Hash；
- 新增增量功能不需要破坏恢复协议。

另一种方案：

```text
FTP_PENDING_SCHEMA = 3
manifest_hash_version = 2
```

但仍必须实现 Schema 2 旧 Hash 的验证兼容，不能只升级后 Fail Closed。

## 5.6 必须新增测试

使用真实 v1.7.3 Marker Fixture：

```text
PREPARED + Mirror Files
FILES_PUBLISHED + Mirror Files
PRUNED + Mirror Files
```

确认当前代码能够继续。

---

# 6. P1-02：默认 4 条 FTP Session 是破坏性变化

## 6.1 当前默认

```python
ftp_connections = 4
```

已有配置无需显式启用就会进入并行。

## 6.2 Capability Profile 没有证明并发能力

Bootstrap Probe 证明：

- 单 Session UTF-8；
- 单 Session STOR/RETR；
- 单 Session Rename；
- 单 Session Delete/RMD。

没有证明：

- 同一账号可同时登录 4 次；
- 同一 IP 可同时建立 4 条连接；
- 多 Session 看见同一文件系统；
- 多 Session 可以同时 STOR；
- 服务端没有连接频率限制。

## 6.3 Pool 启动顺序

代码先：

```text
启动 Primary Worker
```

然后逐个：

```text
连接 Sibling
启动 Sibling Worker
```

所以在建立完整 Pool 之前，Primary 已可能开始：

- Stage Upload；
- Publish Rename。

若第 2/3/4 个连接失败，当前阶段已可能发生部分远端变更。

## 6.4 状态安全

好消息：

- Stage 失败只影响内部 Stage；
- Publish 失败时仍处于 PREPARED；
- 尚未 Prune；
- Ownership/State 尚未推进；
- 下次 Pending 会强制重新发布。

所以不会直接导致孤儿删除或错误 State。

## 6.5 实际兼容问题

如果 Server 最大允许 1 或 2 条连接：

```text
每次默认部署都会失败
每次重跑仍然默认 4
```

用户必须知道手工增加：

```toml
[deploy]
ftp_connections = 1
```

这对旧版本可用的项目属于回归。

## 6.6 必须修复

首选：

```text
默认 = 1
并行 = 显式 Opt-in
```

用户确认实际服务器支持后再配置 4/8。

或者实现自适应：

1. 在启动任何 Worker 前建立全部 Sibling；
2. 创建失败则减少 Pool；
3. 最低降级到 Primary 单连接；
4. 输出明确 Warning；
5. 只有 Primary 也无法工作时失败。

不要在 Worker 已开始远端操作后才发现 Pool 无法建立。

## 6.7 测试

需要真实 FTP Server Fixture：

- Max Session = 1；
- Max Session = 2；
- Config = 4；
- 自动降级；
- Concurrent MKD；
- Worker Reconnect；
- Stage Partial Failure；
- Publish Partial Failure；
- Pending Resume；
- Passive；
- Active。

当前测试只证明线程能分发任务，并未证明真实 FTP 多会话兼容。

---

# 7. P1-03：自动补全安装可能破坏用户 RC

## 7.1 触发时机

真实 Process 调用时：

```python
if argv is None:
    ensure_shell_completion_installed()
    enable_argcomplete(parser)
```

发生在：

```text
parse_args 之前
CLI try/except 之前
```

## 7.2 自动副作用

只要 Package Version 变化，普通命令就会写：

```text
~/.local/share/.../git-deploy
~/.bashrc 或 ~/.zshrc
~/.config/git-deploy/completion-install.version
```

## 7.3 RC 写入不是原子的

当前：

```python
rc_path.write_text(updated)
```

可能先 Truncate，再写入。

一旦写入中途失败：

```text
原 RC 已经丢失
新 RC 可能只写了一部分
异常被自动安装入口吞掉
用户下一次启动 Shell 才发现
```

## 7.4 并发问题

两个 git-deploy Process 同时首次启动：

1. 两者读取同一旧 RC；
2. 两者各自在内存中生成新内容；
3. 两者直接覆盖；
4. 其中一个可能覆盖用户或另一个 Process 的更改。

没有 Lock 或 Compare-and-swap。

## 7.5 推荐策略

最安全：

```text
默认不自动改 RC
```

首次运行只安装 Completion Script，并输出一次：

```text
Run: git-deploy completion install
```

RC 修改保留为显式命令。

如果坚持自动安装，必须：

- 只在交互 TTY；
- CI/cron 默认跳过；
- 同目录临时文件；
- Flush + fsync；
- 保存原权限；
- 原子 `os.replace`；
- 用户级锁；
- 修改前再次确认原文件 Hash；
- 最好保留一次 `.bak`；
- 失败必须明确 Warning，不能完全吞掉。

## 7.6 显式安装异常

`completion install` 当前只把 `ValueError` 转换成 `ConfigError`。

以下异常会产生原始 Traceback：

```text
PermissionError
OSError
FileNotFoundError
UnicodeDecodeError
Disk full
```

应统一转换成可读 CLI Error。

---

# 8. P1 Release Gate：不能以 v1.7.3 发布

## 8.1 同版本不同内容

当前代码仍声明：

```text
1.7.3
```

但真实 v1.7.3 Tag 不包含这些功能。

这是严格的发布阻断。

## 8.2 Completion 状态也依赖版本

自动安装状态只保存：

```text
__version__
```

当实现发生变化但版本仍是 `1.7.3`：

- 已存在 Script 时不会比较新 Script 内容；
- 不会自动刷新；
- 用户可能长期使用旧 Completion；
- `--force` 才能修复。

## 8.3 建议版本

```text
v1.8.0
```

原因：

- 新 CLI Action；
- 新依赖；
- 新自动安装行为；
- 新配置；
- 新默认并行；
- FTP Mirror 语义变化；
- 校验边界变化。

---

# 9. P2：Mirror 不再保证每次内容收敛

## 9.1 当前跳过规则

文件满足以下条件时不上传：

```text
Local State Hash 与当前相同
远端文件名仍存在
非 Adoption
非 --full
没有 Pending
```

## 9.2 没有远端内容证明

远端 MLSD 只证明：

```text
文件存在
```

不证明：

```text
内容等于 Local State Hash
```

## 9.3 典型漂移

以下情况不会自动修复：

- 面板或人工改写受管文件；
- Server 从旧备份恢复；
- 文件被外部程序替换；
- 磁盘损坏但路径仍存在；
- Local State 从另一环境复制；
- FTP Server 返回成功后存储层后续发生变化。

下一次普通部署会跳过该文件。

## 9.4 与 Mirror 术语的冲突

过去 FTP Mirror 每次上传全部当前文件，因此在单次成功部署后：

```text
Remote Current File Content = Local
```

当前更准确的语义是：

```text
State-based Incremental Mirror
```

而不是强内容 Reconcile。

## 9.5 建议

至少增加明确配置：

```toml
[deploy]
ftp_incremental_mirror = false
```

建议默认：

```text
false = 强 Mirror
```

用户明确追求性能时开启：

```text
true
```

或者把当前行为写入 Plan：

```text
FTP MIRROR MODE: LOCAL-STATE INCREMENTAL
REMOTE CONTENT HASH: NOT VERIFIED
```

提供：

```bash
--full
```

作为人工强制收敛手段。

---

# 10. P2：移除 Final RETR 的边界

## 10.1 当前内容证明

```text
Stage STOR
Stage RETR SHA256
Rename Replace
Stage Missing
Final Type = File
```

## 10.2 不再证明

```text
Final File SHA256
```

## 10.3 为什么大多数情况下成立

同一文件系统内 Rename 通常不会改写文件内容，Capability Probe 也证明 Rename Replace 基本契约。

在以下边界内合理：

```text
单发布器
单 FTP Server
共享一致文件系统
无 Rename 后转换 Hook
无异步存储损坏
```

## 10.4 风险

无法检测：

- Rename 成功但最终内容异常；
- 多节点 FTP 后端差异；
- Server-side Hook 转换内容；
- Rename 后即时外部覆盖。

建议将其定位为：

```text
Stage-verified, rename-trusted
```

不要再称为：

```text
Final content verified
```

如需最强保证，可增加可选：

```toml
ftp_verify_final = true
```

---

# 11. P2：自动补全兼容性与 Shell 边界

## 11.1 Zsh 缺少 `#compdef`

静态 Zsh Script 没有：

```zsh
#compdef git-deploy
```

仅在加载时检测：

```zsh
if (( $+functions[compdef] )); then
  compdef ...
fi
```

如果 RC Block 在 `compinit` 之前加载：

- 当时没有 `compdef`；
- Script 不注册；
- 后续 `compinit` 也可能因为没有 `#compdef` Header 而不映射命令。

建议把第一行改成：

```zsh
#compdef git-deploy
```

并增加真正的 Zsh 子进程测试。

## 11.2 RC Path 没有 Shell Quote

RC 使用：

```bash
source "/home/...path..."
```

只用双引号拼接路径。

Home Path 若包含：

```text
"
$
`
$()
换行
反斜杠
```

可能造成语法错误或 Shell Expansion。

应使用 POSIX Shell Quote，例如：

```python
shlex.quote(str(path))
```

Zsh/Bash 分别生成安全片段。

## 11.3 Target Name 规则过宽

Target 目前只拒绝：

- 空；
- `/`；
- `\`；
- `.`；
- `..`；
- Reserved Action。

仍允许：

```text
空格
Tab
换行
引号
通配符
以 - 开头
```

静态 Bash/Zsh Completion 把 Target 列表转成空格分隔字符串，因此这些名称不能可靠补全。

建议 Target Name 统一为：

```text
[A-Za-z0-9._-]{1,64}
且不能以 -
```

与 Workspace Repository Name 规则保持一致。

## 11.4 Completion TOML 读取未复用完整校验

Completion 为了不阻断 Shell，直接读取 `[targets]` Key。

这是合理的 Fail-open 设计，但应至少过滤不满足 Target Name 规则的 Key，避免生成无法执行的补全项。

---

# 12. P2：阶段进度存在日志与状态误导

## 12.1 非 TTY 日志膨胀

Stage/Publish Worker 对每个文件调用：

```python
progress.update_phase(..., force=True)
```

`force=True` 在非 TTY 下也立即输出。

对于 10,000 个文件，可能额外输出：

```text
10,000 verify lines
10,000 rename lines
加上 Upload 完成行
```

这会：

- 放大 CI 日志；
- 降低终端性能；
- 使真正错误难以发现。

建议：

```text
force 只对 TTY 生效
非 TTY 按固定数量/时间采样
```

例如每：

```text
100 files
或 2 秒
```

输出一次阶段进度。

## 12.2 Cleanup 失败仍显示 100%

Cleanup 异常时当前执行：

```text
advance_phase(detail="pending")
finish_phase()
```

最终会显示：

```text
CLEANUP 1/1 100%
```

同时又打印：

```text
cleanup is pending
```

两者矛盾。

应显示：

```text
CLEANUP PENDING
```

或：

```text
CLEANUP FAILED
```

不要强行完成进度。

## 12.3 stdout/stderr 交错

阶段 Progress 使用 Reporter Stream，DELETE/RMD 仍直接 `print()`。

在 TTY 下可能出现：

```text
动态进度行
DELETE 输出
动态进度恢复
```

建议所有 FTP Hybrid 运行日志走统一 Reporter/Renderer。

---

# 13. P2：并行 Worker 的取消与启动模型

## 13.1 Worker 启动前未完成 Pool 建立

Primary 线程启动后才创建 Sibling。

推荐：

```text
先建立全部 Session
成功后再启动全部 Worker
```

这样：

- Pool 建立失败时 Remote Work = 0；
- 可以安全降级；
- Plan 与实际并行度一致。

## 13.2 KeyboardInterrupt

Thread 使用：

```python
daemon=True
```

主线程在 `join()` 中收到 KeyboardInterrupt 时，当前函数没有统一：

```text
stop.set()
join all
```

再退出。

外层会关闭 Transport，Daemon 最终随进程终止；Pending 能恢复，但远端可能出现更多难解释的瞬时错误。

建议在任意 `BaseException` 路径：

```text
stop.set
close worker transports
bounded join
重新抛出
```

## 13.3 多节点共享存储未证明

Capability Probe 只测试一个 FTP Session。

并行 Publish 可能由不同 Backend Node 执行。

如果 FTP Host 是负载均衡入口且后端存储不一致：

- Stage 在 Node A；
- Publish 在 Node B；
- Ownership 在 Primary Node；
- 最终不同节点内容不一致。

默认并行应避免把这一假设隐式加到所有旧 Target。

---

# 14. 测试覆盖评价

## 14.1 已新增

- Mirror Hash State；
- Mirror unchanged skip；
- Remote missing force upload；
- No Final RETR；
- Restage RETR；
- Parallel Worker Fan-out；
- Serial `ftp_connections=1`；
- Concurrent Progress Callback；
- Phase Progress；
- Completion Project/Workspace Targets；
- Completion Script Install；
- RC Marker Idempotence；
- Completion CLI Validation。

## 14.2 关键缺失

### Pending 迁移

- v1.7.3 PREPARED Fixture；
- v1.7.3 FILES_PUBLISHED Fixture；
- v1.7.3 PRUNED Fixture；
- Mirror 嵌套文件；
- 新代码继续恢复。

### 实际 FTP 并行

- Server Max Session 1；
- Server Max Session 2；
- Concurrent MKD；
- Passive；
- Active；
- Worker Reconnect；
- Pool Startup Failure；
- Partial Publish Resume；
- 多 Session Shared Namespace。

### Mirror 漂移

- Remote File 内容被改但路径存在；
- Server Backup Rollback；
- `--full` 强制恢复；
- 增量模式明确 Warning。

### Completion

- 非原子 RC 写故障；
- 两个进程并发安装；
- RC 权限保留；
- Home Path 含空格/引号/$；
- Zsh 在 compinit 前加载；
- Target 含空格/控制字符；
- Unwritable Home；
- Wheel 中静态脚本实际打包；
- Bash/Zsh 真正子进程 Tab 补全。

### Phase

- 10k 非 TTY 日志量；
- Cleanup Failure 状态；
- Parallel Error 后 Progress 状态。

---

# 15. v1.8.0 原子整改计划

## Phase 1：Pending 协议兼容

### TODO-001

- [ ] 固定旧 Schema 2 Manifest Hash 语义；
- [ ] 将 Mirror 嵌套 State 与 Pending Hash 输入解耦；
- [ ] 或增加 Manifest Hash Version；
- [ ] 支持旧 Hash 双算法验证；
- [ ] 增加三个提交前 Phase Fixture。

### 验收

```text
v1.7.3 Pending + 当前版本
    → 能继续
    → 不需要人工删除 Marker
```

---

## Phase 2：并行连接安全

### TODO-101

- [ ] 默认 `ftp_connections = 1`；
- [ ] 并行显式 Opt-in；
- [ ] 先建 Pool、后开 Worker；
- [ ] 连接不足时安全降级；
- [ ] 输出 Effective Connections；
- [ ] Pool Failure 不开始 Stage/Publish；
- [ ] KeyboardInterrupt 有界停止。

### 验收

```text
Max Session = 1
Config 未设置
    → 正常串行部署

Config = 4
Max Session = 2
    → 降级到 2 或明确 Fail Before Work
```

---

## Phase 3：Completion 安装安全

### TODO-201

- [ ] 取消普通 CLI 首次运行自动修改 RC；
- [ ] 或仅交互式一次确认；
- [ ] Script 与 RC 使用原子写；
- [ ] 用户级安装锁；
- [ ] fsync + replace；
- [ ] 保留 RC Mode；
- [ ] 可选备份；
- [ ] 错误显示 Warning；
- [ ] OSError 转 ConfigError。

### TODO-202

- [ ] Zsh 添加 `#compdef git-deploy`；
- [ ] 使用 Shell-safe Path Quote；
- [ ] Target Name 收紧；
- [ ] Static Completion 不使用不安全空格列表；
- [ ] 真实 Bash/Zsh 测试。

---

## Phase 4：Mirror Contract

### TODO-301

- [ ] 明确强 Mirror 与增量 Mirror；
- [ ] 默认策略经过产品决策；
- [ ] Plan 显示 Mode；
- [ ] 增量模式显示“不校验现有文件内容”；
- [ ] `--full` 文档化为强制收敛；
- [ ] 可选 Final RETR。

---

## Phase 5：Progress UX

### TODO-401

- [ ] 非 TTY 阶段进度采样；
- [ ] Cleanup Failure 不显示 100%；
- [ ] 统一 DELETE/RMD 输出；
- [ ] 失败时关闭当前 Phase；
- [ ] 10k 文件日志测试。

---

## Phase 6：Release Gate

### TODO-501

- [ ] Version bump `1.8.0`；
- [ ] `__version__`；
- [ ] uv.lock；
- [ ] Changelog；
- [ ] Release Notes；
- [ ] Tag；
- [ ] Python 3.11；
- [ ] Python 3.12；
- [ ] Full Pytest；
- [ ] Ruff；
- [ ] ty；
- [ ] Wheel/Sdist；
- [ ] Isolated Wheel；
- [ ] Completion Package Data；
- [ ] Real FTP Canary。

---

# 16. 修复后验收标准

1. 旧 v1.7.3 PREPARED Pending 可恢复；
2. 旧 FILES_PUBLISHED Pending 可恢复；
3. 旧 PRUNED Pending 可恢复；
4. 默认配置不会建立多条 FTP Session；
5. Session Limit 1 的服务器正常部署；
6. 并行 Pool 创建失败前远端业务变更为零；
7. 并行部分失败可重跑；
8. `.bashrc/.zshrc` 写入具备原子性；
9. 两个首次运行进程不会丢失 RC 内容；
10. `git-deploy --version` 不静默改 RC；
11. Zsh 无论 compinit 顺序都能注册；
12. 特殊路径不会生成无效 Shell 代码；
13. Remote Existing Content Drift 的产品语义明确；
14. 非 TTY 10k 文件日志可控；
15. Cleanup Failure 不显示成功；
16. Current Main 使用新版本号；
17. CI 全绿；
18. Wheel 中补全脚本可读取。

---

# 17. 当前代码的临时使用建议

在正式收口前，真实项目建议：

## 17.1 完成旧 Pending 后再升级

在 v1.7.3 下先确认：

```text
没有 FTP Pending
```

如果有：

```text
先用 v1.7.3 完成 Deploy/Recover
```

再切换当前代码。

## 17.2 显式禁用并行

```toml
[deploy]
ftp_connections = 1
```

等实际服务器完成并发 Canary 后再提高。

## 17.3 定期使用 `--full`

若存在：

- 面板修改；
- Server Restore；
- 手工 FTP；
- 文件内容漂移风险；

周期性执行：

```bash
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

## 17.4 暂停自动 RC 修改

在当前版本可设置：

```bash
export GIT_DEPLOY_SKIP_COMPLETION_INSTALL=1
```

然后显式运行：

```bash
git-deploy completion install
```

执行前备份：

```bash
cp ~/.bashrc ~/.bashrc.bak
# 或
cp ~/.zshrc ~/.zshrc.bak
```

## 17.5 不要以 1.7.3 构建发布

当前功能必须使用新的版本号和 Release Artifact。

---

# 18. 最终结论

FTP In-place Hybrid 的性能优化本身有价值：

```text
Mirror Incremental
Stage-only Content Verification
Parallel FTP Sessions
Phase Progress
```

自动补全也明显降低了日常使用成本。

但当前 main 同时改变了：

- Pending Hash 输入；
- Mirror 内容保证；
- FTP 默认并发；
- 业务文件校验边界；
- Shell RC；
- CLI 依赖和命令；
- 用户状态安装流程。

其中：

```text
旧 Pending 无法恢复
默认并发破坏旧服务器兼容
自动非原子改写 RC
版本仍停留在 1.7.3
```

属于稳定发布前必须处理的问题。

因此：

> **当前最新代码审计不通过。**

建议：

> **以 v1.8.0 做一次集中收口，不继续堆叠新功能；优先完成 Pending 迁移、并行默认兼容、Completion 原子安装和正式 Release Gate。**

SFTP Staged Hybrid、普通 FTP Incremental 与 v1.7.3 已有稳定能力本轮未发现对应阻断回归。
