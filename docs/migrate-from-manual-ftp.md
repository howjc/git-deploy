# 从手工 FTP/SFTP 发布迁移

这份流程的目标不是“接管未知远端”，而是先证明 Git revision、受管路径和远端
现状一致，再建立可信 current state。全程不要把密码、私钥或 token 写进配置、
命令记录或验收报告。

## 1. 划分受管与保护路径

列出真正由 Git 发布的目录，以及必须保留在服务器上的运行时内容。典型保护项
包括 `.env`、私钥/证书、上传目录、日志、缓存和会话。将前者写入 `include`，
将后者写入 `exclude`/`protected`。不确定的路径先保护，不能先自动 adopt。

## 2. 找到可信 Git revision

从最近一次手工发布记录、制品或文件 hash 找到与远端一致的 commit。先在本地用
`git show COMMIT:path` 核对关键文件，再运行：

```bash
git-deploy doctor application --remote dev
git-deploy state bootstrap application --revision COMMIT --remote dev --dry-run
git-deploy state bootstrap application --revision COMMIT --remote dev --yes
git-deploy state verify application --remote dev --check-remote
```

如果无法证明一致，不要 bootstrap 这个 commit。`--empty` 只用于确认所有受管路径
都不存在的全新目录。

## 3. 先演练 dev

选择一个可识别、易回滚的小改动：

```bash
git-deploy plan application --remote dev
git-deploy deploy application --remote dev --dry-run --check-remote
git-deploy deploy application --remote dev --yes
git-deploy history application --remote dev
git-deploy state verify application --remote dev --check-remote
```

随后执行一次 latest rollback，再部署回来，确认 bytes、mode、owner/group、health
和 generation 都符合预期。

## 4. 迁移生产

生产使用独立 remote、state 和明确的 `risk = "production"`。重新验证可信 revision，
不要直接复制 dev state。先 doctor/remote verify/dry-run，再做小范围发布。

旧 FTP 账号可以在观察期作为人工紧急通道保留，但使用后必须停止自动 deploy，
记录人工改动并重新核对 current；不要让自动发布覆盖未知内容。稳定观察后再按组织
流程撤销或收紧旧账号权限。
