# git-deploy v1.4.0 迭代方案：本地聚合与单 Hybrid Output

> 项目：`howjc/git-deploy`
> 建议版本：`v1.4.0`
> 方案状态：实现完成 / 发布就绪
> 核心能力：Local Aggregation + Single Hybrid Output + Remote Ownership Manifest
> 产品定位：个人使用、极简、稳定、失败后可重跑
> 更新时间：2026-07-17

---

# 1. 执行摘要

v1.4.0 解决一个已经在真实部署中出现的问题：

- 前端构建产物 `dist/` 不进入 Git；
- 当前 Output 删除逻辑依赖本地 `.git/git-deploy/<target>.json`；
- 本地 State 丢失、换机器部署、远端被其他方式修改后，工具可能失去旧前端文件的删除所有权；
- 前端产物直接铺到远端混合项目根目录；
- 远端根目录同时包含 `index.php`、`.env`、后端目录、运行时目录等不能删除的内容；
- 因此既不能清空整个根目录，也不能对整个根目录做普通 Mirror。

最终方案收敛为：

```text
多个前端构建结果
        ↓
本地聚合到 .deploy/frontend-root
        ↓
一个 Hybrid Output
        ↓
远端混合项目根目录
```

Hybrid 的固定语义：

```text
聚合目录下的直接子目录
    → Mirror 管理

聚合目录下的直接文件
    → Incremental Upload
    → Remote Ownership Delete

远端未被 Hybrid Ownership Manifest 声明拥有的内容
    → 永远不处理
```

示例：

```text
本地 .deploy/frontend-root/
├── index.html
├── index10.css
├── favicon.ico
├── assets/
├── images/
└── fonts/

远端 project-root/
├── index.php                   未受管，保留
├── .env                        受保护，保留
├── app/                        未受管，保留
├── runtime/                    受保护，保留
├── uploads/                    受保护，保留
├── index.html                  Hybrid 顶层文件
├── index10.css                 Hybrid 顶层文件
├── favicon.ico                 Hybrid 顶层文件
├── assets/                     Hybrid Mirror 目录
├── images/                     Hybrid Mirror 目录
└── fonts/                      Hybrid Mirror 目录
```

v1.4.0 不实现：

- 项目根目录整体 Mirror；
- 完整远端 Root Reconcile；
- 多个 Hybrid 共同管理同一个物理根目录；
- Hybrid 之间自动转移所有权；
- 通用远端扫描清理；
- Workspace 全局 Hook；
- 自动回滚；
- 发布事务。

---

# 2. 北极星目标

## 2.1 北极星

> 即使本地 State 丢失或换机器部署，也能安全清理远端旧前端产物，同时永远不删除 `index.php`、`.env`、后端目录和其他未知远端内容。

## 2.2 用户体验目标

用户仍然通过一条命令完成日常部署：

```bash
git-deploy prod --yes
```

首次启用和排查时使用：

```bash
git-deploy prod --dry-run
git-deploy prod --remote-plan
git-deploy prod --full
```

## 2.3 成功标准

部署成功后：

1. Hybrid 当前顶层目录与本地聚合目录完全一致；
2. 远端 Hybrid 历史拥有、但本地已删除的顶层目录会被移除；
3. Hybrid 当前顶层文件会正确上传；
4. 远端 Hybrid 历史拥有、但本地已删除的顶层文件会被移除；
5. `index.php`、`.env`、后端目录和未知内容不受影响；
6. 本地 State 丢失不影响远端 Hybrid 删除所有权；
7. 构建或聚合失败时远端连接数为零；
8. 任一远端操作失败时本地 State 不推进；
9. 相同部署命令重跑后自然收敛。

---

# 3. 核心决策

## 3.1 本地聚合

多个前端构建结果先在本地合并成一个最终部署视图：

```text
frontend/dist/
admin/dist/
mobile/dist/
        ↓
.deploy/frontend-root/
```

git-deploy 只处理最终聚合结果，不理解各前端子项目。

## 3.2 单个 Hybrid

同一个物理 Target Root 最多允许一个：

```toml
mode = "hybrid"
```

不允许两个 Hybrid 共同管理同一个根目录。

