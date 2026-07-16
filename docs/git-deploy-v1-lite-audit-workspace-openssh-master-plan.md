# git-deploy v1-lite 最新代码审计、多仓 Workspace 与 Native OpenSSH 总方案

> 仓库：`howjc/git-deploy`
> 审计分支：`main`
> 最新审计提交：`4da20713b1fa9f38ccce4f353e7e9896ab97df3f`
> 当前版本：`v1.0.0`
> 文档日期：`2026-07-16`
> 总体结论：**v1-lite 重构方向正确；先完成 v1.0.1 安全修复，再落地 v1.1 Native OpenSSH，最后实现 v1.2 Thin Workspace。**

---

## 文档说明

本文合并以下两份方案：

1. v1-lite 最新代码审计与多仓部署设计；
2. Native OpenSSH / 1Password SSH Agent / WSL 支持方案。

统一后的主线顺序是：

```text
v1.0.1
修复当前代码审计发现的安全与正确性问题
    ↓
v1.1.0
补齐 Native OpenSSH、1Password/WSL、Target Lock、Git Common Dir
    ↓
v1.2.0
实现 Thin Workspace 与多仓共享 SSH Connection
```

---

## 1. 执行摘要

v1-lite 的破坏性重构总体是成功的。

当前版本已经从 v0.3 的状态化发布系统收敛为：

```text
Build
  ↓
Git / Output Plan
  ↓
FTP / SFTP Sync
  ↓
Lightweight State
```

主要优点：

- 日常部署只有一条命令；
- 配置模型明显简化；
- Build 在 Remote Connect 之前执行；
- Source 使用精确 Commit Blob；
- Output 在连接前冻结并复核 SHA256；
- 所有远端操作成功后才提交 State；
- 失败后直接重跑即可收敛；
- 不扫描、不删除未知远端文件；
- Rename 通过 `--no-renames` 正确降维为 Delete + Add；
- SFTP 使用临时文件和可恢复 Backup Swap；
- FTP 明确只提供基础文件同步语义；
- v0.3 复杂状态引擎已从主线移除。

PR #5 的发布记录声明：

- Python 3.11：58 项测试通过；
- Python 3.12：58 项测试通过；
- Ruff、ty、wheel/sdist 构建通过；
- 验证真实 FTP；
- 验证容器化 OpenSSH/SFTP；
- 验证 pnpm、Composer、PHP+Node 构建链；
- 验证重复部署、中断重试和 State 丢失后的 Full 重建。

但本轮静态审计发现两个会直接影响日常部署安全的 P0：

1. 配置的 Output 根目录如果构建后不存在，可能触发远端旧产物批量删除；
2. Source 和 Output 的完整路径归属没有全局校验，配置变化后可能轮流覆盖同一远端路径。

建议先修复这两个问题，再增加多仓 Workspace。

---

# 2. 当前多仓支持现状

## 2.1 当前不支持一个配置声明多个仓库

当前 `Config` 只有一份：

- `project_root`
- `source`
- `build`
- `outputs`
- `targets`

`project_root` 固定为 `deploy.toml` 所在目录。

因此当前产品模型是：

```text
一个 deploy.toml
    =
一个 Git 仓库
    +
多个远端 Target
```

不是：

```text
一个 deploy.toml
    =
多个 Git 仓库
```

---

## 2.2 当前 State 也是每仓独立

State 保存到当前仓库的 Git 元数据目录：

```text
.git/git-deploy/<target>.json
```

因此多个仓库天然拥有：

- 独立 Last Commit；
- 独立 Output Manifest；
- 独立 Target Fingerprint；
- 独立失败与重跑边界。

这是正确的，不应为了批量部署而改成共享大 State。

---

## 2.3 当前没有 `init` 命令

当前公开入口只有：

```text
git-deploy
git-deploy build
git-deploy doctor
```

因此“每个仓库下面各自 init”在当前版本中实际是：

1. 在每个仓库创建自己的 `deploy.toml`；
2. 在每个仓库执行一次 `git-deploy --dry-run`；
3. 执行首次部署。

未来可以增加 `git-deploy init` 生成模板，但它不是多仓支持的前置条件。

---

# 3. P0 审计问题

## P0-01：Output 根目录缺失可能删除远端旧产物

### 当前行为

Output 扫描逻辑：

```python
if not output.local.exists():
    continue
```

如果上次 State 中记录了：

```text
public/dist/app.js
public/dist/vendor.js
```

本次构建结束后 `dist/` 根目录不存在，当前扫描结果会变成空。

增量 Planner 随后会认为：

```text
上次存在
当前不存在
delete_removed = true
```

从而生成：

```text
DELETE public/dist/app.js
DELETE public/dist/vendor.js
```

### 实际风险

常见触发原因：

- `pnpm build` 命令配置错误但错误地返回 0；
- Build Step 未真正生成目标目录；
- Output 路径拼错；
- 构建脚本改了输出目录；
- Composer Vendor 路径被清理但安装未执行；
- 条件构建跳过了某个产物。

此时工具可能把“产物没有生成”误解为“用户主动删除了全部产物”。

对于前端项目，这可能直接删除线上全部静态资源。

### 修复建议

最简单可靠的规则：

> 配置过的 Output 根目录在 Build 后必须存在。

修改 `scan_outputs()`：

```python
if not output.local.exists():
    raise PlanError(
        f"configured output does not exist after build: {output.local}"
    )
```

不要立即增加复杂的 Optional Output 模型。

如果未来确有可选产物需求，再增加：

```toml
[[outputs]]
local = "optional-report"
remote = "reports"
required = false
```

但 `required = false` 时，根目录缺失应表示：

```text
本轮跳过该 Mapping
不上传
也不删除旧远端文件
```

不能解释为“清空远端”。

### 必须新增测试

- 上次 State 有 Output，本次根目录缺失；
- 根目录缺失时零 Remote Connect；
- 根目录缺失时零 Delete；
- 空目录存在时允许按配置决定是否删除旧文件；
- Output 路径拼错时失败；
- Build 成功但 Output 缺失时失败。

---

## P0-02：Source 与 Output 完整归属没有全局冲突检查

### 当前行为

