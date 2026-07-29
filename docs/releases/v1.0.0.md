# git-deploy v1.0.0

v1.0.0 完成 v1-lite 破坏性重构：`git-deploy` 从状态化发布/回滚平台回归为个人日常使用的 Git 感知型本地构建与 FTP/SFTP 文件同步工具。

## 日常工作流

```bash
git-deploy              # 默认 target：构建、计划、确认、同步、提交 State
git-deploy prod --yes   # 指定环境并非交互执行
git-deploy --dry-run    # 构建并预览，不连接、不写 State
git-deploy --full       # 全量覆盖受管内容并重建轻量 State
git-deploy build        # 只执行可信本地 Build Steps
git-deploy doctor prod  # 检查配置、Git、构建命令、State 与远端根目录
```

## 核心能力

- 源码使用 `LAST_COMMIT..HEAD`、`--no-renames` 计算 Add/Modify/Delete，上传字节固定来自计划捕获的精确 commit。
- 未提交工作区内容默认只警告且不会上传；可配置 clean-worktree 强制门禁。
- `dist/`、`build/`、`vendor/` 等 outputs 使用本地 SHA256 manifest 做新增、修改和安全删除。
- SFTP 支持 OpenSSH Config/alias、SSH Agent、Host Key、目录创建、临时文件安全替换和断线重连。
- FTP 支持密码环境变量、Passive Mode、Binary Upload、目录创建、幂等删除和断线重连。
- 每个 target 的 State 隔离在 `.git/git-deploy/<target>.json`，只在全部远端操作成功后原子提交。
- 未知远端文件永不删除，`.env`、uploads、runtime、证书和私钥规则始终受保护。

## 破坏性变化

v1 不读取 v0.3 配置或 State。Expected State、Generation、CAS、Transaction、Deployment Manifest、History、Verify、Recover、Rollback、Docker Build、1Password Build Provider、FTPS 与远端 Hook 均已删除。旧实现保留在 `legacy/v0.3` 分支和 v0.3.x tags。

## 验证证据

- Python 3.11 / 3.12 全套自动测试；
- Ruff、ty、wheel/sdist 构建及隔离安装；
- 本机真实 FTP、容器化 OpenSSH/SFTP；
- 完整 Planner → Deployer → State 流水线在 FTP/SFTP 上的首次与增量部署；
- pnpm Node、Composer PHP、PHP+Node 混合项目真实构建链；
- 多轮重复部署 soak、部分失败后重跑收敛、State 丢失后的 full 重建；
- GitHub Actions Python 3.11 / 3.12 门禁。

## 已知边界

- FTP 不承诺原子替换或 POSIX 权限语义；
- SFTP 暂不支持 ProxyJump/ProxyCommand；
- 不扫描或校验 Remote Drift；
- 回滚使用 Git revert/checkout 后重新部署；
- 不处理数据库、消息队列、服务重启或其他文件同步之外的副作用。
