# git-deploy v1.6.1

v1.6.1 收紧 v1.6.0 的传输测量契约，使 Native OpenSSH 展示、Streaming 计时边界和输出故障语义与真实能力一致。本版本仍是纯可观测性修正，不改变 Planner、Ownership、Pending、Recovery、State Schema 或远端写入顺序。

## Native OpenSSH 诚实测量

- 新增 `STREAMING` 与 `COARSE` 两种 Transport 测量能力；FTP/Paramiko 使用 Streaming，Native OpenSSH 使用 Coarse。
- Native Batch 上传不再显示虚假的实时百分比或滑动速率，而是显示 `transferring (Native batch)`。
- 单文件完成行明确标注 `avg publish ... (coarse)`；Summary 标注 `coarse Native batch`、`reported bytes >=` 和失败部分字节可能未报告。
- Native 的 Active Time 覆盖 Batch、chmod 与安全发布区间，仅表示粗粒度发布吞吐量，不宣称是纯网络上传速率。

## Fail-open Rendering

- Progress 和 Summary 输出统一经过安全写入层，捕获 Broken Pipe、Stream I/O、Unicode 编码和关闭 Text Stream 错误。
- 首次输出失败后只禁用本次部署的后续显示；Payload、Attempt Bytes、Active Time 与 Retry 统计继续工作。
- 展示故障不会传播到 Transport Callback，不会触发重传，也不会改变已成功的 State、Ownership、Recovery 或命令退出结果。

## Streaming 计时与命名

- Callback 注册只建立 `registered` Attempt；第一次 Transport Callback 才进入 `active`，完成后进入 `completed`。
- FTP 与 Paramiko 都在 Parent Setup 完成、即将执行 STOR/put 时显式发送 `(0, total)`，因此 Parent MKD/Probe 不进入 Active Time；Native 保持相同起点信号。
- `wire bytes` 改名为 `attempt bytes`，明确这是应用层 Callback 报告值而非物理链路字节。
- Rate Formatter 直接从原始速率选择 IEC 单位，移除先舍入为一位再伪装为两位的小数精度问题。

## 验证边界

- 本地自动门禁覆盖 Python 3.11/3.12、Ruff、ty、wheel/sdist 构建、隔离 wheel 安装，以及 FTP、Paramiko、Native OpenSSH、SFTP Hybrid、FTP Hybrid 和故障 Stream 回归。
- 实际外部 FTP/SFTP 100MiB 与小文件 Canary 仍是需要测试账号、隔离目录和人工确认的可选增强；本次发布不读取或记录真实凭据。
- GitHub Actions 是否真正获得 Runner 以对应 Release/Tag 的远端检查结果为准；不以本地通过替代“CI Verified”声明。