## 3.3 顶层目录 Mirror

聚合目录中的直接子目录：

```text
assets/
images/
fonts/
```

被视为完整所有权边界。

成功部署后，这些目录的内容必须等于本地当前目录。

## 3.4 顶层文件增量

聚合目录中的直接文件：

```text
index.html
index10.css
favicon.ico
```

使用 Hash 增量上传。

删除所有权不再只依赖本地 State，而依赖远端 Ownership Manifest。

## 3.5 未知远端内容永远不动

以下远端内容只要没有被 Ownership Manifest 声明拥有，就不能进入删除候选：

```text
index.php
.env
app/
runtime/
uploads/
vendor/
storage/
任意未知文件和目录
```

## 3.6 v1.4.0 仅支持 SFTP Hybrid

为了控制复杂度：

```text
SFTP
    支持 Hybrid

FTP
    配置 mode = "hybrid" 时拒绝
```

FTP Hybrid 需要递归 Upload + Prune，缺少可靠目录切换语义，暂不进入 v1.4.0。

---

# 4. 推荐配置

## 4.1 `.gitignore`

项目必须包含：

```gitignore
# git-deploy local aggregation artifacts
.deploy/
```

## 4.2 Build

```toml
[build]
steps = [
  "python scripts/aggregate_frontend_builds.py"
]
```

## 4.3 Hybrid Output

```toml
[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"
```

## 4.4 Target

```toml
[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/www/wwwroot/project"

after_deploy = [
  "sudo -n /usr/bin/systemctl reload nginx",
  "sudo -n /usr/bin/systemctl is-active --quiet nginx"
]
command_timeout = 120
```

## 4.5 完整示例

```toml
project_id = "howjc/example-project"
default_target = "prod"

[source]
include = ["**"]
exclude = [".deploy/**"]
protect = [".git-deploy/**"]
require_clean_worktree = false

[build]
steps = [
  "pnpm --dir frontend build",
  "pnpm --dir admin build",
  "python scripts/aggregate_frontend_builds.py"
]
timeout = 900

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/www/wwwroot/project"
after_deploy = [
  "sudo -n /usr/bin/systemctl reload nginx"
]
command_timeout = 120

[deploy]
retries = 3
retry_delay = 2
```

---

# 5. 本地聚合方案

## 5.1 推荐目录结构

```text
project/
├── .deploy/
│   └── frontend-root/
├── frontend/
│   └── dist/
├── admin/
│   └── dist/
├── scripts/
│   └── aggregate_frontend_builds.py
├── deploy.toml
└── .gitignore
```

## 5.2 聚合脚本职责

聚合脚本负责：

- 清空并重建 `.deploy/frontend-root`；
- 读取所有明确来源目录；
- 合并到最终部署视图；
- 检查重复相对路径；
- 检查文件与目录类型冲突；
- 检查输出路径穿越；
- 发现冲突立即失败；
- 不连接远端；
- 不修改 Git State。

## 5.3 Fail Closed

必须拒绝：

```text
frontend/dist/assets/app.js
admin/dist/assets/app.js
```

必须拒绝：

```text
项目 A 产生文件 assets
项目 B 产生目录 assets/
```

必须拒绝：

```text
符号链接逃逸
目标路径覆盖 .git-deploy
```

## 5.4 不内置前端框架聚合器

v1.4.0 Core 不负责：

- 自动查找 dist；
- 决定覆盖顺序；
- 合并 HTML；
- 重写 Asset URL；
- 合并 Vite/Webpack Manifest。

聚合属于项目可信 Build 逻辑。

---

# 6. `.deploy` 保护策略

## 6.1 默认 Source Exclude

建议默认增加：

```text
.deploy/**
```

即使有人误把 `.deploy` 提交进 Git，也不会作为 Source 上传。

## 6.2 强保护 `.git-deploy`

建议默认增加：

```text
.git-deploy/**
```

Source 和 Output 都不能覆盖内部 Ownership、Stage、Backup 与 Recovery 数据。

## 6.3 Git Ignore 检查

加载 Hybrid 时执行：

```bash
git check-ignore -q .deploy/frontend-root
```

未忽略时显示：

