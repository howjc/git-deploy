# git-deploy v1.0.0-beta.1

Beta 在 Alpha 的完整 v1-lite 功能上收紧了计划与远端身份绑定，并补充重复日常部署验证。

## Alpha 反馈与修复

- 源码上传现在从计划捕获的精确 commit 读取 blob。即使用户确认期间 HEAD 移动，也不会上传新 HEAD 的内容再把旧 commit 写入 State。
- `ssh_host_alias` 在计划阶段通过 `ssh -G` 解析实际 host、username、port 和存在的 IdentityFile；解析结果与非敏感 target fingerprint 一起冻结，确认后 SSH Config 改动不会把计划发送到另一台服务器。
- SFTP/FTP 逐文件重试会关闭失效连接并重新连接、重新确认远端根目录，覆盖真实网络中断后原连接不可复用的情况。
- OpenSSH 自动输出但本地不存在的默认 IdentityFile 不再传给 Paramiko，避免阻止 SSH Agent 认证。

## 重复使用验证

新增持久远端 soak，连续覆盖：首次完整上传、无变化、源码与 output 同时修改、源码 rename（delete + upload）、旧 hash asset 删除、新 asset 上传、State 丢失后的 `--full` 重建，以及未知远端文件/Uploads 始终保留。

## Alpha 升级说明

Alpha State 使用配置文本形式的 SSH target fingerprint；Beta 改为实际解析后的端点身份。首次从 Alpha 对 SFTP target 运行 Beta 时，若看到 target identity changed，请核对解析后的服务器后使用一次 `--full` 重建轻量 State。

## 验证范围

- Python 3.11 / 3.12 全套测试；
- Ruff、ty、wheel/sdist 与隔离安装；
- 真实本机 FTP 与容器化 OpenSSH/SFTP；
- 真实 pnpm Node、Composer PHP、PHP+Node 混合构建；
- 重复部署收敛与故障重试。
