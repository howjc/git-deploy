# 从手工 FTP/SFTP 迁移到 v1-lite

1. 把当前手工构建命令按顺序写入 `[build].steps`。
2. 把未进入 Git 的 `dist/`、`build/`、`vendor/` 配置为 `[[outputs]]`。
3. 在 `[source]` 中排除测试、日志和运行时目录，并确认 `.env`、uploads、证书与私钥保持保护。
4. 配置一个 `[targets.NAME]`，FTP 密码只通过 `password_env` 提供。
5. 运行 `git-deploy --dry-run`，逐项核对 Upload/Delete 清单。
6. 首次使用 `git-deploy --yes`。没有 state 时工具自动完整上传，不需要 bootstrap。

工具不会删除未知远端文件。确认稳定后，再停止原有脚本管理同一远端路径，避免两个发布器并发覆盖。
