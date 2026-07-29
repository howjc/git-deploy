# git-deploy v1.2.2

v1.2.2 是 v1.2.1 深度审计后的轻量收口版本，聚焦 FTP 大批量删除、幂等收敛和 Workspace build 的纯本地边界。

## FTP 删除性能与兼容性

- FTP 连接内按父目录缓存完整 `NLST` 结果，百个以上 Hash Asset 删除只扫描父目录一次。
- 成功上传、删除和新建目录会同步维护已加载缓存；重连和关闭连接会清空缓存。
- 父目录已被人工删除时，通过可访问祖先目录确认缺失并将删除视为幂等成功。
- 部分 FTP Server 对空目录返回 `550` 时，通过成功 `CWD` 确认父目录存在并自然收敛。
- 明确的 Permission/Access 错误仍然 fail closed，不会跳过删除后推进 State。

## 纯本地 Workspace Build

- `git-deploy build` 只加载每仓配置并顺序执行构建，不再解析 SSH Alias、探测 Native OpenSSH 工具、检查 Git 或比较远端 Root。
- build 不需要 Workspace 默认 Target；用户显式传入 Target 时仍会在首个 Build 前验证每仓都配置了该名称。
- 部署和 Doctor 保持原有完整远端预检与所有权门禁。

## 清理与边界说明

- Native OpenSSH 认证阶段被 Ctrl-C 中断时，会清理刚创建的私有 Control Socket 随机目录。
- 文档明确连接在 upload 与 publish 之间死亡时可能残留远端随机临时文件，且工具不会越权扫描删除未知文件。
- 跨独立仓库命令的本机物理目标锁继续暂缓，决策与未来触发条件记录在 ADR 中。

## 验证

- FTP 120 文件同目录批量删除、缓存上传/删除一致性、缺失父目录、空目录 `550` 与权限失败；
- Workspace build 在无默认 Target、无 SSH 工具、无 Alias 解析、远端 Root 重叠时仍可纯本地完成；
- OpenSSH ControlMaster 启动阶段 Ctrl-C 后私有目录清理；
- Python 3.11/3.12、Ruff、ty、wheel/sdist 构建和隔离安装。
