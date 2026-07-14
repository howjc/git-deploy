# git-deploy 故障恢复手册

总原则：先保留证据，再判断远端是 before、after 还是第三种内容。不要删除
`target.lock`、transaction journal、state、CAS、manifest 或 backup；不要在 open
transaction 上继续 deploy/rollback。

## 通用第一步

```bash
git-deploy doctor application
git-deploy state inspect application
git-deploy state recover application
```

以上命令默认本地只读。需要观察远端时再运行 `doctor --check-remote` 或
`state verify --check-remote`。报告只记录 transaction ID、stage、路径和 hash，
不要复制文件内容或凭据。

## 网络断开或连接失败

先确认本地是否已有 open transaction。没有 remote mutation 证据时修复网络/host
key 后重试 doctor；有 transaction 时先执行只读 recover inspection。不要把网络
恢复等同于事务已恢复。

## 权限、owner/group 或原子 rename 失败

核对 remote root、父目录写权限、临时文件权限和配置的 owner/group。权限修复前
不要反复 deploy。若 journal 为 `remote_mutating`，检查 recover 决策；工具不能证明
一致时必须人工核对列出的 hash。

## Hook 或 health 失败

文件通常会自动恢复，但 hook 本身产生的外部副作用无法自动回滚。先核对 current、
history 和 open transaction，再由应用负责人检查服务/cache/队列。不要在未知服务
状态上直接再次发布。

## Open transaction

`state recover PROJECT` 只显示决策；确认输出和备份完整后，才使用：

```bash
git-deploy state recover application --execute --yes
```

`finalize` 只用于可证明远端等于 after；`restore` 只用于可证明可恢复 before；
`manual_recovery_required` 禁止自动覆盖。

## State、manifest 或 backup 损坏

运行 doctor 获取具体路径。backup hash 不匹配时 rollback 会在远端 I/O 前停止。
从可信离线备份恢复损坏文件，或由负责人制定人工恢复；不要手工改 JSON hash、伪造
generation、清空目录或删除坏记录来让门禁通过。

## 何时人工介入

以下任一情况必须人工介入：远端是 before/after 之外的第三种内容、backup 缺失或
hash 不符、identity/policy/generation 不一致、hook 有不可逆副作用、自动 restore
也失败。保留完整 state 目录副本和只读检查结果，再决定恢复远端还是修复本地证据。
