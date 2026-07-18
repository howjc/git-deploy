# git-deploy v1.5.0 实施方案：FTP In-place Hybrid

> 项目：`howjc/git-deploy`
> 当前稳定基线：`v1.4.3`
> 目标版本：`v1.5.0`
> 方案状态：实现完成 / 发布就绪；外部 FTP 服务器兼容验证为可选人工增强
> 核心能力：FTP In-place Hybrid + Remote Ownership + Forward Resume
> 产品定位：个人使用、单发布器、简单稳定、失败后可重跑收敛
> 更新时间：2026-07-18

---

# 1. 执行摘要

v1.4.3 的 Hybrid 只支持 SFTP，但实际存在一类无法绕开的部署场景：

```text
只有 FTP 账号
无法开通 SSH / SFTP
远端根目录同时包含后端文件和前端构建产物
前端构建目录需要清理孤儿文件
本地 State 丢失后仍要保留删除所有权
```

普通 Incremental Output 无法完整满足：

- 顶层前端目录内的孤儿文件清理；
- 整个旧前端目录删除；
- 本地 State 丢失或换机器后的远端删除所有权恢复；
- 混合根目录下未知后端内容保护。

因此 v1.5.0 应支持：

> **FTP In-place Hybrid**

用户层语义保持与 SFTP Hybrid 一致：

```text
Local Aggregation Root 的直接文件
    → Incremental Root Files

Local Aggregation Root 的直接目录
    → Mirror Directories

Remote Ownership Manifest
    → 记录可删除的前端顶层文件和目录

未知远端根目录内容
    → 永远不处理
```

执行后端根据协议自动选择：

```text
SFTP Hybrid
    → Stage / Backup / Swap / Recovery
    → 接近原子目录替换

FTP Hybrid
    → File Stage / In-place Publish / Upload-first / Prune-last
    → Forward Resume
    → 最终一致
```

FTP 版本不宣称：

- 目录原子替换；
- 零停机；
- 跨整个文件树回滚；
- 多发布器协调；
- SSH `after_deploy`；
- POSIX 权限与符号链接语义。

但仍然保证：

- 只处理当前或历史 Ownership 明确拥有的顶层路径；
- 未知 `index.php`、`.env`、后端和运行时目录不进入删除候选；
- 所有当前文件发布成功前不开始孤儿删除；
- 删除失败、连接中断或进程退出后可向前重跑收敛；
- Ownership 与 Local State 不因半成功部署静默推进；
- 本地 State 丢失时仍可从 Remote Ownership 恢复删除事实。

---

# 2. 北极星与护栏

## 2.1 北极星

> 在只有 FTP 权限的混合项目根目录中，安全地部署本地聚合后的前端产物，清理前端受管目录中的孤儿内容，并始终保留未知后端和运行时内容。

## 2.2 核心成功标准

部署成功后：

1. 当前 Hybrid Root Files 与本地聚合结果一致；
2. 当前 Hybrid Mirror Directories 中的文件树与本地一致；
3. 历史拥有但本地已删除的 Root File 被移除；
4. 历史拥有但本地已删除的 Mirror Directory 被移除；
5. 未在 Ownership 中的远端根目录内容保持不变；
6. 本地 State 丢失后仍可依据 Remote Ownership 清理历史产物；
7. 中断后重跑可以继续向最终状态收敛；
8. Ownership 只在文件发布与孤儿清理全部成功后提交；
9. Local State 只在 Ownership 提交后保存；
10. 无法可靠识别远端类型时 Fail Closed。

## 2.3 产品护栏

v1.5.0 不允许为了支持 FTP Hybrid 引入：

- 完整 Root Reconcile；
- FTP 目录 Stage/Swap；
- FTP 历史版本；
- 通用 Rollback；
- HTTP/PHP 远端 Helper；
- 多 Hybrid 同根；
- Remote Distributed Lock；
- 通用 Pipeline DSL；
- FTP `after_deploy`；
- 自动探测全部构建目录。

---

# 3. 产品语义

## 3.1 用户配置保持统一

不新增：

```toml
mode = "ftp-hybrid"
```

继续使用：

```toml
[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"
```

目标协议决定执行后端：

```toml
[targets.prod]
protocol = "ftp"
host = "ftp.example.com"
port = 21
username = "deploy"
password_env = "FTP_PASSWORD"
remote_root = "/public_html"
passive = true
```

## 3.2 后端分类

```text
protocol = "sftp"
    → Staged Hybrid

protocol = "ftp"
    → In-place Hybrid
```

## 3.3 不新增用户级 Mode 的原因

Hybrid 的业务语义没有变化：

```text
Root Files
Mirror Directories
Remote Ownership
Unknown Root Preservation
```

变化的是协议可提供的执行保证，不应该要求用户理解两个 Hybrid 配置模型。

---

# 4. SFTP 与 FTP 保证矩阵

| 能力 | SFTP Hybrid | FTP In-place Hybrid |
|---|---:|---:|
| Local Aggregation | 是 | 是 |
| Single Hybrid | 是 | 是 |
| Remote Ownership | 是 | 是 |
| Unknown Root Preservation | 是 | 是 |
| State Loss Ownership Recovery | 是 | 是 |
| Root File Staging | 是 | 是 |
| Mirror Directory File Staging | 是 | 是 |
| Upload-first / Prune-last | 是 | 是 |
| Orphan File Cleanup | 是 | 是 |
| Removed Directory Cleanup | 是 | 是 |
| Directory Atomic Swap | 接近 | 否 |
| Backup Restore | 是 | 否 |
| Forward Resume | Recovery | Resume Marker |
| Zero-downtime | 否 | 否 |
| Remote Command | SSH | 否 |
| POSIX Mode | 是 | 否 |
| Symlink Semantics | Fail Closed | Fail Closed / Unsupported |
| Concurrent Publisher Safety | 不保证 | 不保证 |

Plan 必须明确显示：

```text
HYBRID BACKEND: FTP IN-PLACE

Guarantees:
  remote ownership: yes
  unknown root preservation: yes
  upload before prune: yes
  forward resume: yes
  atomic directory swap: no
  rollback: no
  after_deploy: no
```

---

# 5. 当前代码差距

当前 FTP Transport 已具备：

- 连接与认证；
- Binary `STOR`；
- 文件删除；
- 递归创建目录；
- `NLST` 目录枚举；
- 目录缓存；
- Passive/Active；
- 路径边界；
- 重试上层能力。

