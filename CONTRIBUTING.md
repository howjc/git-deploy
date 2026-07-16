# Contributing to git-deploy

## v1-lite 产品边界

`git-deploy` 只负责：本地构建、Git 源码差异、output SHA256 差异和 FTP/SFTP 文件同步。

新增能力必须直接缩短个人日常部署流程，且不能重新引入 Generation、CAS、Transaction、Rollback、Recover、远端 Expected State、构建沙箱或平台化 Application Layer。旧架构只在 `legacy/v0.3` 接受必要安全修复。

## 实施纪律

- 新增或修改函数需写明用途、参数和返回值。
- 文件安全、幂等、兼容回退和 state 提交边界需说明“为什么”。
- 自动验证优先使用 Fake、fixture 或本地容器，不读取真实密钥，不连接生产服务器。
- Build 必须发生在 Connect 之前；State 必须发生在全部远端操作成功之后。
- 未知远端文件永不删除，protected 路径必须在最终合并计划再次校验。
- 禁止手工编辑 `uv.lock`；使用 `uv lock` 更新。

## 门禁

```bash
uv lock --check
uv run pytest -q
uv run ruff check src tests
uv run ty check src
uv build --clear
```

入口、版本或打包变更还需要隔离安装 wheel 并运行 `git-deploy --version`、`git-deploy --help` 和最小 dry-run。
