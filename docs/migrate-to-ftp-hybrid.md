# 从 FTP Incremental 迁移到 FTP In-place Hybrid

FTP Hybrid 适用于前端产物铺到混合项目根目录、需要在 Local State 丢失后仍安全清理历史 Root File 与 Mirror Directory 孤儿的项目。普通 FTP Incremental 没有变化；不需要远端所有权和目录 Mirror 时可继续使用原配置。

## 1. 建立本地聚合视图

把所有前端构建结果聚合到一个被 Git 忽略的目录，例如 `.deploy/frontend-root/`。直接文件会作为 Root File 管理，直接目录会作为完整 Mirror 边界管理。不要聚合 `.env`、上传目录、后端代码或其他运行时内容。

## 2. 切换唯一 Hybrid Mapping

```toml
project_id = "github.com/example/project"

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"

[targets.prod]
protocol = "ftp"
host = "ftp.example.com"
username = "deploy"
password_env = "DEPLOY_FTP_PASSWORD"
remote_root = "/public_html"
```

FTP Target 不能配置 `after_deploy`。Hybrid 不能显式配置 `delete_removed`，且一个项目只能有一个 Hybrid Mapping。

## 3. 显式探测服务器

```bash
git-deploy doctor prod --probe-ftp-hybrid
```

此命令先用 Root MLSD 检查 `.git-deploy` 的远端未知别名；通过后才会在 `.git-deploy/ftp-probe/<随机 ID>` 创建、读取、Rename、删除临时文件，并清理本次 Probe Root。服务器必须广告 `UTF8`；客户端会尝试 `OPTS UTF8 ON`，Pure-FTPd 等 always-on 实现对 OPTS 返回 5xx 时仍视为 UTF-8 已启用。随后必须证明中文文件名、NFC/NFD 两个精确名称、大小写变体可同时存在且能独立 MLSD/RETR/Delete/Rename，同时通过二进制零/非零回读、跨目录 Rename、Rename Replace、DELE 与 RMD。Profile 缺失、损坏、Target/Banner 变化、Schema 1/2 或运行期能力错误时重新执行；`--probe-ftp-hybrid` 本身会明确覆盖 Schema 3 Profile，不再需要 `--reprobe`。不要改用 LIST/NLST 或关闭校验。

## 4. 首次审阅与 Adoption

```bash
git-deploy prod --dry-run
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```

`--full` 只接管当前本地同名 Root File / Mirror Directory。`index.php`、`.env`、后端目录、上传目录与其他未知根内容不会进入计划。部署成功后 Ownership 写入 `.git-deploy/hybrid/<mapping>.json`。

## 5. 日常部署与 Forward Resume

```bash
git-deploy prod --remote-plan
git-deploy prod --yes
```

FTP backend 总是先发布当前文件，再删除受管孤儿；Ownership 最后提交，Local State 在 Ownership 之后保存。`PREPARED`、`FILES_PUBLISHED`、`PRUNED` 中断后继续运行普通 `--remote-plan` 和部署；Schema 2 Marker 会要求 Project、Mapping、Remote、Target Fingerprint、HEAD、Local Manifest、稳定的 Source/Incremental Operation 与配置策略、Previous State，以及阶段对应的 Ownership Hash 全部匹配。网络重试会在新 Session 的任何业务操作前重新完成 Banner/FEAT/`OPTS UTF8 ON`。`FILES_PUBLISHED` 恢复不会再次执行普通 Source/Incremental 队列。进入 `OWNERSHIP_COMMITTED` 或 `STATE_COMPLETE` 后只运行 `git-deploy prod --recover`：包括 `--remote-plan` 在内的非 Dry-run 会在 Build/Plan 前拒绝并提示 Recovery；恢复路径不 Build、不读取当前 State、不扫描当前 Hybrid，并始终把 Marker 中冻结的 State 写入当前 clone 后再清理。

State 丢失不会丢失删除所有权；Remote Ownership 仍会清理历史受管内容。不要手工删除 `.git-deploy`。

## 6. 已知边界

- 只支持单发布器；部署期间不要并发手工 FTP、CI 或面板修改受管路径。
- 无目录原子 Swap、旧目录树回滚和 FTP `after_deploy`。
- File→Directory / Directory→File 必须拆成两次部署：先移除旧类型并确认删除，再添加新类型并用 `--full` 审阅。
- Source、Incremental、Hybrid Direct、历史受管根与 `.git-deploy` 共用 NFC + casefold 根命名空间；Remote Plan 会把这些根与远端所有 Direct Entries 比对。精确同名仍按 Adoption/Ownership 处理，`Assets`/`assets`、NFC/NFD 和 `.GIT-DEPLOY`/`.git-deploy` 等异形在写前拒绝；无关未知根不会递归或删除。
- vsftpd、Pure-FTPd/ProFTPD 与实际目标服务器应在上线前人工执行 Doctor Probe；自动门禁使用本地 pyftpdlib fixture，不需要真实账号或生产凭据。