Planner 只合并“本次发生变化的 Operation”。

如果同一个远端路径同时属于：

- Git Source；
- Build Output；

只有两者在同一次 Plan 中都发生变化时，才会检测到冲突。

### 风险场景

第一次部署只有 Source：

```text
public/app.js 由 Git Source 管理
```

后续修改配置：

```toml
[[outputs]]
local = "dist"
remote = "public"
```

此时 `dist/app.js` 也映射到：

```text
public/app.js
```

如果本次 Source 没变化、Output 有变化：

```text
只有 Output Upload Operation
没有 Source Operation
=> 不发生冲突
=> Output 覆盖 Source
```

下一次 Source 有变化、Output 未变化：

```text
只有 Source Upload Operation
=> Source 又覆盖 Output
```

远端同一路径的内容会根据“哪一边本次发生变化”而来回切换。

### 修复建议

每次 Plan 都构建完整所有权集合：

```python
source_owned_paths = {
    path
    for path in HEAD entries
    if is_source_managed(path)
}

output_owned_paths = set(current_output_manifest)

overlap = source_owned_paths & output_owned_paths
if overlap:
    raise PlanError(...)
```

冲突检查不能只基于当前 Operation。

同时在配置阶段拒绝嵌套 Output Mapping：

```text
public/assets
public/assets/js
```

因为删除归属会变得不明确。

### 必须新增测试

- Source 与 Output 路径相同但 Source 未变化；
- Source 与 Output 路径相同但 Output 未变化；
- 修改配置后产生新冲突；
- 两个 Output Mapping 嵌套；
- 两个 Output Mapping 当前未碰撞、未来可能碰撞；
- 冲突时零 Remote Connect。

---

# 4. P1 高优先级问题

## P1-01：Git Executable Mode 被接受但不会部署

Git Reader 接受：

```text
100644
100755
```

但 `UploadOperation` 不携带 Mode，Transport 的 `upload()` 也没有 Mode 参数。

结果是：

```text
Git 文件为 100755
SFTP 上传后由服务器 Umask 决定权限
工具仍返回成功
```

Shell Script、CLI、CGI 或可执行 Worker 可能无法运行。

### 简洁修复路线

短期 v1.0.1：

- 如果 Source Entry 是 `100755`，明确报错；
- 不要静默成功。

中期 v1.1：

- `UploadOperation` 增加 `executable: bool`；
- SFTP 临时文件上传后执行 `chmod 0755/0644`；
- FTP 无法可靠处理时明确拒绝或警告；
- Output 如果需要 Mode，可在 Mapping 中显式配置，不自动扩展复杂 POSIX 模型。

---

## P1-02：没有 Target 级进程锁

两个终端同时执行：

```bash
git-deploy prod --yes
```

可能出现：

- 两个 Build 同时运行；
- 两个 Plan 基于同一个旧 State；
- 远端 Operation 交错；
- 后完成的进程覆盖 State；
- State 与远端内容对应不同 Plan。

不需要恢复 v0.3 的事务系统，只需一个简单文件锁：

```text
.git/git-deploy/<target>.lock
```

建议在 Build 前获取锁，整个部署结束后释放。

Linux/WSL 可使用：

```python
fcntl.flock()
```

锁冲突时直接提示：

```text
target prod is already being deployed by another process
```

---

## P1-03：State 使用 Per-Worktree Git Dir

当前 `git_dir()` 使用：

```text
git rev-parse --git-dir
```

在 Git Worktree 场景中，每个 Worktree 有独立 Git Dir。

这意味着同一仓库的不同 Worktree 会拥有不同部署 State。

对于经常使用 Worktree 的开发方式，可能出现：

- Worktree A 已部署；
- Worktree B 看不到 State；
- Worktree B 认为是首次部署；
- 触发 Full Upload；
- 两个 Worktree 都可能管理同一个 Target。

建议改用：

```text
git rev-parse --git-common-dir
```

将同一仓库的 Target State 和 Lock 放到 Common Git Dir。

State：

```text
<git-common-dir>/git-deploy/<target>.json
```

Lock：

```text
<git-common-dir>/git-deploy/<target>.lock
```

这样同一仓库的多个 Worktree 共享部署进度。

---

## P1-04：昂贵 Build 发生在廉价 State/Target 预检之前

当前顺序大致为：

```text
Git Validate
Dirty Check
Build
State Load
Target Resolve
Plan
```

如果：

- State 已损坏；
- Target Fingerprint 已变化；
- SSH Config 包含不支持的 ProxyJump；
- Target 名不存在；

用户可能先等待很长时间的 pnpm/Composer Build，然后才收到配置错误。

建议顺序：

```text
Load Config
Resolve Target
Validate Git
Load State
Validate State / Target Fingerprint
Check Build Command
Build
Scan Output
Create Final Plan
```

Build 前只做廉价、不依赖产物的 Preflight。

---

## P1-05：首次 Connect / Ensure Root 不参与 Retry

当前 Retry 只包裹单个 Upload/Delete。

最初的：

```python
transport.connect()
transport.ensure_root()
```

如果发生瞬时网络失败，会立即结束，不使用配置中的 `deploy.retries`。

建议统一一个连接重试函数：

```text
Connect
Authenticate
Ensure Root
```

也使用相同 Retry 配置。

---

## P1-06：FTP Missing Delete 判断依赖英文消息

FTP Delete 只将以下文本视为“不存在”：

```text
not found
no such file
does not exist
```

但常见 FTP Server 可能返回：

```text
550 File unavailable
550 Failed to delete file
550 Requested action not taken
```

不能把全部 `550` 当不存在，因为也可能是权限错误。

建议：

1. Delete 前先用 `SIZE`、`MLST` 或受控 `NLST` 检查；
2. 明确不存在则成功；
3. 存在但 Delete 返回 550，则报告权限错误。

---

## P1-07：Build 后没有再次检查工作区变化

Dirty Worktree 只在 Build 前检查。

如果 Build Step 修改了跟踪文件：

- Lockfile；
- Generated Source；
- Composer 文件；
- 版本文件；

Source 部署仍只使用 Commit HEAD，但用户不会看到 Build 后新产生的 Dirty Warning。