FTP Hybrid 仍缺少：

```python
read_file()
write_file_atomic_or_staged()
rename_path()
list_directory_typed()
scan_tree()
remove_empty_directory()
remove_tree()
probe_hybrid_capabilities()
```

Config 当前还会拒绝 Hybrid 与 FTP Target 共存，需要按能力探测替代静态禁止。

---

# 6. FTP Hybrid 最低能力要求

## 6.1 必需命令

服务器必须支持：

```text
FEAT
MLSD 或等价可靠类型化枚举
RETR
STOR
RNFR / RNTO
DELE
MKD
RMD
CWD / PWD
```

## 6.2 v1.5.0 强制要求 MLSD

FTP Hybrid 的删除安全依赖类型化递归扫描。

v1.5.0 不使用通用 `LIST` 文本解析，因为：

- Unix/Windows/IIS/自定义格式不同；
- 日期、权限和名称解析不稳定；
- 文件名可包含空格；
- 符号链接表示不统一；
- 解析错误可能造成误删。

首版也不使用 `NLST + CWD` 递归猜测类型作为正式删除依据。

规则：

```text
MLSD 不支持
    → FTP Hybrid Fail Closed
    → 普通 Incremental FTP 仍可使用
```

## 6.3 接受的 MLSD Type

允许：

```text
type=file
type=dir
type=cdir
type=pdir
```

跳过：

```text
cdir
pdir
```

拒绝或要求人工检查：

```text
OS.unix=slink
slink
unknown
特殊设备
无法识别类型
```

## 6.4 Rename 能力

v1.5.0 最低能力要求：

```text
同一 FTP Root 内跨目录 RNFR/RNTO
Regular File Rename
Rename 可替换已有 Regular File
```

用途：

```text
.git-deploy/ftp-stage/<id>/...
    → 在线目标文件

临时 Ownership
    → 正式 Ownership

临时 Resume Marker
    → 正式 Resume Marker
```

目录 Rename 不作为 v1.5.0 必需能力。

## 6.5 为什么首版要求 Rename Replace

如果服务器不支持覆盖已有文件：

```text
必须使用 Final → Backup → Temp → Final
```

这会重新引入：

- 在线文件短暂缺失；
- Backup 恢复；
- 每文件 Durable Journal；
- 更多协议差异。

为了保持 v1.5.0 简单稳定：

```text
Rename Replace 不支持
    → FTP Hybrid 拒绝
```

后续真实需求足够强时，再评估 Compatibility Backend。

---

# 7. 显式能力探测

## 7.1 不在普通 Plan 前暗中写入

能力探测需要创建临时文件和目录，不能在：

```bash
git-deploy prod --remote-plan
```

中静默执行。

新增显式命令：

```bash
git-deploy doctor prod --probe-ftp-hybrid
```

## 7.2 Probe 路径

```text
<remote_root>/.git-deploy/ftp-probe/<uuid>/
```

## 7.3 Probe 内容

检查：

1. `.git-deploy` 可创建；
2. MLSD 可用；
3. MLSD 正确返回 File/Directory；
4. STOR/RETR 二进制一致；
5. 跨目录 RNFR/RNTO；
6. RNTO 可替换已有文件；
7. DELE；
8. RMD；
9. 空目录 MLSD；
10. 名称边界与 UTF-8；
11. 清理 Probe 后无残留。

## 7.4 Capability Profile

本地保存：

```text
<git-common-dir>/git-deploy/ftp-capabilities/<target-fingerprint>.json
```

示例：

```json
{
  "schema": 1,
  "target_fingerprint": "...",
  "server_banner_hash": "...",
  "features": {
    "mlsd": true,
    "retr": true,
    "rename_cross_directory": true,
    "rename_replace_file": true,
    "delete_file": true,
    "remove_directory": true
  },
  "probed_at": 1784361600
}
```

不记录：

- 密码；
- Host 密钥；
- 文件内容；
- FTP Session；
- 环境变量值。

## 7.5 Profile 失效

以下情况要求重新 Probe：

- Target Fingerprint 变化；
- Host/Port/Username/Root 变化；
- Server Banner Hash 变化；
- Capability Cache 损坏；
- 显式再次运行 `--probe-ftp-hybrid`（v1.5.1 已移除无独立语义的 `--reprobe`）；
- 执行中出现能力不兼容错误。

---

# 8. 远端内部目录

FTP Hybrid 使用：

```text
<remote_root>/.git-deploy/
├── hybrid/
│   └── frontend-root.json
├── ftp-hybrid/
│   ├── stage/
│   │   └── <deployment-id>/
│   ├── pending/
│   │   └── frontend-root.json
│   └── probe/
└── ...
```

强制 Protect：

```text
.git-deploy/**
```

FTP Hybrid 不读取或处理：

```text
.git-deploy
```

之外的内部未知元数据。

---

# 9. Remote Ownership Manifest

## 9.1 复用现有 Schema

继续使用：

```text
.git-deploy/hybrid/<mapping>.json
```

不修改 Ownership Schema。

示例：

```json
{
  "schema": 1,
  "project_id": "github.com/howjc/project",
  "mapping": "frontend-root",
  "remote": ".",
  "directories": [
    "assets",
    "images"
  ],
  "root_files": [
    "favicon.ico",
    "index.html"
  ],
  "last_commit": "abc123",
  "updated_at": 1784361600
}
```

## 9.2 FTP 读取

```text
MLSD 检查 Manifest 是 File
RETR 读取
限制 64 KiB
UTF-8 JSON
严格 Schema/Identity
```

## 9.3 FTP 写入

流程：

```text
1. STOR 到 ftp-stage/<id>/ownership.json
2. RETR 回读并验证 Hash
3. RNFR / RNTO 替换正式 Ownership
4. RETR 正式文件确认 Hash
```

Ownership 必须在：

```text
当前文件全部发布成功
孤儿文件全部删除成功
多余目录全部删除成功
```

之后更新。

---

# 10. FTP Forward Resume Marker

## 10.1 为什么必须增加

如果部署已经创建新的 Root Path，但在 Ownership 更新前中断：

```text
Remote Path 已存在
Ownership 仍然没有它
```

下一次普通计划会误认为：

```text
已有未知路径
需要 --full Adoption
```

这会破坏自然重跑收敛。

因此 FTP Hybrid 需要一个轻量远端 Resume Marker。

## 10.2 Marker 路径

```text
.git-deploy/ftp-hybrid/pending/<mapping>.json
```

