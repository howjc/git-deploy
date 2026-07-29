# Release Notes v1.6.2

## 变更

- FTP Hybrid UTF-8 会话协商兼容 Pure-FTPd：服务器在 FEAT 中广告 `UTF8` 但对 `OPTS UTF8 ON` 返回永久 5xx（如 `504 Unknown command`）时，仍启用客户端 `encoding=utf-8`，不再 Fail Closed。
- 路径语义仍由 `doctor --probe-ftp-hybrid` 的中文名 / NFC-NFD / 大小写探测证明；缺少 FEAT `UTF8` 或临时性 OPTS 错误仍会失败。

## 验证

- 单元测试覆盖 always-on OPTS 拒绝、临时 OPTS 失败与 FEAT 缺失。
- 针对宝塔 Pure-FTPd 的 Hybrid 部署需先执行 `git-deploy doctor TARGET --probe-ftp-hybrid`。