建议 Build 后再检查一次：

```text
Build created uncommitted tracked changes;
these changes are not included in source deployment
```

若 `require_clean_worktree = true`，Build 后变脏也应阻止部署。

---

# 5. P2 改进项

## P2-01：Doctor 会创建远端 Root

`doctor` 调用：

```text
connect
ensure_root
```

这意味着 Doctor 不是纯只读检查。

README 已经说明会尝试创建 Root，因此不是隐藏行为，但命令语义仍容易让人误解。

建议：

```bash
git-deploy doctor
```

默认只检查连接和 Root 是否存在。

显式使用：

```bash
git-deploy doctor --create-root
```

才创建目录。

---

## P2-02：没有配置初始化命令

建议增加：

```bash
git-deploy init
```

行为：

- 检查当前目录是 Git 仓库；
- 生成最小 `deploy.toml`；
- 不猜测服务器；
- 可检测 `package.json`、`pnpm-lock.yaml`、`composer.lock` 给出 Build Step 建议；
- 不自动写入密码；
- 不连接服务器。

这能降低单仓和多仓首次配置成本。

---

# 6. 当前多仓的立即可用方案

当前版本最合理的使用方式确实是：

> 每个仓库拥有自己的 `deploy.toml`，由外层脚本按顺序批量执行。

示例目录：

```text
workspace/
├── deploy-all.sh
├── api/
│   ├── .git/
│   └── deploy.toml
├── web/
│   ├── .git/
│   └── deploy.toml
└── admin/
    ├── .git/
    └── deploy.toml
```

脚本：

```bash
#!/usr/bin/env bash
set -Eeuo pipefail

target="${1:-dev}"
shift || true

repos=(
  "api"
  "web"
  "admin"
)

for repo in "${repos[@]}"; do
  printf '\n===== Deploy %s -> %s =====\n' "$repo" "$target"
  (
    cd "$repo"
    git-deploy "$target" --yes "$@"
  )
done
```

用法：

```bash
./deploy-all.sh dev
./deploy-all.sh prod
./deploy-all.sh prod --dry-run
./deploy-all.sh prod --skip-build
```

### 优点

- 无需修改 git-deploy；
- 每仓配置和 State 完全隔离；
- 某仓失败立即停止；
- 重跑时已成功仓库通常是 No-op；
- 操作顺序清晰；
- 非常符合 v1-lite 当前模型。

### 缺点

- Build/Plan/Deploy 是逐仓串行完成；
- 后面仓库 Build 失败时，前面仓库可能已经部署；
- 没有统一汇总；
- 仓库列表需要维护脚本；
- 多仓 Doctor/Dry-run 输出不够结构化。

因此脚本适合作为当前立即方案，但产品层可以增加一个极薄的 Workspace。

---

# 7. 推荐的多仓产品设计：Thin Workspace

## 7.1 设计原则

Workspace 只负责：

```text
发现仓库
确定顺序
统一 Target
批量 Preflight / Build / Plan
统一确认
顺序 Deploy
汇总结果
```

Workspace 不负责：

- 合并多个 Git 历史；
- 共享 Last Commit；
- 共享 Output Manifest；
- 跨仓 Transaction；
- 跨仓 Rollback；
- 依赖图调度；
- 分布式锁；
- 并行执行；
- 共享远端 Expected State。

每个仓库仍是一个完整独立的 v1-lite Project。

---

## 7.2 Workspace 配置

在多仓根目录增加：

```text
deploy.workspace.toml
```

最小格式：

```toml
default_target = "dev"

[[repositories]]
name = "api"
path = "api"

[[repositories]]
name = "web"
path = "web"

[[repositories]]
name = "admin"
path = "admin"
```

列表顺序就是部署顺序。

每个仓库内部继续保留：

```text
api/deploy.toml
web/deploy.toml
admin/deploy.toml
```

Workspace 不复制：

- Build Steps；
- Output Mapping；
- FTP/SFTP 凭据；
- Remote Root；
- Source Protect。

---

## 7.3 Target 命名约定

第一版要求所有仓库使用一致 Target 名：

```text
dev
test
prod
```

Workspace 执行：

```bash
git-deploy prod
```

会把 `prod` 传给每个仓库。

如果某仓没有 `prod`，在任何 Build 或 Remote Connect 之前失败。

不要在第一版增加复杂的 `target_map`。

统一 Target 名本身就是减少认知负荷的最佳实践。

---

## 7.4 自动模式识别

在当前目录：

### 存在 `deploy.toml`

进入单仓模式：

```bash
git-deploy prod
```

### 存在 `deploy.workspace.toml`

进入 Workspace 模式：

```bash
git-deploy prod
```

### 两个文件同时存在

直接报错，要求显式指定：

```bash
git-deploy --config deploy.toml prod
git-deploy --workspace deploy.workspace.toml prod
```

不要静默猜测。

这样用户仍然只记一条命令。

---

# 8. Workspace 执行流程

## Phase 1：全部 Preflight、Build 和 Plan

对每个仓库按顺序：

1. 加载仓库 `deploy.toml`；
2. 选择同名 Target；
3. 读取共享 Git Common State；
4. 获取该仓 Target Lock；
5. 验证 Git；
6. 验证 State；
7. 验证 Target Fingerprint；
8. 执行 Build；
9. 扫描 Outputs；
10. 创建 Plan；
11. 冻结 Upload 文件。

要求：

```text
所有仓库 Phase 1 成功前
Remote Connect = 0
Remote Writes = 0
```

这样不会发生：

```text
API 已上线
Web Build 才失败
```

---

## Phase 2：统一 Plan 和一次确认

输出：

```text
Workspace Target: prod

[api]
  2 uploads
  0 deletes

[web]
  12 uploads
  3 deletes

[admin]
  No changes

Total:
  14 uploads
  3 deletes
```

只确认一次：

```text
Deploy 3 repositories to prod? [y/N]
```

---

## Phase 3：顺序部署

按 Workspace 配置顺序部署。

每个仓库：

1. Connect；
2. Upload/Delete；
3. 全部成功后提交自己的 State；
4. 释放自己的 Lock；
5. 进入下一个仓库。

