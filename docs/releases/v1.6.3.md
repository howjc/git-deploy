# Release Notes v1.6.3

## 变更

- FTP Hybrid Capability Profile 的 server banner 指纹在哈希前剥离 Pure-FTPd 会话易变字段（在线用户数、`Local time is now …`），避免每次连接因欢迎语时钟变化而要求重新 `--probe-ftp-hybrid`。
- 服务器软件/稳定欢迎语变化仍会使 fingerprint 失效并要求重新 Probe。

## 迁移

- 从 v1.6.2 升级后，每个 FTP Hybrid Target 需**重新执行一次** `git-deploy doctor TARGET --probe-ftp-hybrid`（旧 Profile 使用未规范化 banner hash）。