```text
WARNING:
hybrid output directory '.deploy/frontend-root'
is not ignored by Git; add '.deploy/' to .gitignore
```

若：

```text
source.require_clean_worktree = true
```

则未忽略 Hybrid Local Root 应直接阻断。

## 6.4 `git-deploy init`

初始化模板建议包含 `.deploy/`，但普通部署不自动修改 `.gitignore`。

---

# 7. Hybrid 本地扫描模型

## 7.1 只扫描直接子项

将 `local_root.iterdir()` 分为：

```text
root_files
root_directories
```

## 7.2 Root File

示例：

```text
index.html
index10.css
favicon.ico
```

记录：

```python
HybridRootFile(
    name="index.html",
    hash="...",
    size=1234,
)
```

## 7.3 Root Directory

示例：

```text
assets/
images/
fonts/
```

递归扫描完整 Manifest：

```python
HybridDirectory(
    name="assets",
    files={
        "app.js": ManifestEntry(...),
        "css/app.css": ManifestEntry(...),
    },
    total_size=...,
)
```

## 7.4 Symlink

第一版不支持本地或远端 Hybrid Symlink：

```text
hybrid output does not support symlinks
```

---

# 8. 远端 Ownership Manifest

## 8.1 路径

```text
<remote_root>/.git-deploy/hybrid/frontend-root.json
```

## 8.2 内容

```json
{
  "schema": 1,
  "project_id": "github.com/howjc/example-project",
  "mapping": "frontend-root",
  "remote": ".",
  "directories": [
    "assets",
    "fonts",
    "images"
  ],
  "root_files": [
    "favicon.ico",
    "index.html",
    "index10.css"
  ],
  "last_commit": "abc123",
  "updated_at": 1784265600
}
```

## 8.3 Manifest 作用

远端 Manifest 只回答：

> 哪些项目根目录直接子项明确属于该 Hybrid。

它不扫描或记录：

- `index.php`
- `.env`
- 后端目录内部
- uploads/runtime 内容
- 未知远端内容

## 8.4 身份校验

以下情况 Fail Closed：

- Schema 不支持；
- Project ID 不匹配；
- Mapping 不匹配；
- Remote 不匹配；
- JSON 损坏；
- Manifest 是符号链接；
- 文件过大或编码非法。

---

# 9. 首次接管

## 9.1 无 Manifest 且当前路径不存在

可直接首次部署并创建 Manifest。

## 9.2 无 Manifest 但当前同名路径存在

例如远端已有：

```text
assets/
index.html
```

普通部署必须拒绝：

```text
remote path 'assets' exists but is not owned by hybrid output 'frontend-root';
review the path and rerun with --full to adopt it
```

## 9.3 `--full` 接管

```bash
git-deploy prod --full
```

Plan 显示：

```text
ADOPT HYBRID OUTPUT OWNERSHIP

Mapping: frontend-root
Existing path: assets/
Existing path: index.html
```

只接管当前本地聚合结果中存在的同名路径。

未知路径仍不接管：

```text
index.php
.env
app/
runtime/
```

---

# 10. 路径所有权冲突

## 10.1 Hybrid 与 Git Source

如果 Hybrid Mirror 目录为：

```text
assets/
```

Git Source 中存在：

```text
assets/backend.php
```

必须拒绝。

## 10.2 Hybrid 与其他 Output

如果 Hybrid 拥有：

```text
assets/
```

另一个 Output 映射：

```text
assets/special.js
```

必须拒绝。

## 10.3 Hybrid Root File 与 Source

如果 Hybrid 顶层文件为：

```text
index.html
```

Git Source 也管理 `index.html`，必须拒绝。

## 10.4 Protected 路径

Hybrid 直接子项匹配以下任一规则时拒绝：

```text
.env
.git
.git-deploy
uploads
runtime
storage
storage/cert
source.protect 中的其他路径
```

---

# 11. 同一根目录只允许一个 Hybrid

## 11.1 规则

同一物理 Target：

```text
protocol + resolved host + username + port + remote_root
```

最多一个 Hybrid Mapping。

## 11.2 Workspace

如果 Workspace 中两个仓库的 Hybrid 指向相同物理 Target Root：