## 10.3 Marker 内容

```json
{
  "schema": 1,
  "project_id": "github.com/howjc/project",
  "mapping": "frontend-root",
  "remote": ".",
  "deployment_id": "uuid",
  "phase": "PREPARED",
  "previous_ownership_hash": "...",
  "next_ownership_hash": "...",
  "local_manifest_hash": "...",
  "head": "abc123",
  "next_state": {
    "target": "prod",
    "target_fingerprint": "...",
    "last_commit": "abc123",
    "outputs": {
      "index.html": {
        "sha256": "...",
        "size": 1234
      }
    }
  },
  "created_at": 1784361600
}
```

## 10.4 Phase

```text
PREPARED
FILES_PUBLISHED
PRUNED
OWNERSHIP_COMMITTED
STATE_COMPLETE
```

## 10.5 Marker 语义

### PREPARED

- Stage 可能部分存在；
- 在线当前文件可能部分发布；
- 未开始 Prune；
- Ownership 仍旧。

重跑：

```text
重新上传全部当前文件
重新发布全部当前文件
继续
```

### FILES_PUBLISHED

- 当前文件全部发布完成；
- 尚未 Prune 或 Prune 未完成；
- Ownership 仍旧。

重跑：

```text
可重新发布
然后继续 Prune
```

### PRUNED

- 当前文件已发布；
- Orphan 已清理；
- Ownership 尚未提交。

重跑：

```text
重新验证远端
提交 Ownership
```

### OWNERSHIP_COMMITTED

- Remote Ownership 已是新值；
- Local State 可能未保存。

重跑或显式 Resume：

```text
使用 Marker 中 next_state 保存 Local State
```

### STATE_COMPLETE

```text
清理 Stage
删除 Pending Marker
```

## 10.6 Resume 条件

自动恢复或继续前必须验证：

```text
Project ID 匹配
Mapping 匹配
Remote 匹配
Target Fingerprint 匹配
Current Ownership Hash
    ∈ {previous_ownership_hash, next_ownership_hash}
Current Local Manifest Hash
    = pending.local_manifest_hash
```

如果 Local Manifest 已变化：

```text
Fail Closed
提示先恢复原 Build 或人工清理 Pending
```

首版不允许把旧 Pending 自动迁移到新 Local Manifest。

## 10.7 Resume 不是 Rollback

FTP Marker 只提供：

```text
Forward Resume
```

不提供：

- 恢复整个旧目录树；
- 撤销已经成功发布的文件；
- 恢复已经删除的 Orphan；
- 历史版本回滚。

---

# 11. FTP Remote Scanner

## 11.1 扫描范围

只扫描：

```text
Current Local Hybrid Directories
+
Historical Ownership Directories
```

不扫描整个项目根目录。

Root 只枚举：

```text
Current Direct Names
Historical Direct Names
.git-deploy
```

## 11.2 Typed Entry

```python
@dataclass(frozen=True, slots=True)
class FTPRemoteEntry:
    path: str
    kind: Literal["file", "directory"]
    size: int | None
    modify: str | None
```

## 11.3 Remote Tree

```python
@dataclass(frozen=True, slots=True)
class FTPRemoteTree:
    root: str
    files: tuple[str, ...]
    directories: tuple[str, ...]
```

## 11.4 递归规则

- MLSD 每目录一次；
- 跳过 `cdir/pdir`；
- Unknown Type Fail Closed；
- 路径组件必须满足 Stable Component；
- 不接受绝对路径；
- 不接受 `..`；
- 限制最大深度；
- 限制最大条目数；
- 列表失败不得解释为 Empty；
- Permission Error 不得解释为 Missing。

## 11.5 建议限制

默认：

```toml
[ftp_hybrid]
max_scan_entries = 200000
max_scan_depth = 64
```

不建议首版开放过多调参。

可使用内部常量，Doctor 输出实际限制即可。

---

# 12. FTP Hybrid Plan

## 12.1 新 Backend 标识

```python
HybridBackend = Literal[
    "sftp-staged",
    "ftp-in-place",
]
```

## 12.2 Operation 类型

复用或新增：

```python
FTPStageFile
FTPPublishFile
FTPCreateDirectory
FTPDeleteFile
FTPRemoveDirectory
FTPWriteOwnership
FTPWritePending
FTPClearPending
```

Plan 不必把每个 Stage 临时文件都显示给用户。

用户 Plan 聚合显示：

```text
FTP HYBRID [frontend-root] .deploy/frontend-root -> .

ROOT FILES
  UPLOAD index.html
  DELETE old.css

MIRROR DIRECTORIES
  MIRROR assets/
    publish 138 file(s)
    delete 46 orphan file(s)
    remove 3 directory(s)

OWNERSHIP
  UPDATE .git-deploy/hybrid/frontend-root.json

BACKEND
  FTP IN-PLACE
  upload-first / prune-last
  no directory rollback
```

## 12.3 Root File 增量

Root File 上传条件：

```text
--full
Local State Entry 变化
Remote Root File Missing
Remote Type 不是 File
Adoption
Pending Resume
```

不读取远端 Hash。

## 12.4 Directory Mirror

每次：

```text
上传本地全部当前文件
```

原因：

- FTP 无可靠远端 Hash；
- Size/Modify 不足以证明内容；
- 保持简单和确定；
- 目录本来就是强 Mirror 单元。

## 12.5 Orphan

```text
Remote Directory Files
-
Local Directory Files
=
Delete File Operations
```

```text
Remote Directories
-
Local Directories
=
RMD Candidates
```

目录从最深层开始删除。

---

# 13. FTP 执行算法

## 13.1 总体顺序

```text
Build
Local Scan
Freeze Bytes
Read Capability Profile
Read Ownership
Read Pending Marker
Typed Remote Scan
Generate Full Plan
Confirm
Write Pending PREPARED
Upload all files to FTP Stage
Publish all current files
Write Pending FILES_PUBLISHED
Delete orphan files
Remove orphan directories
Write Pending PRUNED
Publish new Ownership
Write Pending OWNERSHIP_COMMITTED
Save Local State
Write Pending STATE_COMPLETE
Cleanup FTP Stage
Delete Pending Marker
```

## 13.2 核心原则

```text
Upload First
Prune Last
Ownership Last
State After Ownership
```

---

# 14. 文件 Stage 与 Publish

## 14.1 Stage 路径

```text
.git-deploy/ftp-hybrid/stage/<deployment-id>/<final-relative-path>
```

