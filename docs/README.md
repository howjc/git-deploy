# Documentation

面向用户与贡献者的文档入口。日常用法以仓库根目录 [README.md](../README.md) 为准；贡献门禁见 [CONTRIBUTING.md](../CONTRIBUTING.md)。

## Guides

| 文档 | 说明 |
|------|------|
| [Product scope](scope.md) | v1-lite 产品边界：做什么、明确不做什么 |
| [Migrate from manual FTP](migrate-from-manual-ftp.md) | 从手工 FTP 迁到 git-deploy |
| [Migrate to Hybrid Output](migrate-to-hybrid-output.md) | 启用 Hybrid 聚合产物发布 |
| [Migrate to FTP Hybrid](migrate-to-ftp-hybrid.md) | FTP In-place Hybrid 专用步骤 |
| [Native OpenSSH / WSL](native-openssh-wsl.md) | WSL、OpenSSH Config、1Password、Windows Hello |

## Architecture decisions (ADR)

| ADR | 说明 |
|-----|------|
| [Hybrid Output](adr/hybrid-output.md) | SFTP Staged Hybrid 边界与所有权 |
| [FTP Hybrid](adr/ftp-hybrid.md) | FTP In-place Hybrid 能力与限制 |
| [Physical target lock](adr/physical-target-lock.md) | 跨仓本机物理目标锁 |

## Release notes

按版本分文件存放在 [releases/](releases/)。最新版：

- [v1.8.1](releases/v1.8.1.md)

完整列表见 [releases/README.md](releases/README.md)。