如果某仓失败：

```text
已成功仓库：State 已提交
失败仓库：State 不变
后续仓库：未执行
```

直接重跑 Workspace：

```bash
git-deploy prod --yes
```

已成功仓库会变成 No-op，失败仓库继续执行。

不需要 Workspace Global State 或跨仓 Recover。

---

# 9. Workspace 为什么默认不并行

第一版应坚持顺序执行。

原因：

- pnpm/Composer 并行容易争抢 CPU、内存和磁盘；
- 多个 SSH Agent 认证提示可能交错；
- 日志更难阅读；
- 同一服务器并发上传可能降低稳定性；
- 失败边界更复杂；
- 用户通常更看重稳定，而不是节省几十秒。

如果真实使用长期证明串行太慢，再增加：

```bash
git-deploy prod --jobs 2
```

但它不应进入 Workspace 首版。

---

# 10. Workspace 代码结构

不需要恢复 Application Layer。

只增加：

```text
src/git_deploy/
├── workspace.py
└── prepared.py
```

### `prepared.py`

将当前单仓 `_deploy()` 拆成：

```python
prepare_project(...) -> PreparedDeployment
execute_prepared(...) -> DeploymentResult
```

`PreparedDeployment` 包含：

- Config；
- Target；
- Frozen Plan；
- Frozen Upload Paths；
- State Store；
- Lock；
- Cleanup Context。

单仓和 Workspace 共用同一套函数。

### `workspace.py`

只负责：

- 读取 Workspace；
- 遍历 Repository；
- Prepare All；
- Render Combined Plan；
- Confirm Once；
- Execute Sequentially；
- Print Summary。

---

# 11. 多仓配置复用建议

不要在 Workspace 中共享大段 Target 配置。

使用以下方式减少重复：

## SFTP

所有仓库复用 SSH Alias：

```toml
[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/srv/api"
```

不同仓库通常只需要修改：

```text
remote_root
outputs
build
```

## FTP

复用相同环境变量名：

```toml
password_env = "DEPLOY_FTP_PASSWORD"
```

配置重复的几行是可以接受的。

不要立即增加：

- Target Include；
- TOML Inheritance；
- Template；
- Variable Interpolation；
- Workspace Shared Target Merge。

这些功能很容易让配置重新复杂化。

---

# 12. 推荐迭代顺序

## v1.0.1：安全修复

必须完成：

- Output 根目录缺失 Fail Closed；
- Source/Output 完整 Ownership Collision；
- Output Mapping Nested Collision；
- 对应测试。

建议同时完成：

- Initial Connect Retry；
- Build 后 Dirty Check；
- Doctor Missing Output Warning。

---

## v1.1.0：单仓可靠性

- Target Lock；
- Git Common Dir State；
- Executable Mode 明确处理；
- Doctor `--create-root`；
- `git-deploy init`。

---

## v1.2.0：Thin Workspace

- `deploy.workspace.toml`；
- 自动模式发现；
- Prepare All；
- Combined Plan；
- Confirm Once；
- Sequential Deploy；
- Summary；
- Workspace Doctor；
- Workspace Build；
- Workspace Dry-run。

不要在 v1.2 增加：

- 并行；
- Depends On；
- Target Map；
- Shared State；
- Global Rollback。

---

# 13. Workspace 验收标准

## 配置

- 每仓仍可独立执行；
- Workspace 只保存 Name/Path/Order；
- 同名 Target 约定清晰；
- 无运行时配置继承。

## 安全

- 任一仓 Build/Plan 失败时零 Remote Connect；
- Protected Path 仍由每仓配置控制；
- Source/Output Collision 在每仓 Prepare 阶段拒绝；
- 同仓同 Target 并发运行被 Lock 拒绝。

## 失败恢复

- 仓库 A 成功、B 失败、C 未执行；
- 重跑时 A No-op；
- B 继续；
- C 随后执行；
- 不需要 Workspace Recover。

## 使用体验

单仓：

```bash
git-deploy prod
```

多仓：

```bash
cd workspace
git-deploy prod
```

用户不需要学习另一套命令。

---

# 14. 最终建议

当前问题不应通过“在一个 deploy.toml 重新添加 projects 表”解决。

正确方向是：

```text
每仓一个独立 v1-lite 配置
        ↓
每仓一个独立轻量 State
        ↓
上层一个极薄 Workspace
        ↓
同一条 git-deploy 命令批量编排
```

立即使用：

```text
每仓 deploy.toml + deploy-all.sh
```

产品演进：

```text
v1.0.1 修安全边界
v1.1 补单仓锁和 Worktree State
v1.2 增加 Thin Workspace
```

这样既能满足多仓部署，又不会把 v1-lite 重新变成复杂发布平台。

---

# Native OpenSSH / 1Password SSH Agent / WSL 专项设计

> 本章节是前述单仓可靠性与 Thin Workspace 设计的认证和传输专项实现方案。
> 核心原则：**配置 SSH 主机别名时，git-deploy 完整委托给系统 OpenSSH。**

## 1. 背景

`git-deploy v1-lite` 的目标是：

> 一次配置，通过一条命令完成本地构建、增量计划和 FTP/SFTP 文件同步。

用户的本地开发环境主要运行在：

```text
Windows
  ↓
WSL
  ↓
git-deploy
  ↓
SSH / SFTP
```

用户已经完成 WSL 与 Windows 1Password SSH Agent 的接管配置。

当前直接执行：

```bash
ssh project-prod
```

时，WSL 中的系统 OpenSSH 会：

1. 读取 `~/.ssh/config`；
2. 匹配 `Host project-prod`；
3. 通过已经配置好的 Agent Socket 发起签名请求；
4. 唤起 Windows 1Password；
5. 通过 Windows Hello 或其他生物认证授权；
6. 完成 SSH 登录。

因此，`git-deploy` 不需要自己理解 1Password，也不应读取或管理私钥。

它只需要保证：

> 配置了 SSH 主机别名时，部署连接完整委托给 WSL 中的系统 OpenSSH。

---

## 2. 目标

### 2.1 北极星

用户只配置：

```toml
[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/www/wwwroot/project"
```

