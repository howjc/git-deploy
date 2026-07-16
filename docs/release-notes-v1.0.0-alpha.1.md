# git-deploy v1.0.0-alpha.1

这是 v1-lite 破坏性重构的首个 Alpha：项目从状态化发布/回滚系统回归为 Git 感知型本地构建与 FTP/SFTP 文件同步工具。

## 主要变化

- 默认 `git-deploy [TARGET]` 一条命令执行构建、差异计算、确认、同步和轻量 state 提交。
- 公开入口只保留默认 Deploy、`build` 和 `doctor`。
- 源码按上次成功 commit 到当前 `HEAD` 的 `--no-renames` 差异同步；上传字节固定为 committed HEAD。
- `dist/`、`vendor/` 等 outputs 用 SHA256 manifest 做增量上传和安全删除。
- SFTP 支持 SSH Config、SSH Agent、Host Key、临时文件替换；FTP 支持密码环境变量和被动模式。
- 每个 target 的 state 隔离在 `.git/git-deploy/<target>.json`，只在全部成功后原子更新。

## 不兼容变化

v1 不读取 v0.3 配置、Expected State、CAS、transactions 或 manifests。旧用户需按 `deploy.example.toml` 手工创建短配置；旧版保留在 `legacy/v0.3` 和 v0.3.x tags。

## 验证

- 单元与 Fake Transport 编排测试；
- 本机真实 FTP；
- 容器化真实 OpenSSH/SFTP；
- pnpm Node、Composer PHP、PHP+Node 混合构建链；
- Ruff、ty、wheel/sdist 构建与隔离安装冒烟。

这是 Alpha 版本，建议先对非生产 target 使用 `--dry-run` 和 `--full` 验证路径所有权。