## 14.2 Upload Stage

每个文件：

```text
STOR Stage Path
RETR Stage Path
校验 SHA256
```

FTP 下载校验会增加流量，但只在 Stage 文件发布前执行，保证：

- 传输完整；
- Binary Mode 正确；
- 服务器未修改内容；
- Rename 前内容可信。

可在未来增加配置关闭回读，但 v1.5.0 默认强制。

## 14.3 Publish

```text
RNFR Stage Path
RNTO Final Path
```

能力 Probe 已证明：

```text
跨目录
覆盖已有 File
```

## 14.4 Publish 失败

```text
Pending 保留
Ownership 不更新
State 不更新
Stage 未消费文件保留
```

重跑：

```text
重新 Stage / Publish
```

## 14.5 Root File Missing 的并发边界

FTP Rename Replace 会覆盖最后一刻出现的同名目标。

因此 FTP Hybrid 明确要求：

```text
单发布器
部署期间不得由其他工具修改受管路径
```

Plan 显示：

```text
Concurrent last-moment no-overwrite: not guaranteed by FTP backend
```

---

# 15. Directory Mirror

## 15.1 创建目录

根据 Local Manifest：

```text
浅到深 MKD
```

已存在：

```text
视为成功，但必须由 MLSD 证明是 Directory
```

## 15.2 上传当前文件

所有当前文件：

```text
Stage
Verify
Publish
```

文件发布完成前不执行删除。

## 15.3 删除 Orphan Files

所有当前文件成功后：

```text
DELE
```

只删除：

```text
Ownership 管理的 Direct Directory 内
Remote Scan 确认的 Orphan File
```

## 15.4 删除多余目录

文件删除完成后：

```text
深到浅 RMD
```

只删除：

- 不在 Local Manifest；
- 已确认 Empty；
- 位于受管 Mirror Directory 内。

## 15.5 整个旧顶层目录删除

历史 Ownership 有：

```text
old-assets/
```

当前 Local 无：

```text
递归扫描
删除 Files
深到浅 RMD
最后 RMD old-assets
```

---

# 16. Root File 删除

历史 Ownership：

```text
index9.css
```

当前 Local：

```text
不存在
```

执行顺序：

```text
所有当前文件 Publish 成功
    ↓
DELE index9.css
```

如果 Delete 失败：

```text
Ownership 不更新
State 不更新
Pending 保留
重跑继续
```

---

# 17. 类型变化

## 17.1 v1.5.0 规则

FTP Hybrid 首版拒绝直接路径：

```text
File → Directory
Directory → File
```

错误：

```text
FTP Hybrid cannot safely change an owned direct path between file and directory;
remove or migrate it explicitly, then rerun with --full
```

## 17.2 原因

类型变化需要：

- 删除旧类型；
- 创建新类型；
- 中间不可避免缺失；
- 无目录回滚；
- 失败恢复复杂。

真实前端构建的顶层类型变化很少，不值得为首版增加复杂状态机。

---

# 18. Adoption

## 18.1 首次部署

当前 Local 有：

```text
assets/
index.html
```

Remote 同名路径已存在，但 Ownership 无记录：

```text
普通部署
    → 拒绝
```

## 18.2 `--full`

```bash
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

显示：

```text
ADOPT assets/
ADOPT index.html
```

只接管当前 Local 同名路径。

未知：

```text
index.php
.env
uploads/
app/
```

不接管。

## 18.3 Pending Resume 例外

如果同名路径来自一个已验证 Pending Marker，且：

```text
Local Manifest Hash 一致
Previous Ownership Hash 一致
```

则不要求重复 `--full`，视为上次部署的 Forward Resume。

---

# 19. `--dry-run`、`--remote-plan` 与 Doctor

## 19.1 `--dry-run`

保持：

```text
Build
Local Scan
Freeze
Remote Connect = 0
```

显示：

```text
FTP Remote Scan not loaded
Run --remote-plan for exact orphan deletes.
```

## 19.2 `--remote-plan`

只读：

- Capability Profile；
- Ownership；
- Pending Marker；
- MLSD Remote Scan；
- Exact Upload/Delete/RMD Plan。

不写：

- Stage；
- Pending；
- Ownership；
- State。

## 19.3 `doctor`

新增检查：

```text
FTP Hybrid Capability Profile
FTP Server Features
MLSD
Rename Replace
Cross-directory Rename
Remote Internal Paths
Ownership
Pending Resume
Remote Scan Boundary
```

## 19.4 `--probe-ftp-hybrid`

显式写 Probe：

```bash
git-deploy doctor prod --probe-ftp-hybrid
```

必须显示：

```text
This probe creates and removes temporary files under .git-deploy/ftp-probe.
```

非交互环境要求：

```bash
--yes
```

---

# 20. `after_deploy`

FTP Target 没有 SSH 命令通道。

规则：

```text
FTP Hybrid + after_deploy
    → ConfigError
```

不新增：

- HTTP Callback；
- PHP Endpoint；
- Webhook；
- Remote Script Upload；
- FTP SITE EXEC。

---

# 21. 失败语义

## 21.1 Build 失败

```text
Remote Connect = 0
Remote Mutation = 0
State Change = 0
```

## 21.2 Remote Plan 失败

```text
Remote Write = 0
State Change = 0
```

## 21.3 Pending 写入失败

```text
Online Files = 0 changes
State = unchanged
```

## 21.4 Stage Upload 失败

```text
Online Final = unchanged for un-published files
No Prune
Ownership unchanged
State unchanged
Pending PREPARED
```

## 21.5 Publish 部分失败

```text
Some current files may be new
No Prune
Ownership unchanged
State unchanged
Pending PREPARED
Rerun re-publishes all
```

## 21.6 Prune 失败

```text
Current files already published
Some orphans may already be deleted
Ownership unchanged
State unchanged
Pending FILES_PUBLISHED
Rerun continues
```

## 21.7 Ownership 写入失败

```text
Files and prune may be complete
Ownership remains old or publish failed
State unchanged
Pending PRUNED
Rerun verifies and commits
```

## 21.8 State Save 失败

```text
Ownership committed
Pending OWNERSHIP_COMMITTED
Next run saves stored next_state
```

## 21.9 Pending Cleanup 失败

```text
Deployment facts successful
State successful
Pending STATE_COMPLETE
Doctor reports cleanup
Next run removes internal remnants
```

---

# 22. 安全边界

## 22.1 未知根目录

永远不删除：

```text
Remote Root Entry
not in Current Ownership
not in Historical Ownership
not in verified Pending Next Ownership
```

## 22.2 Mirror Directory 内部

一旦顶层目录属于 Ownership：

```text
其内部由 Local Manifest 完整管理
未知内部文件会被 Prune
```

## 22.3 符号链接

FTP 无统一可靠 Symlink 语义。

规则：

```text
MLSD 返回 Symlink/Unknown
    → Fail Closed