然后执行：

```bash
git-deploy prod
```

部署过程自动完成：

```text
git-deploy
    ↓
WSL /usr/bin/ssh
    ↓
~/.ssh/config
    ↓
WSL 已配置的 SSH Agent 接管
    ↓
Windows 1Password
    ↓
Windows Hello / 生物认证
    ↓
SFTP 文件上传
```

用户不需要额外配置：

```text
1Password Vault
Agent Socket
Private Key Path
Windows Hello
Biometric Provider
OpenSSH Identity Backend
```

这些都属于用户现有 SSH 环境的职责。

---

### 2.2 成功标准

单仓部署：

```bash
git-deploy prod
```

应满足：

- 自动读取 `ssh_host_alias`；
- 使用 WSL 内系统 `ssh`；
- 使用用户现有 `~/.ssh/config`；
- 自动触发 Windows 1Password；
- 自动触发生物认证；
- 一次部署只需要授权一次；
- 多文件上传不重复弹出生物认证；
- 私钥不进入 git-deploy；
- 部署成功后正确提交轻量 State。

多仓部署：

```bash
git-deploy prod
```

应满足：

- 多仓使用相同 SSH Alias 时共用一条 Master Connection；
- 一次生物认证；
- 多个仓库顺序部署；
- 每个仓库保持独立 State；
- 某仓失败后重跑可以收敛。

---

## 3. 当前实现与缺口

### 3.1 当前配置能力

当前 v1-lite 已支持：

```toml
[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/www/wwwroot/project"
```

并通过：

```bash
ssh -G project-prod
```

解析：

- HostName；
- User；
- Port；
- IdentityFile。

### 3.2 当前真正连接方仍是 Paramiko

现有流程是：

```text
ssh -G Alias
    ↓
解析 OpenSSH 配置
    ↓
Paramiko 建立 SSH 连接
```

这意味着：

- `ssh_host_alias` 目前只是配置解析来源；
- 真正执行认证的不是系统 `ssh`；
- OpenSSH 的完整行为没有保留；
- `ProxyJump` / `ProxyCommand` 当前会被拒绝；
- WSL 中已经配置好的 OpenSSH 与 1Password 链路不一定完整生效。

### 3.3 问题本质

该需求不应定义为：

> 让 Paramiko 兼容 1Password SSH Agent。

正确的职责拆分应是：

```text
git-deploy
  负责构建、差异计划和文件同步

OpenSSH
  负责 SSH Config、Alias、Proxy、Agent 和认证

1Password
  负责私钥保存和签名授权

Windows Hello
  负责生物认证
```

---

## 4. 核心设计决策

### 4.1 新增 Native OpenSSH SFTP Backend

新增：

```text
OpenSSHSFTPTransport
```

它不再通过 Paramiko 建立连接，而是调用当前运行环境中的：

```bash
ssh
sftp
```

在 WSL 中应解析为：

```text
/usr/bin/ssh
/usr/bin/sftp
```

### 4.2 自动后端选择

保持配置简单，不增加必填 Backend 字段。

推荐规则：

```text
配置 ssh_host_alias
        ↓
自动使用 Native OpenSSH Backend

只配置 host / username
        ↓
继续使用 Paramiko Backend
```

示例一：WSL + 1Password 推荐模式

```toml
[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/www/wwwroot/project"
```

示例二：普通 Paramiko 直连

```toml
[targets.legacy]
protocol = "sftp"
host = "192.0.2.20"
username = "deploy"
remote_root = "/srv/project"
key_file = "~/.ssh/id_ed25519"
```

### 4.3 不显式配置 1Password

禁止引入：

```toml
use_1password = true
use_windows_hello = true
agent_socket = "..."
biometric = true
```

理由：

- Agent 实现不属于 git-deploy；
- 用户未来可能切换到其他 SSH Agent；
- 1Password、GPG Agent、Windows OpenSSH Agent 应保持可互换；
- git-deploy 只依赖标准 OpenSSH 行为。

---

## 5. OpenSSH Config 示例

用户现有配置可以继续保持：

```sshconfig
Host project-prod
    HostName 192.0.2.10
    User deploy
    Port 22
    IdentityFile ~/.ssh/1password/project-prod.pub
    IdentitiesOnly yes
```

也可以继续使用：

```sshconfig
Include ~/.ssh/conf.d/*
```

以及：

```sshconfig
Host project-prod
    HostName 10.0.0.10
    User deploy
    ProxyJump gateway
```

Native OpenSSH Backend 不需要自己实现这些复杂逻辑，直接交给系统 OpenSSH。

---

## 6. 连接复用设计

### 6.1 为什么需要 ControlMaster

如果每个上传操作都单独调用一次 `sftp`，可能导致：

- 重复建立 SSH 连接；
- 重复触发 Agent 签名；
- 重复唤起 Windows Hello；
- 多文件部署体验很差。

因此部署开始时应建立一条 OpenSSH Master Connection。

### 6.2 建立 Master Connection

执行类似：

```bash
ssh   -o ControlMaster=yes   -o ControlPath=<control-socket>   -o ControlPersist=60   -MN project-prod
```

行为：

1. 系统 SSH 读取 `~/.ssh/config`；
2. 触发 1Password Agent；
3. 用户进行一次 Windows Hello；
4. Master Connection 保持；
5. 后续 SFTP 操作复用连接。

### 6.3 执行 SFTP 操作

通过：

```bash
sftp   -b -   -o ControlPath=<control-socket>   project-prod
```

传入 Batch 指令：

```text
mkdir /www/wwwroot/project/public
put /tmp/git-deploy/app.js /www/wwwroot/project/public/app.js.tmp
rename /www/wwwroot/project/public/app.js.tmp /www/wwwroot/project/public/app.js
```

删除：

```text
rm /www/wwwroot/project/public/old.js
```

### 6.4 关闭连接

部署完成或失败后，在 `finally` 中执行：

```bash
ssh   -o ControlPath=<control-socket>   -O exit   project-prod
```

随后删除 Control Socket 目录。

---

## 7. Control Socket 安全设计

### 7.1 存放位置

不使用共享固定路径：