```text
Preflight 拒绝
Remote Write = 0
```

## 11.3 允许的多 Hybrid

只允许远端根目录完全分离，例如：

```text
/www/wwwroot/project/frontend
/www/wwwroot/project/admin
```

当前 `remote = "."` 的混合根目录场景只允许一个 Hybrid。

---

# 12. Plan 数据模型

建议增加高层操作：

```python
@dataclass(frozen=True, slots=True)
class HybridRootFileUpload:
    path: str
    local_path: Path
    hash: str
    size: int

@dataclass(frozen=True, slots=True)
class HybridRootFileDelete:
    path: str

@dataclass(frozen=True, slots=True)
class HybridDirectoryMirror:
    name: str
    local_root: Path
    manifest: dict[str, ManifestEntry]
    file_count: int
    total_size: int
    adopt: bool = False

@dataclass(frozen=True, slots=True)
class HybridDirectoryDelete:
    name: str
```

远端 Manifest 删除需要显式显示来源：

```text
DELETE [hybrid-owner] index9.css
DELETE [hybrid-owner] old-assets/
```

不能伪装成普通本地 State 删除。

---

# 13. Plan 生成流程

## 13.1 Local Prepare

```text
Load Config
Validate Git
Run Build
Validate Aggregation Root
Scan Source
Scan Incremental Outputs
Scan Hybrid Output
Freeze Upload Bytes
```

此阶段：

```text
Remote Connect = 0
```

## 13.2 Remote Ownership Preflight

Hybrid 存在时：

```text
Read Ownership Manifest
Inspect current/historical owned top-level paths
Validate Adoption
Merge Ownership Plan
```

不递归扫描整个远端项目根目录。

## 13.3 Full Plan

生成：

```text
Source Operations
Incremental Output Operations
Hybrid Root File Operations
Hybrid Directory Operations
Ownership Manifest Update
after_deploy Commands
```

---

# 14. Dry-run 与 Remote Plan

## 14.1 保持 `--dry-run` 零连接

```bash
git-deploy prod --dry-run
```

显示本地 Hybrid Manifest，但不读取远端 Ownership。

## 14.2 新增 `--remote-plan`

```bash
git-deploy prod --remote-plan
```

语义：

- 执行 Build；
- 冻结本地字节；
- 建立只读远端连接；
- 读取 Ownership Manifest；
- 显示完整 Adopt/Delete Plan；
- 不上传；
- 不删除；
- 不执行 after_deploy；
- 不写远端 Manifest；
- 不写本地 State。

## 14.3 普通部署

```bash
git-deploy prod
```

流程：

```text
Local Prepare
Remote Ownership Preflight
Full Plan
Confirm
Execute
```

Native OpenSSH 复用同一个 ControlMaster，通常只触发一次身份认证。

---

# 15. SFTP 顶层目录 Mirror

## 15.1 内部路径

```text
<remote_root>/.git-deploy/
├── hybrid/
├── stage/
├── backup/
└── recovery/
```

## 15.2 Stage + Swap

对于 `assets/`：

```text
1. 上传完整目录到 .git-deploy/stage/<id>/assets
2. 现有 assets → .git-deploy/backup/<id>/assets
3. stage/assets → assets
4. 失败时恢复 backup/assets → assets
5. 成功后删除 Backup
```

## 15.3 目录删除

历史 Manifest 拥有但本地已经消失：

```text
old-assets/
```

先移动到 Backup，再在整个部署完成后清理。

## 15.4 原子性边界

SFTP 没有标准目录交换操作，Rename 之间存在极短切换窗口，但上传阶段不会影响线上旧目录。

不宣称严格零停机发布。

---

# 16. 顶层文件策略

## 16.1 上传

当前顶层文件使用已有安全临时发布：

```text
put temp
chmod
rename / backup swap
```

## 16.2 删除

删除候选：

```text
remote ownership root_files
-
current local root_files
```

示例：

```text
历史：index9.css, index.html
当前：index10.css, index.html
删除：index9.css
```

## 16.3 未受管文件

`index.php` 不在 Ownership Manifest 中，因此永远不删除。

---

# 17. Ownership、after_deploy 与 State 顺序