```

不跟随、不删除。

## 22.4 路径名

继续使用 Stable Component：

- 无 `/`；
- 无 `\`；
- 非 `.` / `..`；
- 无首尾空格；
- 无 Tab；
- 无控制字符；
- 无不可见空白。

## 22.5 单发布器

不支持：

- 两台机器同时部署；
- CI 与本地同时部署；
- 面板和 git-deploy 同时修改受管路径；
- 手工 FTP 同时上传。

## 22.6 最大删除护栏

建议：

```text
Delete > 10,000 files
    → 强警告

Remove > 1,000 directories
    → 强警告

Delete all owned paths
    → 强警告
```

`--yes` 可跳过交互，但 Plan 必须完整显示统计。

---

# 23. Config 修改

## 23.1 移除静态禁止

删除：

```text
只要有 Hybrid 就不能存在 FTP Target
```

## 23.2 新验证

```text
Hybrid + FTP:
    after_deploy 必须为空
    Capability Profile 必须存在
    FTP Hybrid Requirements 必须通过
```

## 23.3 可选配置

首版建议只增加：

```toml
[ftp_hybrid]
verify_staged_uploads = true
```

甚至可以不开放配置，直接固定为 `true`。

不要在首版暴露：

- scan mode；
- rename mode；
- fallback mode；
- direct upload；
- unsafe mode；
- parallel delete。

---

# 24. 数据模型

## 24.1 Backend

```python
class HybridBackend(str, Enum):
    SFTP_STAGED = "sftp-staged"
    FTP_IN_PLACE = "ftp-in-place"
```

## 24.2 Capability

```python
@dataclass(frozen=True, slots=True)
class FTPHybridCapabilities:
    schema: int
    target_fingerprint: str
    server_banner_hash: str
    mlsd: bool
    rename_cross_directory: bool
    rename_replace_file: bool
    retr: bool
    delete_file: bool
    remove_directory: bool
    probed_at: int
```

## 24.3 Pending

```python
class FTPPendingPhase(str, Enum):
    PREPARED = "PREPARED"
    FILES_PUBLISHED = "FILES_PUBLISHED"
    PRUNED = "PRUNED"
    OWNERSHIP_COMMITTED = "OWNERSHIP_COMMITTED"
    STATE_COMPLETE = "STATE_COMPLETE"
```

```python
@dataclass(frozen=True, slots=True)
class FTPHybridPending:
    schema: int
    project_id: str
    mapping: str
    remote: str
    deployment_id: str
    phase: FTPPendingPhase
    previous_ownership_hash: str
    next_ownership_hash: str
    local_manifest_hash: str
    head: str
    next_state: TargetState
    created_at: int
```

## 24.4 Remote Tree

```python
@dataclass(frozen=True, slots=True)
class FTPRemoteTree:
    root: str
    files: tuple[str, ...]
    directories: tuple[str, ...]
