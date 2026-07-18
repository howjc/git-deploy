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

此命令会明确提示并只在 `.git-deploy/ftp-probe/<随机 ID>` 创建、读取、Rename、删除临时文件，完成后清理。服务器必须通过 MLSD 类型、二进制零/非零文件回读、跨目录 Rename、Rename Replace、DELE 与 RMD。Profile 缺失、损坏、Target/Banner 变化或运行期能力错误时重新执行；不要改用 LIST/NLST 或关闭校验。

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

FTP backend 总是先发布当前文件，再删除受管孤儿；Ownership 最后提交，Local State 在 Ownership 之后保存。中断后继续运行普通 `--remote-plan` 和部署即可。Pending Marker 会要求 Project、Mapping、Remote、Target Fingerprint、HEAD、Local Manifest 与旧/新 Ownership Hash 完全匹配；Build 已变化时先恢复原构建视图或人工检查 Pending，工具不会合并两个部署。

State 丢失不会丢失删除所有权；Remote Ownership 仍会清理历史受管内容。不要手工删除 `.git-deploy`。

## 6. 已知边界

- 只支持单发布器；部署期间不要并发手工 FTP、CI 或面板修改受管路径。
- 无目录原子 Swap、旧目录树回滚和 FTP `after_deploy`。
- File→Directory / Directory→File 必须拆成两次部署：先移除旧类型并确认删除，再添加新类型并用 `--full` 审阅。
- vsftpd、Pure-FTPd/ProFTPD 与实际目标服务器应在上线前人工执行 Doctor Probe；自动门禁使用本地 pyftpdlib fixture，不需要真实账号或生产凭据。