推荐顺序：

```text
Source/Incremental Operations
Hybrid Root Files
Hybrid Stage/Swap
Hybrid Historical Deletes
Remote Ownership Manifest Commit
after_deploy
Local State Save
Backup Cleanup
```

关键原则：

- Ownership Manifest 表示远端实际所有权事实；
- after_deploy 失败时，本地 State 保持旧值；
- 重跑根据远端 Ownership 继续收敛；
- after_deploy 不得修改 Hybrid 管理路径。

---

# 18. Recovery

Hybrid 多目录 Swap 需要一个非常窄的 Recovery Record：

```text
.git-deploy/recovery/<deployment-id>.json
```

记录：

- Mapping；
- Stage Paths；
- Backup Paths；
- 当前阶段；
- Old/New Ownership Hash。

阶段：

```text
PREPARED
STAGED
SWAPPING
OWNERSHIP_COMMITTED
COMMANDS_COMPLETE
STATE_COMPLETE
CLEANUP_COMPLETE
```

Recovery 只服务本次 Hybrid 部署，不扩展成历史回滚系统。

下次部署发现未完成 Recovery 时：

- 可安全继续则继续；
- 可安全恢复则恢复；
- 无法判断则 Fail Closed。

---

# 19. No-op 语义

建议固定：

```text
Hybrid 顶层目录
    每次部署均完整 Mirror
```

原因：

- 保证目录中没有远端孤儿；
- 不依赖本地 State；
- 不需要扫描目录内远端内容。

顶层文件仍可根据 Hash 跳过上传。

因此，只要聚合目录包含至少一个顶层目录，Hybrid 部署通常不是 No-op，`after_deploy` 也会执行。

这一成本是强一致目录 Mirror 的必然代价，必须在 README 中明确。

---

# 20. 失败语义

## Build 或聚合失败

```text
Remote Connect = 0
Remote Mutation = 0
State Change = 0
```

## Ownership Preflight 失败

```text
Remote Write = 0
State Change = 0
```

## Stage 上传失败

```text
线上旧目录完整保留
Ownership 不更新
State 不更新
```

## Swap 失败

```text
尝试恢复 Backup
Recovery 保留
Ownership 不推进
State 不推进
```

## Ownership 写入失败

```text
文件可能已切换
Recovery 保留
State 不推进
```

## after_deploy 失败

```text
远端文件和 Ownership 已生效
Local State 不更新
重跑时命令可能重复
```

## State Save 失败

```text
远端已成功
命令已成功
Local State 仍旧
重跑时命令可能重复
```

继续遵守 v1.3.0 的 At-least-once 边界。

---

# 21. 人工验收

## 21.1 基础部署

本地：

```text
.deploy/frontend-root/
├── index.html
└── assets/app.js
```

远端：

```text
index.php
.env
app/
```

确认：

- index.html 上传；
- assets Mirror；
- index.php 不变；
- `.env` 不变；
- Ownership Manifest 创建。

## 21.2 State 丢失

删除本地 State，远端人工增加：

```text
assets/old.js
```

重新部署后：

- old.js 消失；
- index.php/.env 不变；
- State 重建。

## 21.3 顶层文件删除

第一次：`index9.css`
第二次：`index10.css`

确认：

- index10.css 上传；
- index9.css 根据远端 Ownership 删除。

## 21.4 整个目录删除

第一次拥有 `old-assets/`，第二次本地聚合目录移除它。

确认远端 old-assets 消失。

## 21.5 未知目录

远端人工创建 `manual-backup/`。

确认不扫描、不删除。

## 21.6 首次接管

远端已有 `assets/`，但无 Manifest：

- 普通部署拒绝；
- `--full` 显示 Adoption；
- 接管后 Manifest 记录 assets。

## 21.7 Source 冲突

Git Source 存在：

```text
assets/backend.php
```

Hybrid 产生：

```text
assets/
```

确认本地阶段拒绝且不连接远端。

## 21.8 Workspace 冲突

两个仓库 Hybrid 指向相同物理 Root。

确认 Preflight 拒绝。

---

# 22. 原子 TODO

## Phase 0：规格冻结