```text
/tmp/git-deploy.sock
```

推荐放在仓库的共享 Git Common Dir：

```text
<git-common-dir>/git-deploy/ssh/
└── prod-<random-id>/
    └── control.sock
```

例如：

```text
.git/git-deploy/ssh/prod-a83f29/control.sock
```

### 7.2 权限

目录必须：

```text
0700
```

Socket 只允许当前用户访问。

### 7.3 生命周期

```text
创建随机私有目录
    ↓
建立 Master
    ↓
执行所有上传
    ↓
关闭 Master
    ↓
删除目录
```

异常退出时也应尽量清理。

---

## 8. WSL 兼容策略

### 8.1 使用当前环境中的 OpenSSH

通过：

```python
shutil.which("ssh")
shutil.which("sftp")
```

解析命令。

在 WSL 内应优先得到：

```text
/usr/bin/ssh
/usr/bin/sftp
```

不要自动调用 Windows 的 `ssh.exe`，因为用户已经在 WSL 中配置好了 Agent 接管流程。

### 8.2 不管理 Agent Socket

git-deploy 不主动设置：

```bash
SSH_AUTH_SOCK=...
```

直接继承当前 Shell 环境。

这意味着用户日常终端中能成功执行：

```bash
ssh project-prod
```

git-deploy 就应使用同一环境成功连接。

### 8.3 WSL 前置检查

```bash
which ssh
which sftp
echo "$SSH_AUTH_SOCK"
ssh project-prod
```

如果直接 SSH 已能唤起 Windows 1Password，git-deploy 才进入后续验收。

---

## 9. 交互模式

### 9.1 日常交互部署

```bash
git-deploy prod
```

不应默认传：

```text
BatchMode=yes
```

OpenSSH Master 建立过程应继承当前终端：

- stdin；
- stdout；
- stderr；
- TTY。

这样 Windows 1Password 和 Windows Hello 可以正常工作。

### 9.2 `--yes` 的含义

```bash
git-deploy prod --yes
```

只代表：

> 跳过 git-deploy 自己的部署确认。

它不代表：

- 禁止 SSH 认证交互；
- 禁止 1Password 生物认证；
- 强制 OpenSSH BatchMode。

### 9.3 非交互模式

未来可增加：

```bash
git-deploy prod --non-interactive
```

此时才传：

```text
BatchMode=yes
```

该能力不是 v1.1.0 首发必需项。

---

## 10. 不启用 Agent Forwarding

本需求仅需要本地 SSH Agent 用于本地登录远端，不需要将 Agent 转发到远端。

因此 git-deploy 不应自动启用：

```sshconfig
ForwardAgent yes
```

这会扩大安全边界，且与文件上传没有直接关系。

---

## 11. Transport 接口设计

现有抽象可以继续保留：

```python
connect()
ensure_root()
upload()
delete()
close()
```

建议目录：

```text
transports/
├── base.py
├── ftp.py
├── sftp.py
└── openssh_sftp.py
```

Factory：

```python
def create_transport(target):
    if target.protocol == "ftp":
        return FTPTransport(target)

    if target.protocol == "sftp" and target.ssh_host_alias:
        return OpenSSHSFTPTransport(target)

    return ParamikoSFTPTransport(target)
```

---

## 12. SFTP 上传语义

### 12.1 上传流程

```text
确保远端父目录
    ↓
上传临时文件
    ↓
Rename 临时文件到目标
```

### 12.2 Rename 覆盖兼容

不同 SFTP Server 对覆盖 Rename 的行为可能不同。

推荐：

1. 优先尝试直接 Rename；
2. 失败时进入 Backup Swap；
3. 先把旧目标 Rename 到 `.bak`；
4. 再发布临时文件；
5. 发布失败时恢复 `.bak`；
6. 发布成功后删除 `.bak`。

---

## 13. Doctor 设计

输出示例：

```text
[OK] Config: /workspace/project/deploy.toml
[OK] Git: HEAD abc123
[OK] SSH backend: Native OpenSSH
[OK] SSH executable: /usr/bin/ssh
[OK] SFTP executable: /usr/bin/sftp
[OK] SSH alias: project-prod
[OK] SSH endpoint: deploy@192.0.2.10:22
[OK] Authentication: connected
[OK] Remote root: /www/wwwroot/project
```

Doctor 可以显示：

```text
Authentication may trigger your configured SSH Agent or system biometric authorization.
```

不需要判断 Agent 是否来自 1Password。

建议：

```bash
git-deploy doctor prod
```

默认只检查连接和 Root 是否存在。

显式使用：

```bash
git-deploy doctor prod --create-root
```

才创建目录。

---

## 14. 多仓 Workspace 结合方案

### 14.1 相同 SSH Endpoint 复用连接

多个仓库可能配置相同：

```toml
ssh_host_alias = "project-prod"
```

但拥有不同：

```text
remote_root
```

例如：

```text
api   → /srv/project/api
web   → /srv/project/web
admin → /srv/project/admin
```

这些仓库可以复用同一条 SSH Master Connection。

### 14.2 Endpoint Key

连接池 Key 应至少包含：

```text
ssh executable
sftp executable
ssh_config_file
ssh_host_alias
effective host
effective user
effective port
```

不包含 `remote_root`。

### 14.3 Workspace 执行体验

```text
Prepare api
Prepare web
Prepare admin
    ↓
一次确认
    ↓
建立 project-prod Master
    ↓
一次 Windows Hello
    ↓
Deploy api
Deploy web
Deploy admin
    ↓
关闭 Master
```

最终体验：

```text
一条命令
一次生物认证
多个仓库完成部署
```

### 14.4 Workspace Connection Pool

建议增加：

```text
SSHConnectionPool
```

单仓部署可以使用临时 Pool，Workspace 可以在多个仓库间共享 Pool。

不要引入：

- 全局常驻 SSH Daemon；
- 跨进程连接池；
- 后台服务；
- 长期 ControlPersist。

连接只在当前部署命令生命周期内存在。

---

## 15. Target Lock 与 Git Worktree

Native OpenSSH 支持应与 Target Lock 一起落地。

锁文件：

```text
<git-common-dir>/git-deploy/<target>.lock
```

