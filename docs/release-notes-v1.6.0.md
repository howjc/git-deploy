# git-deploy v1.6.0

v1.6.0 在 v1.5.3 的协议与状态机稳定基线上新增 Transfer Rate Visualization。该版本只增加部署可观测性，不改变 Planner、Ownership、Pending、State Schema 或远端写入顺序。

## 实时上传进度

- 普通 Source/Incremental、SFTP Hybrid Stage、FTP Hybrid Stage/Restage 共用一个部署级统计器。
- TTY 使用 1.5 秒滑动窗口显示当前速率，并以 250ms 为最短刷新间隔；`--verbose` 不会退化为逐 Block 输出。
- 非 TTY 只输出每个文件的完成行和最终 Summary，适合 CI 与日志采集。
- 字节和速率使用 IEC 单位；大样本的最终平均同时显示 decimal Mbps。

## 统计口径

- `payload`：成功逻辑文件按最终路径去重后的大小；同一文件重试不重复累计。
- `wire bytes`：所有 Upload Attempt 的正向字节增量，包括失败的部分上传、重传和 FTP Restage。
- `active time`：Upload Attempt 的活动时间之和，排除 Build、Freeze、Plan、重试等待、FTP RETR、Rename、Delete/RMD 和远端命令。
- `average upload`：`wire bytes / active time`，不是完整部署吞吐量。
- 小于 1 MiB 或 1 秒的样本标记 `sample too small`，避免展示误导性的 Mbps 精度。
- Delete-only/No-op 不显示 Summary；失败部署不显示成功 Summary；Zero-byte 文件安全计数且不会除零。

## Retry 与 Hybrid

- Upload Retry 在下一次 Attempt 前关闭旧计时，保留已发送字节并递增 Retry Counter。
- SFTP Hybrid 的内部 Stage 路径对用户显示为最终逻辑路径。
- FTP Hybrid 只统计 Stage/Restage Upload；RETR 完整性校验、Rename 与 Final RETR 明确不计入上传速率。
- Workspace 为每个 Repository 独立输出 `[name] TRANSFER SUMMARY`，不合并不同协议或服务器的速率。

## 验证边界

- 自动门禁覆盖 Python 3.11/3.12、Ruff、ty、构建与隔离 wheel 安装，以及 Fake Transport、本机 FTP、Paramiko、Native OpenSSH、SFTP Hybrid 和 FTP Hybrid 回归。
- 实际外部 FTP/SFTP 目标的网络速率验收是可选人工增强，需要隔离测试目录、测试账号和明确授权；本次发布不读取或记录真实凭据，也不以该人工项阻塞自动主线。