### TODO-0001：新增 Hybrid ADR

- [x] 新建 `docs/adr-hybrid-output.md`
- [x] 记录真实问题；
- [x] 记录为何不做 Root Mirror；
- [x] 记录为何不做 Full Root Reconcile；
- [x] 记录单 Hybrid 约束；
- [x] 记录 SFTP-only；
- [x] 记录 State 与 Ownership 分工；
- [x] 记录 Dry-run 与 Remote-plan 契约。

### TODO-0002：冻结术语

- [x] Hybrid Output；
- [x] Root File；
- [x] Mirror Directory；
- [x] Remote Ownership Manifest；
- [x] Adoption；
- [x] Recovery Record；
- [x] Local Aggregation Root。

---

## Phase 1：配置

### TODO-0101：扩展 OutputConfig

- [x] 增加 `name`；
- [x] 增加 `mode`；
- [x] 默认 `incremental`；
- [x] Hybrid Name 必填；
- [x] Hybrid 仅 SFTP；
- [x] Hybrid 仅 `remote = "."`；
- [x] Hybrid 禁止显式 `delete_removed`；
- [x] 同 Config 最多一个 Hybrid。

### TODO-0102：增加 Project ID

- [x] 顶层可选 `project_id`；
- [x] 默认读取 Git Origin；
- [x] 规范化 SSH/HTTPS Git URL；
- [x] 无法确定时 Hybrid 拒绝；
- [x] 测试私有 URL 不泄露凭据。

### TODO-0103：兼容旧配置

- [x] 旧 Output 保持 Incremental；
- [x] 旧 TOML 无需修改；
- [x] Lockfile 与版本升级。

---

## Phase 2：`.deploy` 与 Git

### TODO-0201：默认 Exclude

- [x] 增加 `.deploy/**`；
- [x] 增加 `.git-deploy/**` Protect；
- [x] 回归测试。

### TODO-0202：Git Ignore 检查

- [x] `GitRepository.is_ignored()`；
- [x] Hybrid Local Root 未忽略时警告；
- [x] Clean Worktree 模式未忽略时拒绝；
- [x] 错误消息建议添加 `.deploy/`。

### TODO-0203：Init

- [x] 模板建议 `.deploy/`；
- [x] 不自动覆盖 `.gitignore`；
- [x] README 更新。

---

## Phase 3：本地扫描

### TODO-0301：数据类型

- [x] `HybridRootFile`；
- [x] `HybridDirectoryManifest`；
- [x] `HybridLocalManifest`；
- [x] 稳定排序。

### TODO-0302：扫描

- [x] Direct File；
- [x] Direct Directory；
- [x] Recursive File Manifest；
- [x] Empty Directory；
- [x] Hash/Size/Count；
- [x] Symlink Reject；
- [x] Unsupported Type Reject。

### TODO-0303：冲突检查

- [x] Root File vs Source；
- [x] Directory Prefix vs Source；
- [x] Hybrid vs Incremental Output；
- [x] Hybrid vs Protect；
- [x] File/Directory Type Conflict；
- [x] `.git-deploy` Conflict。

---

## Phase 4：Ownership Manifest

### TODO-0401：Schema

- [x] Schema Version；
- [x] Project ID；
- [x] Mapping；
- [x] Remote；
- [x] Directories；
- [x] Root Files；
- [x] Last Commit；
- [x] Timestamp。

### TODO-0402：读取

- [x] Missing；
- [x] Valid；
- [x] Corrupt；
- [x] Wrong Identity；
- [x] Wrong Schema；
- [x] Symlink；
- [x] Size Limit。

### TODO-0403：原子写入

- [x] Temp；
- [x] chmod；
- [x] Rename；
- [x] Backup Swap；
- [x] Restore；
- [x] Cleanup。

---

## Phase 5：Remote Preflight

### TODO-0501：Transport Read API

- [x] Native OpenSSH；
- [x] Paramiko；
- [x] FTP Unsupported；
- [x] Read-only Manifest；
- [x] `lstat` Path Type。

### TODO-0502：Adoption

- [x] Manifest Missing；
- [x] 路径不存在；
- [x] 路径存在且未拥有；
- [x] 普通模式拒绝；
- [x] `--full` 接管；
- [x] Unknown Path 不接管。