流程：

```text
获取 Target Lock
    ↓
Build
    ↓
Plan
    ↓
建立 SSH Master
    ↓
Deploy
    ↓
State Commit
    ↓
关闭 Master
    ↓
释放 Lock
```

State、Lock 和 SSH Socket Root 建议使用：

```bash
git rev-parse --git-common-dir
```

这样同一仓库的多个 Worktree 共享部署状态和互斥锁。

---

## 16. 错误处理

### 缺少系统 SSH

```text
Native OpenSSH backend requires the system 'ssh' executable.
```

### 缺少 SFTP

```text
Native OpenSSH backend requires the system 'sftp' executable.
```

### Alias 解析失败

```text
cannot resolve SSH alias 'project-prod' using ssh -G
```

### Master 建立失败

```text
OpenSSH authentication failed for target prod
```

应保留系统 SSH 原始 stderr，方便用户看到 Host Key、Agent、认证、Proxy 或网络错误。

### Batch SFTP 失败

错误应包含：

- Target；
- Remote Path；
- SFTP Exit Code；
- Stderr；
- 当前操作。

---

## 17. 日志与可观测性

普通模式：

```text
Target: prod
SSH: project-prod via Native OpenSSH
Authentication: waiting for SSH Agent authorization...
Connected.
UPLOAD public/app.js
UPLOAD public/dist/app.js
Deployment completed.
```

Verbose 模式：

```text
SSH executable: /usr/bin/ssh
SFTP executable: /usr/bin/sftp
Alias: project-prod
Resolved endpoint: deploy@192.0.2.10:22
```

不要输出：

- Agent Socket 详细内部信息；
- 私钥内容；
- 密码；
- 1Password Item URI；
- Windows 用户隐私信息。

---

## 18. 测试方案

### 18.1 单元测试

- Alias 自动选择 OpenSSH Backend；
- Host 直连选择 Paramiko；
- `ssh` 不存在；
- `sftp` 不存在；
- Control Socket 路径安全；
- Command 参数正确；
- stderr 错误映射；
- `close()` 幂等；
- Batch 指令转义；
- Remote Path 安全；
- Backup Swap；
- Cleanup。

### 18.2 Fake Process 测试

模拟：

```text
ssh -G
ssh -MN
sftp -b -
ssh -O exit
```

验证：

- 调用顺序；
- 环境继承；
- 退出码；
- 生物认证交互不被 BatchMode 阻断；
- Master 只建立一次；
- 多个文件共用 Connection。

### 18.3 真实 OpenSSH/SFTP 集成测试

使用容器化 OpenSSH Server，覆盖：

- Alias；
- OpenSSH Config；
- 非默认 Port；
- IdentityFile；
- ProxyJump；
- 上传；
- Delete；
- Rename；
- Backup Swap；
- ControlMaster；
- 多文件一次认证；
- 重试；
- 连接关闭。

### 18.4 WSL + 1Password 人工验收

#### 前置检查

```bash
which ssh
which sftp
echo "$SSH_AUTH_SOCK"
ssh project-prod
```

#### Doctor

```bash
git-deploy doctor prod
```

确认：

- 使用 `/usr/bin/ssh`；
- 使用 Alias；
- 唤起 Windows Hello；
- 成功连接；
- 只认证一次。

#### 正式部署

```bash
git-deploy prod --yes
```

确认：

- 构建完成后才连接；
- 触发一次生物认证；
- 多文件上传不重复认证；
- 上传成功；
- State 正确提交。

#### 重复部署

```bash
git-deploy prod --yes
```

确认：

- 无变化时不连接；
- 不触发生物认证；
- 正确显示 No Changes。

---

## 19. 多仓人工验收

三个仓库都使用：

```toml
ssh_host_alias = "project-prod"
```

执行：

```bash
git-deploy prod --yes
```

确认：

```text
一次 Windows Hello
api 部署成功
web 部署成功
admin 部署成功
```

失败场景：

```text
api 成功
web 失败
admin 未执行
```

重跑时：

```text
api No-op
web 继续
admin 执行
```

---

## 20. 推荐版本规划

### v1.0.1：安全修复

- Output 根目录缺失 Fail Closed；
- Source / Output 完整所有权冲突；
- Output Mapping 嵌套冲突；
- Initial Connect Retry；
- Build 后 Dirty Check。

### v1.1.0：Native OpenSSH / WSL / 1Password

- `OpenSSHSFTPTransport`；
- Alias 自动选择 Native OpenSSH；
- 系统 `ssh` / `sftp` 探测；
- ControlMaster；
- ControlPersist；
- Control Socket 安全目录；
- SFTP Batch；
- Backup Swap；
- ProxyJump / ProxyCommand；
- Doctor Native OpenSSH 检查；
- Target Lock；
- Git Common Dir State；
- WSL 人工验收文档。

### v1.2.0：Thin Workspace

- `deploy.workspace.toml`；
- Prepare All；
- Combined Plan；
- Confirm Once；
- Sequential Deploy；
- Shared SSH Connection Pool；
- 相同 Endpoint 一次认证；
- Workspace Doctor；
- Workspace Dry-run；
- Workspace Summary。

---

## 21. 原子 TODO

### A. Config 与 Backend

- [ ] 定义 SFTP Backend 选择规则
- [ ] Alias 自动使用 Native OpenSSH
- [ ] Host 直连保留 Paramiko
- [ ] 拒绝冲突配置
- [ ] 更新 Config Tests
- [ ] 更新 README

### B. OpenSSH 探测

- [ ] 检测 `ssh`
- [ ] 检测 `sftp`
- [ ] 记录绝对路径
- [ ] 缺失时结构化报错
- [ ] WSL 环境测试
- [ ] 不调用 Windows `ssh.exe`

### C. Master Connection

- [ ] 设计私有 Control Socket 目录
- [ ] 设置目录权限 0700
- [ ] 建立 Master
- [ ] 等待 Master Ready
- [ ] 支持认证交互
- [ ] 关闭 Master
- [ ] 异常清理
- [ ] Close 幂等

### D. SFTP Batch

