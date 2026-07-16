# Native OpenSSH、WSL 与 1Password SSH Agent

配置 `ssh_host_alias` 后，git-deploy 完整委托当前环境中的系统 `ssh` 和 `sftp`。工具不调用 1Password API、不读取私钥、不修改 `SSH_AUTH_SOCK`，也不启用 Agent Forwarding。

## WSL 前置检查

```bash
which ssh       # 应为 /usr/bin/ssh
which sftp      # 应为 /usr/bin/sftp
test -n "$SSH_AUTH_SOCK"
ssh project-prod
```

最后一条命令必须已经能够通过用户现有的 WSL Agent Bridge 唤起 1Password/Windows Hello。git-deploy 不负责安装或配置该 Bridge。

## OpenSSH Config

```sshconfig
Host project-prod
    HostName 192.0.2.10
    User deploy
    Port 22
    IdentityFile ~/.ssh/1password/project-prod.pub
    IdentitiesOnly yes
```

`Include`、`Match`、`ProxyJump` 和 `ProxyCommand` 均由 OpenSSH 自己解析。例如：

```sshconfig
Host project-gateway
    HostName gateway.example.com
    User deploy

Host project-prod
    HostName 10.0.0.10
    User deploy
    ProxyJump project-gateway
```

项目配置只引用 Alias：

```toml
[targets.prod]
protocol = "sftp"
ssh_host_alias = "project-prod"
remote_root = "/www/wwwroot/project"
```

如果使用非默认配置文件，可增加 `ssh_config_file = "~/.ssh/project-config"`。Alias Target 不允许混用 Paramiko 的 `host`、`username`、`port`、`password_env`、`key_file`、`known_hosts_file`、`use_ssh_agent` 或 `strict_host_key_checking`。

## 单仓人工验收

```bash
git-deploy doctor prod
git-deploy prod --dry-run
git-deploy prod --yes
git-deploy prod --yes
```

确认：

1. Doctor 显示 Native OpenSSH、`/usr/bin/ssh`、`/usr/bin/sftp`、Alias 和实际 Endpoint；
2. 首次正式部署只触发一次 Windows Hello；
3. 多文件上传不重复认证；
4. State 只在全部文件成功后提交；
5. 第二次无变化部署不连接、不触发生物认证。

`--yes` 只跳过 git-deploy 自己的确认，不设置 `BatchMode=yes`，因此不会禁用 Agent 或生物认证交互。

## 多仓人工验收

Workspace 内多个仓库使用同一 Alias 时执行：

```bash
git-deploy prod --yes
```

确认只建立一个 ControlMaster、只授权一次，然后按配置顺序部署。若 api 成功、web 失败，则后续仓库不执行；重跑后 api 为 No-op，web 继续并最终收敛。

## 故障排查

- `requires the system 'ssh' executable`：在 WSL 安装 OpenSSH Client，并确认 `which ssh`。
- `requires a POSIX ssh executable`：PATH 命中了 Windows `ssh.exe`；调整 WSL PATH 使用 `/usr/bin/ssh`。
- `cannot resolve SSH alias`：先运行 `ssh -G ALIAS` 检查 Config/Include。
- `OpenSSH authentication failed`：直接运行 `ssh ALIAS` 查看 Host Key、Agent、Proxy 或网络错误。
- ControlPath 太长时，工具自动回退到当前 UID 专属、权限为 `0700` 的短临时目录，并在命令结束时清理随机子目录。
- `target ... already being deployed`：另一个进程持有 common-dir Target Lock；等待它完成，不要删除锁文件绕过。

## 安全边界

- ControlMaster 只存在于当前 git-deploy 命令生命周期，`ControlPersist=60` 仅用于短暂进程收尾；
- common-dir 路径可用时 Socket 存放在 `<git-common-dir>/git-deploy/ssh/`；过长时使用 `/tmp/git-deploy-<uid>/<hash>/`，所有目录均为 `0700`；
- 不输出 Agent Socket、私钥、密码、1Password Item URI 或 Windows 用户信息；
- 不自动设置 `ForwardAgent yes`；
- Windows Hello/1Password 属于用户现有 SSH 环境，自动测试使用隔离临时密钥，不触碰真实 Vault。