### TODO-0503：Workspace Gate

- [x] 相同物理 Root 两 Hybrid 拒绝；
- [x] 不同 Root 允许；
- [x] Remote Write 前完成。

---

## Phase 6：Planner

### TODO-0601：Hybrid Operation

- [x] Root File Upload；
- [x] Root File Delete；
- [x] Directory Mirror；
- [x] Directory Delete；
- [x] Adoption；
- [x] Ownership Update。

### TODO-0602：Root File Plan

- [x] Hash 增量上传；
- [x] Remote Ownership 删除；
- [x] State 丢失仍可删除；
- [x] 未拥有文件忽略。

### TODO-0603：Directory Plan

- [x] 当前 Directories 全部 Mirror；
- [x] 历史目录消失 → Delete；
- [x] 未拥有目录忽略；
- [x] Type Change；
- [x] Adoption。

### TODO-0604：Plan Render

- [x] Local Dry-run；
- [x] Remote Plan；
- [x] Adoption Warning；
- [x] File Count；
- [x] Bytes；
- [x] Ownership Delete Label；
- [x] Summary。

---

## Phase 7：CLI

### TODO-0701：`--remote-plan`

- [x] Parser；
- [x] 与 Dry-run 互斥；
- [x] Read-only Remote；
- [x] Zero Remote Write；
- [x] Zero State Write；
- [x] Zero after_deploy。

### TODO-0702：普通 Hybrid Deploy

- [x] Local Prepare；
- [x] Remote Preflight；
- [x] Full Plan；
- [x] Confirm；
- [x] Execute。

### TODO-0703：Adoption UX

- [x] 普通部署错误；
- [x] `--full` Adoption Plan；
- [x] 确认显示 Adoption 数量；
- [x] 非交互要求 `--yes`。

---

## Phase 8：SFTP Stage/Swap

### TODO-0801：Stage

- [x] Deployment ID；
- [x] Stage Path；
- [x] Permission；
- [x] Empty Directory；
- [x] Upload Progress；
- [x] Retry；
- [x] Cleanup。

### TODO-0802：Backup

- [x] Existing Directory Rename；
- [x] Missing；
- [x] Symlink Reject；
- [x] Ownership Verify。

### TODO-0803：Publish

- [x] Stage → Final；
- [x] Failure Restore；
- [x] Error Detail；
- [x] Recovery Phase Update。

### TODO-0804：Cleanup

- [x] Recursive Backup Delete；
- [x] 不跟随 Symlink；
- [x] Cleanup Failure；
- [x] Native OpenSSH；
- [x] Paramiko。

---

## Phase 9：Recovery

### TODO-0901：Recovery Schema

- [x] Deployment ID；
- [x] Mapping；
- [x] Target Fingerprint；
- [x] Stage/Backup；
- [x] Phase；
- [x] Old/New Ownership Hash。

### TODO-0902：生命周期

- [x] Create；
- [x] Update；
- [x] Detect；
- [x] Continue；
- [x] Restore；
- [x] Cleanup；
- [x] Doctor Report。

---

## Phase 10：Deployer

### TODO-1001：执行顺序

- [x] Source/Incremental；
- [x] Hybrid Root Files；
- [x] Hybrid Stage/Swap；
- [x] Hybrid Deletes；
- [x] Ownership Commit；
- [x] after_deploy；
- [x] Local State；
- [x] Backup Cleanup。

### TODO-1002：失败重跑

- [x] Stage Failure；
- [x] Swap Failure；
- [x] Ownership Failure；
- [x] Command Failure；
- [x] State Failure；
- [x] Ctrl-C；
- [x] Rerun Convergence。

### TODO-1003：No-op 语义

- [x] Hybrid Directory 每次 Mirror；
- [x] Root File Hash 可跳过；
- [x] after_deploy 行为文档化；
- [x] Summary 明确。

---

## Phase 11：Workspace

### TODO-1101：冲突 Gate

- [x] 同 Root 多 Hybrid 拒绝；
- [x] Different Root 允许；
- [x] Endpoint Resolution；
- [x] Build/Write 前 Fail Closed。