```

---

# 25. Transport API

建议扩展通用 Transport：

```python
read_file(remote_path, max_bytes)
write_file_staged(remote_path, data)
list_directory_typed(remote_path)
remove_directory(remote_path)
```

FTP 专属：

```python
probe_hybrid_capabilities()
scan_tree(remote_path)
publish_staged_file(stage_path, final_path)
```

不建议把 FTP 不具备的能力伪装为 SFTP：

```python
lstat()
rename_path_no_replace()
```

Planner 应基于 Backend Capability，而不是假设所有 Transport 语义相同。

---

# 26. 开发阶段

## Phase 0：规格冻结

- FTP Hybrid 使用相同 `mode = "hybrid"`；
- Backend 自动选择；
- MLSD 强制；
- Rename Replace 强制；
- Type Change 拒绝；
- FTP `after_deploy` 禁止；
- Forward Resume 必须实现；
- 不做目录 Swap/回滚。

## Phase 1：Capability Probe

- FEAT；
- MLSD；
- STOR/RETR；
- Cross-directory Rename；
- Rename Replace；
- DELE/RMD；
- Profile Cache；
- Doctor。

## Phase 2：FTP Typed Scanner

- MLSD Parser；
- File/Directory；
- Unknown Type Fail Closed；
- Recursive Scan；
- Limits；
- Stable Paths。

## Phase 3：Ownership Read/Write

- RETR Ownership；
- Stage Ownership；
- Verify；
- Publish；
- Re-read Hash。

## Phase 4：Pending Resume

- Schema；
- Read/Write；
- Phase；
- Current Manifest Match；
- Ownership Match；
- Resume Planner；
- State Recovery。

## Phase 5：Planner

- Backend Selection；
- FTP Root Files；
- Mirror Upload；
- Orphan Delete；
- RMD；
- Adoption；
- Pending Resume；
- Plan Render。

## Phase 6：Executor

- Write Pending；
- Stage All Files；
- Verify All；
- Publish；
- Prune；
- Ownership；
- State；
- Cleanup。

## Phase 7：CLI / Workspace

- Remote Plan；
- Doctor Probe；
- Workspace Combined Plan；
- Workspace Resume；
- Confirm；
- Summary。

## Phase 8：Tests / Docs / Release

---

# 27. 原子 TODO

## Phase 0：ADR 与契约

### TODO-0001：建立 FTP Hybrid ADR

- [x] 新建 `docs/adr-ftp-hybrid.md`
- [x] 记录真实 FTP-only 场景；
- [x] 记录为什么不用 Incremental；
- [x] 记录为什么不做 FTP Directory Swap；
- [x] 记录 MLSD 与 Rename Replace 要求；
- [x] 记录 Forward Resume；
- [x] 记录单发布器边界；
- [x] 记录 Type Change 拒绝。

验收：

- ADR 可独立解释所有取舍。

### TODO-0002：更新 Hybrid Backend 术语

- [x] `SFTP Staged Hybrid`
- [x] `FTP In-place Hybrid`
- [x] `Forward Resume`
- [x] `Upload-first`
- [x] `Prune-last`
- [x] `Capability Profile`

## Phase 1：Config

### TODO-0101：解除 FTP Hybrid 静态禁止

- [x] 移除 Hybrid + FTP 全局 ConfigError；
- [x] 保持普通 FTP；
- [x] 保持单 Hybrid；
- [x] 保持 `remote = "."`；
- [x] 保持 Project ID。

### TODO-0102：禁止 FTP after_deploy

- [x] FTP Hybrid + Commands 拒绝；
- [x] 错误信息清晰；
- [x] 普通 SFTP 不回归。

### TODO-0103：Backend Resolve

- [x] `resolve_hybrid_backend(target)`
- [x] FTP → FTP_IN_PLACE；
- [x] SFTP → SFTP_STAGED；
- [x] Unknown Protocol 拒绝。

## Phase 2：Capability Probe

### TODO-0201：定义 Capability Schema

- [x] Dataclass；
- [x] JSON；
- [x] Size Limit；
- [x] Target Fingerprint；
- [x] Banner Hash；
- [x] Timestamp。

### TODO-0202：FTP FEAT

- [x] 读取 Feature；
- [x] 识别 MLST/MLSD；
- [x] 不依赖语言错误文本；
- [x] 缓存本次连接结果。

### TODO-0203：创建 Probe Root

- [x] `.git-deploy/ftp-probe/<uuid>`；
- [x] 显式确认；
- [x] 失败清理；
- [x] 不碰业务路径。

### TODO-0204：Binary Round-trip

- [x] STOR 随机二进制；
- [x] RETR；
- [x] SHA256；
- [x] Zero-byte；
- [x] Cleanup。

### TODO-0205：MLSD 类型

- [x] File；
- [x] Directory；
- [x] Empty Directory；
- [x] Unknown Type；
- [x] cdir/pdir。

### TODO-0206：Cross-directory Rename

- [x] Probe Root A → B；
- [x] Verify Content；
- [x] Cleanup。

### TODO-0207：Rename Replace

- [x] Existing Target A；
- [x] Source B；
- [x] RNFR/RNTO；
- [x] 验证 Target = B；
- [x] 验证 Source 消失。

### TODO-0208：Profile Store

- [x] Git Common Dir；
- [x] Atomic Local Write；
- [x] Corrupt Reject；
- [x] Fingerprint Match；
- [x] Banner Match。

### TODO-0209：Doctor CLI

- [x] `--probe-ftp-hybrid`；
- [x] 非交互要求 `--yes`；
- [x] 结果列表；
- [x] Unsupported Reason。

## Phase 3：Typed Scanner

### TODO-0301：FTP MLSD Entry

- [x] Name；
- [x] Type；
- [x] Size；
- [x] Modify；
- [x] Facts Normalize。

### TODO-0302：Stable Name

- [x] 复用 Stable Component；
- [x] cdir/pdir Skip；
- [x] Symlink Reject；
- [x] Unknown Reject。

### TODO-0303：Recursive Scan

- [x] File List；
- [x] Directory List；
- [x] Empty Directory；
- [x] Stable Sort；
- [x] Max Depth；
- [x] Max Entries。

### TODO-0304：Error Semantics

- [x] Permission != Missing；
- [x] MLSD Failure != Empty；
- [x] Timeout；
- [x] Connection Reset；
- [x] Retry；
- [x] Reconnect 后清除目录缓存。

## Phase 4：Remote Read/Write

### TODO-0401：FTP RETR Bounded

- [x] `read_file(max_bytes)`；
- [x] Streaming Limit；
- [x] Binary；
- [x] Missing；
- [x] Permission Error。

### TODO-0402：FTP Stage File

- [x] Internal Stage Path；
- [x] Parent MKD；
- [x] Binary Upload；
- [x] Progress；
- [x] Retry。

### TODO-0403：Stage Verify

- [x] RETR Stage；
- [x] SHA256；
- [x] Size；
- [x] Mismatch Fail；
- [x] No Publish on Mismatch。

### TODO-0404：Publish Staged File

- [x] RNFR；
- [x] RNTO；
- [x] Replace Existing；
- [x] Verify Final；
- [x] Stage Consumed。

### TODO-0405：RMD

- [x] Empty Directory Only；
- [x] Missing Idempotent；
- [x] Non-empty Failure；
- [x] Permission Error。

## Phase 5：Ownership

### TODO-0501：FTP Ownership Read

- [x] Typed File Check；
- [x] RETR；
- [x] Parse Existing Schema；
- [x] Identity；
- [x] Size Limit。

### TODO-0502：FTP Ownership Publish

- [x] Serialize；
- [x] Stage；
- [x] Verify；
- [x] Publish；
- [x] Re-read Final Hash。

### TODO-0503：State Loss Test

- [x] 删除 Local State；
- [x] Remote Ownership 继续删除；
- [x] Unknown Root 保留。

## Phase 6：Pending Resume

### TODO-0601：Pending Schema

- [x] Identity；
- [x] Phase；
- [x] Ownership Hash；
- [x] Local Manifest Hash；
- [x] Next State；
- [x] Size Limit。

### TODO-0602：Pending Publish

- [x] Stage；
- [x] Verify；
- [x] Replace；
- [x] Re-read。

### TODO-0603：Pending Read

- [x] Missing；
- [x] Corrupt；
- [x] Wrong Mapping；
- [x] Wrong Target；
- [x] Unknown Phase。

### TODO-0604：Resume Validation

- [x] Local Manifest Match；
- [x] Previous Ownership；
- [x] New Ownership；
- [x] State Fingerprint；
- [x] Local HEAD。

### TODO-0605：Phase Advance

- [x] PREPARED；
- [x] FILES_PUBLISHED；
- [x] PRUNED；
- [x] OWNERSHIP_COMMITTED；
- [x] STATE_COMPLETE。

### TODO-0606：Ownership Committed State Recovery

- [x] 保存 Marker Next State；
- [x] 不使用当前 HEAD；
- [x] 不信任当前 Build；
- [x] Cleanup。

### TODO-0607：Mismatch Fail Closed

- [x] Local Build Changed；
- [x] Ownership Unknown；
- [x] Pending Corrupt；
- [x] Doctor Manual Instructions。

## Phase 7：Planner

### TODO-0701：Backend in Plan

- [x] Plan Field；
- [x] Render；
- [x] Guarantees。

### TODO-0702：FTP Root Scan

- [x] Current/Old Direct Names；
- [x] Adoption；
- [x] Type Check；
- [x] Type Change Reject。

### TODO-0703：Mirror Plan

- [x] Upload All Local Files；
- [x] Create Missing Directories；
- [x] Orphan Files；
- [x] Orphan Directories。

### TODO-0704：Historical Directory Delete

- [x] Typed Recursive Scan；
- [x] File Deletes；
- [x] Deep RMD；
- [x] Top-level RMD。

### TODO-0705：Pending Resume Plan

- [x] Display Resume；
- [x] No Adoption for verified pending paths；
- [x] Exact Remaining Phases；
- [x] Confirm。

### TODO-0706：Delete Guard

- [x] Count；
- [x] Large Warning；
- [x] Delete All Warning；
- [x] Summary。

## Phase 8：Executor

### TODO-0801：Write PREPARED

- [x] Confirm 后；
- [x] 文件变更前；
- [x] Next State Freeze。

### TODO-0802：Stage All

- [x] Root Files；
- [x] Mirror Files；
- [x] Retry；
- [x] Verify；
- [x] No Prune。

### TODO-0803：Publish All

- [x] Stable Order；
- [x] Root；
- [x] Mirror；
- [x] Replace；
- [x] Retry Rules。

### TODO-0804：FILES_PUBLISHED

- [x] 所有 Current File 完成后推进；
- [x] 失败保持 PREPARED。

### TODO-0805：Prune Files

- [x] Root Historical；
- [x] Mirror Orphans；
- [x] Missing Idempotent；
- [x] Permission Fail Closed。

### TODO-0806：Prune Directories

- [x] Deepest First；
- [x] Empty Verify；
- [x] RMD；
- [x] Historical Top-level。

### TODO-0807：PRUNED

- [x] 所有 Delete/RMD 完成后推进。

### TODO-0808：Ownership

- [x] Publish；
- [x] Verify；
- [x] OWNERSHIP_COMMITTED。

### TODO-0809：State

- [x] Save Frozen Next State；
- [x] STATE_COMPLETE。

### TODO-0810：Cleanup

- [x] Remove Stage；
- [x] Remove Empty Stage Dirs；
- [x] Delete Pending；
- [x] Cleanup Failure Doctor。

## Phase 9：CLI / Workspace

### TODO-0901：Dry-run

- [x] Zero Connection；
- [x] FTP Backend Local Summary；
- [x] Remote Scan Hint。

### TODO-0902：Remote Plan

- [x] Ownership；
- [x] Pending；
- [x] Typed Recursive Scan；
- [x] Zero Write。

### TODO-0903：Normal Deploy

- [x] Profile Required；
- [x] Full Plan；
- [x] Confirm；
- [x] Pending Resume。

### TODO-0904：Workspace

- [x] All Local Prepare；
- [x] All Remote Plan；
- [x] Combined FTP/SFTP Backend；
- [x] Sequential Execute；
- [x] Partial Failure；
- [x] Resume。

### TODO-0905：Exit Codes

- [x] Capability Missing；
- [x] Remote Type Unsupported；
- [x] Pending Mismatch；
- [x] Resume Required；
- [x] FTP Scan Failed。

## Phase 10：Tests

### TODO-1001：Config Tests

- [x] FTP Hybrid Allowed；
- [x] FTP Commands Rejected；
- [x] Single Hybrid；
- [x] SFTP Regression。

### TODO-1002：Capability Tests

- [x] MLSD Missing；
- [x] Rename Replace Missing；
- [x] Cross-dir Missing；
- [x] Profile Match；
- [x] Profile Stale。

### TODO-1003：Scanner Tests

- [x] File；
- [x] Directory；
- [x] Empty；
- [x] Unknown；
- [x] Symlink；
- [x] Permission；
- [x] Depth；
- [x] Count。

### TODO-1004：Plan Tests

- [x] Root Upload；
- [x] Root Delete；
- [x] Mirror Upload All；
- [x] Orphan Delete；
- [x] Directory RMD；
- [x] Adoption；
- [x] Type Change Reject；
- [x] Unknown Root Ignore。

### TODO-1005：Resume Tests

- [x] PREPARED；
- [x] FILES_PUBLISHED；
- [x] PRUNED；
- [x] OWNERSHIP_COMMITTED；
- [x] STATE_COMPLETE；
- [x] Manifest Mismatch；
- [x] Ownership Mismatch；
- [x] Build Changed。

### TODO-1006：Failure Tests

- [x] Stage Upload；
- [x] Stage Verify；
- [x] Publish；
- [x] Delete；
- [x] RMD；
- [x] Ownership；
- [x] State；
- [x] Cleanup；
- [x] Ctrl-C；
- [x] Connection Reset。

### TODO-1007：Real FTP Integration

- [x] pyftpdlib MLSD；
- [x] Replace Rename；
- [x] Mixed Root；
- [x] State Loss；
- [x] Adoption；
- [x] Orphan Cleanup；
- [x] Resume；
- [x] Empty Directory；
- [x] Passive；
- [x] Active。

### TODO-1008：Real Server Compatibility

至少人工验证：

> 自动发布主线已由 pyftpdlib Passive/Active fixture 验证完成；以下真实服务器项按外部系统策略独立保留，不阻塞本地构建与发布。

- [x] pyftpdlib；
- [ ] vsftpd；
- [ ] Pure-FTPd 或 ProFTPD；
- [ ] 实际目标服务器。

## Phase 11：Docs / Release

### TODO-1101：README

- [x] FTP Hybrid；
- [x] Probe；
- [x] Guarantees；
- [x] Single Publisher；
- [x] No after_deploy；
- [x] Resume。

### TODO-1102：Migration

- [x] Incremental FTP → Hybrid；
- [x] `.deploy/`；
- [x] First Adoption；
- [x] State Loss；
- [x] Pending；
- [x] Unsupported Server。

### TODO-1103：Release Notes

- [x] v1.5.0；
- [x] No Config Schema Break；
- [x] Capability Requirements；
- [x] Known Limits。

### TODO-1104：Release Gate

- [x] Python 3.11；
- [x] Python 3.12；
- [x] Ruff；
- [x] ty；
- [x] Lock；
- [x] Wheel/sdist；
- [x] Isolated Install；
- [x] Real FTP；
- [x] SFTP Regression；
- [x] Native Regression。

---

# 28. 测试矩阵

## 28.1 能力

| 场景 | 预期 |
|---|---|
| MLSD 支持 | 允许 |
| MLSD 不支持 | Fail Closed |
| Rename Replace 支持 | 允许 |
| Rename Replace 不支持 | Fail Closed |
| Cross-dir Rename 不支持 | Fail Closed |
| RETR 内容不一致 | Fail Closed |
| Profile 缺失 | 要求 Probe |
| Profile Fingerprint 不一致 | 要求 Reprobe |

## 28.2 Mixed Root

远端：

```text
index.php
.env
app/
uploads/
index.html
assets/
```

确认：

- `index.php` 保留；
- `.env` 保留；
- `app/` 保留；
- `uploads/` 保留；
- `index.html` 更新；
- `assets/` Mirror。

## 28.3 Failure

| Phase | Online Current | Prune | Ownership | State | Rerun |
|---|---|---|---|---|---|
| Stage Fail | 旧/部分未变 | 不执行 | 旧 | 旧 | 重传 |
| Publish Fail | 部分新 | 不执行 | 旧 | 旧 | 重发 |
| Prune Fail | 新 | 部分 | 旧 | 旧 | 继续 |
| Ownership Fail | 新 | 完成 | 旧 | 旧 | 提交 |
| State Fail | 新 | 完成 | 新 | 旧 | Marker 恢复 |
| Cleanup Fail | 新 | 完成 | 新 | 新 | 清理 |

---

# 29. 人工验收流程

## 29.1 Probe

```bash
git-deploy doctor prod --probe-ftp-hybrid
```

验收：

- Profile 生成；
- Probe 临时内容清理；
- MLSD；
- Replace Rename；
- Binary Verify。

## 29.2 首次 Adoption

```bash
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

