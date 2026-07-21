# Release Notes v1.7.0

## 变更

### FTP Hybrid Bootstrap

- 新增 `git-deploy bootstrap`：在 Project 与 Workspace 下一次性初始化全部符合条件的 FTP Hybrid Target。
- 只读 Preflight 后输出统一 Plan，批次只确认一次；顺序执行 CREATE_ROOT / PROBE / REPROBE，单目标失败后 best-effort 继续，Summary 汇总；任一失败 exit 非 0。
- 支持 `--yes`、`--force`、`--no-create-root` 与可选 positional Target 过滤。
- 跳过 SFTP、非 Hybrid、被过滤的 Target，并输出 SKIP 原因。
- 不执行 Build、业务上传、Adoption、Ownership、Pending、Deployment State；密码不打印；Remote Root Alias Gate 与 Pending 检测 Fail Closed。
- Doctor 与 Bootstrap 共用 `probe_and_save_ftp_hybrid_capabilities` / `inspect_capability_profile`。

### FTP Hybrid P2 加固

- `OPTS UTF8 ON` 仅将明确“命令不支持”的 500/501/502/504 视为 always-on UTF-8；其它永久 5xx Fail Closed。
- Banner 规范化改为字段脱敏（用户数、本地时间），保留行结构与稳定后缀；规范化结果为空时拒绝稳定身份缺失。

## 迁移

- 从 v1.6.x 升级后，可用一条命令替代逐 Target 的 Doctor Probe：

```bash
git-deploy bootstrap --yes
# 强制重新探测（Schema / Banner 迁移）
git-deploy bootstrap --force --yes
```

- `init` 仍只生成本地配置模板，不连接远端。
- 首次正式部署仍需：

```bash
git-deploy prod --remote-plan --full
git-deploy prod --full --yes
```