### TODO-1102：Combined Plan

- [x] Local Hybrid；
- [x] Remote Ownership；
- [x] Adoption；
- [x] Commands；
- [x] Bytes；
- [x] Order。

### TODO-1103：Partial Failure

- [x] A 成功；
- [x] B Hybrid 失败；
- [x] C 不执行；
- [x] Rerun 收敛；
- [x] Shared Native Master。

---

## Phase 12：Doctor

### TODO-1201：Local

- [x] `.deploy` Ignore；
- [x] Hybrid Local Root；
- [x] Project ID；
- [x] Config Conflict；
- [x] Build Command。

### TODO-1202：Remote

- [x] Ownership Manifest；
- [x] Recovery Record；
- [x] `.git-deploy` 权限；
- [x] Owned Path Types；
- [x] Adoption Required；
- [x] Read-only。

---

## Phase 13：参考聚合脚本

### TODO-1301：Python 示例

文件：

```text
examples/aggregate_frontend_builds.py
```

- [x] 显式 Sources；
- [x] 显式 Destination；
- [x] Duplicate Detection；
- [x] File/Directory Conflict；
- [x] Symlink Reject；
- [x] Atomic Local Replace；
- [x] 清晰错误信息。

### TODO-1302：示例文档

- [x] 单前端；
- [x] 多前端；
- [x] pnpm/npm；
- [x] `.gitignore`；
- [x] Build Steps。

---

## Phase 14：测试与发布

### TODO-1401：单元测试

- [x] Config；
- [x] Scanner；
- [x] Ownership；
- [x] Planner；
- [x] CLI；
- [x] Deployer；
- [x] Workspace；
- [x] Recovery。

### TODO-1402：集成测试

- [x] Docker Native OpenSSH；
- [x] Docker Paramiko；
- [x] index.php Preserve；
- [x] `.env` Preserve；
- [x] Old Root File Delete；
- [x] Old Directory Delete；
- [x] Unknown Directory Preserve；
- [x] State Loss；
- [x] Adoption；
- [x] Recovery。

### TODO-1403：发布

- [x] Python 3.11；
- [x] Python 3.12；
- [x] Ruff；
- [x] ty；
- [x] Lock Check；
- [x] Wheel/sdist；
- [x] Isolated Install；
- [x] PR CI；
- [x] Main/Tag Blob Verify。

---

# 23. 里程碑

## Milestone 1：Local Model

交付：

- Config；
- `.deploy` 检查；
- Scanner；
- Conflict；
- Local Dry-run。

## Milestone 2：Remote Ownership

交付：

- Ownership Manifest；
- Remote Plan；
- Adoption；
- Root File Delete。

## Milestone 3：Directory Mirror

交付：

- Stage；
- Backup；
- Swap；
- Restore；
- Recovery。

## Milestone 4：Workspace 与稳定性

交付：

- Workspace Gate；
- Combined Plan；
- Doctor；
- Failure/Rerun。

## Milestone 5：Release

交付：

- 完整测试；
- README；
- Migration Guide；
- Release Notes；
- Wheel；
- Tag。

---

# 24. 最终结论

v1.4.0 应只聚焦一个真实能力：

> 使用一个本地聚合目录，将其通过一个 Hybrid Output 安全部署到包含后端与环境文件的混合远端根目录。

最终职责划分：

```text
多个前端项目
    → 各自构建

项目 Build 脚本
    → 本地聚合
    → 冲突检查

git-deploy Hybrid
    → 顶层目录 Mirror
    → 顶层文件 Incremental
    → Remote Ownership Delete
    → 未知远端内容保留

after_deploy
    → Reload / Restart / Health Check

Local State
    → 记录最后成功部署

Remote Ownership Manifest
    → 记录远端 Hybrid 删除所有权
```

这套设计比完整 Root Reconcile 更简单，比单纯依赖本地 State 更可靠，也不会因为前端产物铺到项目根目录而误删 `index.php`、`.env` 和后端目录。

建议将本方案作为 v1.4.0 唯一主要功能，暂停同时开发 Root Mirror、FTP Hybrid、多 Hybrid 同根协调和完整远端 Reconcile。