验收：

- 只 Adoption 当前同名前端路径；
- 未知后端路径不进入 Plan；
- Ownership 创建；
- Pending 清理。

## 29.3 Directory Orphan

第一次：

```text
assets/a.js
assets/old.js
```

第二次：

```text
assets/a.js
assets/new.js
```

验收：

- a.js/new.js 先发布；
- old.js 后删除；
- Ownership 不变；
- State 成功。

## 29.4 整个目录删除

第一次：

```text
old-assets/
```

第二次 Local 无。

验收：

- 递归 File Delete；
- Deep RMD；
- Top-level RMD；
- 未知其他目录保留。

## 29.5 State 丢失

删除 Local State。

验收：

- Remote Ownership 仍用于删除历史 Root/Directory；
- 不要求接管未知内容。

## 29.6 Publish 中断

在部分文件发布后中断。

验收：

- Pending PREPARED；
- 不 Prune；
- Ownership/State 不推进；
- 重跑继续；
- 不要求 `--full`。

## 29.7 Prune 中断

在删除部分 Orphan 后中断。

验收：

- Pending FILES_PUBLISHED；
- Ownership/State 不推进；
- 重跑继续删除；
- 当前文件保持可用。

## 29.8 State Save 失败

Ownership 已提交后模拟 State 失败。

