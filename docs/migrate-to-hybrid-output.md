# 迁移到 Hybrid Output

Hybrid 适用于“前端产物直接位于远端混合项目根目录，但后端、环境文件和未知目录必须保留”的场景。普通 Incremental Output 没有变化，也无需迁移。

## 1. 建立本地聚合视图

在 `.gitignore` 增加：

```gitignore
.deploy/
```

让项目 Build 明确聚合所有前端结果：

```toml
[build]
steps = [
  "pnpm --dir frontend build",
  "pnpm --dir admin build",
  "python examples/aggregate_frontend_builds.py"
]
```

参考脚本 `examples/aggregate_frontend_builds.py` 不允许覆盖顺序、重复文件、文件/目录冲突或符号链接；请按项目修改文件顶部的显式 Sources 与 Destination。

## 2. 配置唯一 Hybrid Mapping

```toml
project_id = "github.com/example/project"

[[outputs]]
name = "frontend-root"
local = ".deploy/frontend-root"
remote = "."
mode = "hybrid"
```

Hybrid 只支持 SFTP，必须有唯一 Name、`remote = "."`，且不能配置 `delete_removed`。同一配置最多一个 Hybrid。`project_id` 可省略并从无凭据的 Git Origin 推导；无法推导时必须显式填写。

不要让 Source 或其他 Output 管理 Hybrid 当前直接子项，也不要聚合 `.env`、`.git`、`.git-deploy`、`uploads`、`runtime`、`storage` 等保护路径。

## 3. 首次审阅与接管

先完成本地零连接检查：

```bash
git-deploy prod --dry-run
```

再读取远端所有权并显示完整计划：

```bash
git-deploy prod --remote-plan
```

若远端已经有当前同名的 `assets/`、`index.html` 等路径，普通部署会拒绝。人工确认这些路径确实应由当前聚合视图覆盖后执行：

```bash
git-deploy prod --full
```

`--full` 只 Adoption 当前本地存在的同名直接子项，不会接管 `index.php`、`.env`、后端目录或任意其他未知内容。

## 4. 日常部署与恢复

```bash
git-deploy prod --yes
```

Mirror Directory 每次都会完整 Stage/Swap，因此聚合根只要含直接目录，通常就不是 No-op，`after_deploy` 也会执行。Root File 仍按 Hash 跳过未变化上传。

中断后直接重跑同一命令。工具只根据受身份约束的 Recovery Record 恢复/继续当前 Swap；不要手工删除 `.git-deploy`，也不要让另一个发布器修改 Hybrid 拥有的路径。Doctor 会只读报告 Ownership、Recovery、路径类型和是否需要 Adoption。