- [ ] Batch Command Builder
- [ ] Remote Path 转义
- [ ] Parent Directory 创建
- [ ] Upload Temp
- [ ] Rename Publish
- [ ] Delete
- [ ] Error Mapping
- [ ] Progress 输出
- [ ] Retry

### E. Doctor

- [ ] 显示 Backend
- [ ] 显示 SSH 路径
- [ ] 显示 SFTP 路径
- [ ] 显示 Alias
- [ ] 显示 Resolved Endpoint
- [ ] 连接检查
- [ ] Root 检查
- [ ] `--create-root`
- [ ] Agent 授权提示

### F. Lock 与 Worktree

- [ ] 使用 Git Common Dir
- [ ] 迁移 State 路径
- [ ] Target Lock
- [ ] Lock Owner 信息
- [ ] Lock 冲突提示
- [ ] 多 Worktree 测试

### G. Workspace Connection Pool

- [ ] 定义 Endpoint Key
- [ ] Acquire
- [ ] Reuse
- [ ] Close All
- [ ] 相同 Alias 复用
- [ ] 不同 Alias 隔离
- [ ] 一次命令一次认证测试

### H. 文档与验收

- [ ] WSL 配置说明
- [ ] 1Password 前置说明
- [ ] OpenSSH Config 示例
- [ ] Windows Hello 验收
- [ ] 单仓验收
- [ ] 多仓验收
- [ ] 故障排查
- [ ] Security Boundary

---

## 22. 非目标

本方案不实现：

- 直接调用 1Password API；
- 读取 1Password Vault；
- 管理 SSH Private Key；
- 自动配置 WSL Agent Socket；
- 自动安装 1Password；
- 自动启用 Agent Forwarding；
- Windows 原生 SSH 客户端选择；
- 常驻 SSH 后台服务；
- 跨进程永久连接池；
- 多仓全局事务；
- 多仓自动回滚。

---

## 23. 最终结论

该需求的最佳实现不是继续增强 Paramiko，而是：

> 配置 SSH 主机别名时，git-deploy 完整委托给系统 OpenSSH。

最终职责边界：

```text
git-deploy
  构建、计划、文件同步、轻量 State

OpenSSH
  SSH Config、Host Alias、Proxy、Agent、连接复用

1Password
  私钥保存、签名授权

Windows Hello
  生物认证
```

最终用户体验：

```text
git-deploy prod
    ↓
自动使用 project-prod
    ↓
自动唤起 Windows 1Password
    ↓
一次 Windows Hello
    ↓
完成单仓或多仓部署
```

这既完整兼容 WSL 与 1Password SSH Agent，也符合 v1-lite 的核心原则：

> **工具保持简单，认证交给系统 SSH。**

---

# 统一版本路线图

## v1.0.1：安全与正确性修复

必须完成：

- Output 根目录缺失时 Fail Closed；
- Source / Output 完整路径所有权冲突；
- Output Mapping 嵌套冲突；
- Initial Connect / Ensure Root Retry；
- Build 后 Dirty Worktree 检查；
- 对应零远端连接、零删除回归测试。

## v1.1.0：单仓可靠性与 Native OpenSSH

必须完成：

- Target Lock；
- Git Common Dir State；
- Git Executable Mode 明确处理；
- `OpenSSHSFTPTransport`；
- `ssh_host_alias` 自动选择 Native OpenSSH；
- 系统 `ssh` / `sftp` 探测；
- ControlMaster / ControlPath；
- WSL 与 1Password SSH Agent 人工验收；
- Doctor 默认只读，创建 Root 需要显式参数；
- `git-deploy init`。

## v1.2.0：Thin Workspace

必须完成：

- `deploy.workspace.toml`；
- 自动单仓 / Workspace 发现；
- 所有仓库先 Prepare；
- Combined Plan；
- Confirm Once；
- Sequential Deploy；
- Workspace Doctor / Build / Dry-run；
- 相同 SSH Endpoint 复用 Master Connection；
- 一条命令、一次生物认证、多个仓库部署。

明确不做：

- 多仓全局事务；
- 多仓自动回滚；
- 全局共享 State；
- 依赖图调度；
- 默认并行；
- 常驻 SSH 后台服务；
- 运行时 TOML 继承系统。

---

# 统一实施优先级

## P0：发布阻断

- [x] Output 根目录缺失不得解释为远端全量删除
- [x] Source / Output 完整所有权必须全局校验
- [x] 嵌套 Output Mapping 必须拒绝
- [x] 冲突和缺失时必须零 Remote Connect

## P1：单仓可靠性

- [x] Initial Connect Retry
- [x] Build 后 Dirty Check
- [ ] Target Lock
- [ ] Git Common Dir State
- [ ] Executable Mode 明确处理
- [ ] Doctor 默认只读

## P1：OpenSSH / WSL / 1Password

- [ ] Alias 自动选择 Native OpenSSH
- [ ] `/usr/bin/ssh` 与 `/usr/bin/sftp` 探测
- [ ] ControlMaster 一次认证复用
- [ ] Control Socket 私有目录与清理
- [ ] SFTP Batch
- [ ] ProxyJump / ProxyCommand 交由 OpenSSH
- [ ] WSL / Windows Hello 人工验收

## P2：多仓 Workspace

- [ ] 每仓独立 `deploy.toml`
- [ ] Workspace 仅保存仓库 Path 与顺序
- [ ] Prepare All Before Connect
- [ ] Combined Plan
- [ ] Confirm Once
- [ ] Sequential Deploy
- [ ] Shared SSH Connection Pool
- [ ] 重跑自然收敛

---

# 最终产品边界

最终架构保持：

```text
每个仓库
  独立 Build
  独立 Git History
  独立 Output Manifest
  独立 Target State
  独立 Target Lock
        ↓
Thin Workspace
  发现仓库
  统一 Target
  Prepare All
  Confirm Once
  顺序部署
        ↓
Native OpenSSH
  SSH Config
  Host Alias
  Proxy
  Agent
  ControlMaster
        ↓
1Password / Windows Hello
  私钥与生物认证
```

一句话总结：

> **git-deploy 负责构建、计划与文件同步；Workspace 只做薄编排；认证完整交给系统 OpenSSH。**
