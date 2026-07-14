# Contributing to git-deploy

## v0.3 产品范围

`git-deploy` 是面向个人和小团队的可靠文件部署 CLI，不是多人发布平台。变更应优先减少日常操作、提高部署/恢复可靠性并降低长期维护成本。

提交新功能提案前必须回答：

1. 是否直接提高日常部署可靠性？
2. 是否减少本人每次部署的操作？
3. 是否降低长期维护成本？
4. 能否不新增常驻依赖？
5. 能否在 fake、fixture 或本地容器环境自动验证？

新功能只有至少满足前三项中的两项，才允许进入当前里程碑；第 4、5 项若回答“否”，必须说明替代方案、维护成本和自动门禁。仅为了未来 UI、平台化或理论完整性增加的抽象不予准入。

以下能力在 v0.3 冻结或不做：TUI/Web UI、非最新回滚、自动 GC、多用户/RBAC/审批、Kubernetes 发布、通用流水线 DSL、数据库 migration 自动回滚、自动 adopt 未知远端内容。重新评估必须基于重复出现的真实使用证据，并先更新 ADR、北极星和原子 TODO。

## 实施与验证

- 公共 application contract 变更必须同步更新 `docs/application-contract-v0.3.md` 和 contract tests。
- 修改函数时补充用途、参数和返回值注释；安全边界说明为什么这样做。
- 自动测试优先使用临时 Git 仓库、fake transport 或本地容器，不读取生产 secret，不写生产服务器。
- 每项任务运行其精确测试、Ruff 和 ty；打包/入口变化还要运行 lock check 和 build。
- 禁止手工编辑 `uv.lock` 或生成代码，禁止用 `--force` 绕过 identity、policy、integrity、generation 或 transaction 门禁。

完整产品边界见 [v0.3 简化稳定版北极星](docs/planning/2026-07-14-git-deploy-v0.3-simplified-northstar.md)。