验收：

- Pending OWNERSHIP_COMMITTED；
- 下次使用 Marker Next State；
- 不使用当前 HEAD；
- Cleanup 完成。

## 29.9 Local Build 变化

存在 Pending 后改变聚合目录。

验收：

```text
Fail Closed
Pending Manifest does not match current local deployment view
```

不自动合并两个部署。

---

# 30. 里程碑

## Milestone 1：FTP Capability

交付：

- Probe；
- Profile；
- MLSD；
- Rename Replace；
- Doctor。

验收：

- 可明确判断服务器是否支持 FTP Hybrid。

## Milestone 2：Read-only Plan

交付：

- Ownership；
- Pending Read；
- Typed Scan；
- Exact Plan；
- Adoption。

验收：

- `--remote-plan` 零写入并显示完整 Orphan。

## Milestone 3：In-place Execute

交付：

- Stage；
- Verify；
- Publish；
- Upload-first；
- Prune-last；
- Ownership；
- State。

验收：

- 成功部署收敛；
- 未知根内容保留。

## Milestone 4：Forward Resume

交付：

- Pending；
- Phase；
- Restart；
- State Recovery；
- Mismatch Fail Closed。

验收：

- 每个失败点重跑自然继续。

## Milestone 5：Workspace / Docs / Release

交付：

- Workspace；
- Real FTP；
- README；
- Migration；
- v1.5.0。

---

# 31. 发布验收

## 功能

- [x] FTP Target 可使用 `mode = "hybrid"`；
- [x] SFTP Hybrid 不回归；
- [x] FTP Incremental 不回归；
- [x] Unknown Root 保留；
- [x] Mirror Orphan 清理；
- [x] State Loss；
- [x] Adoption；
- [x] Forward Resume。

## 安全

- [x] MLSD 不可靠时拒绝；
- [x] Permission 不当 Missing；
- [x] Unknown Type 不删除；
- [x] Symlink 不跟随；
- [x] Ownership 严格身份；
- [x] Pending 严格身份；
- [x] Type Change 拒绝；
- [x] Commands 拒绝；
- [x] Delete Guard。

## 稳定

- [x] Upload-first；
- [x] Prune-last；
- [x] Ownership-last；
- [x] State-after-ownership；
- [x] Every Failure Rerun；
- [x] Passive/Active；
- [x] Large Directory；
- [x] Connection Retry；
- [x] No Temp Leak after Success。

---

# 32. 最终建议

v1.5.0 应只增加一个主要能力：

> **FTP In-place Hybrid**

最终模型：

```text
Local Aggregation
    ↓
Single Hybrid
    ↓
Protocol Backend

SFTP:
    Stage / Backup / Swap / Recovery

FTP:
    File Stage / Verify / Publish
    Upload-first / Prune-last
    Ownership / Forward Resume
```

FTP 版本的目标不是复制 SFTP 的目录事务，而是解决真实问题：

- 前端 Mirror Directory 中的孤儿文件；
- 旧 Root File；
- 旧整个前端目录；
- State 丢失；
- 混合根目录中的未知后端内容保护。

建议版本：

```text
v1.5.0
```

并严格控制范围：

```text
必须：
    MLSD
    Rename Replace
    Remote Ownership
    Forward Resume
    Upload-first / Prune-last

不做：
    FTP Directory Swap
    FTP Rollback
    FTP after_deploy
    多发布器
    多 Hybrid 同根
    Full Root Reconcile
```

这套方案可以在不破坏 v1.4.3 稳定基线的前提下，为只有 FTP 权限的项目提供真正可用、可恢复、边界明确的 Hybrid 部署能力。
