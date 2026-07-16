# git-deploy v1.0.1

v1.0.1 是 v1-lite 的安全与正确性补丁版，修复审计发现的两个发布阻断问题，并收紧构建后检查与初始连接重试。

## 安全修复

- 配置的 Output 根目录在构建后必须存在。缺失目录不再被解释为“全部产物已删除”，因此不会生成远端批量 Delete。
- 每次计划都比较 HEAD 下完整 Source 所有权和当前完整 Output 所有权，即使某一侧本轮没有变化也会拒绝相同远端路径。
- 配置加载阶段拒绝相同或相互嵌套的 Output 远端根，避免当前无碰撞但未来产生不明确删除归属。
- 上述缺失或冲突错误均发生在 Transport 创建和 Remote Connect 之前。

## 可靠性修复

- 初次 Connect + Ensure Root 使用 `deploy.retries`，不再只重试单文件操作。
- Build 后重新读取工作区状态；新增变化会明确警告，`require_clean_worktree = true` 时直接阻止部署。
- 廉价 State 读取和 SFTP Target 解析移到 Build 前，损坏 State 或变化的 Target 不再浪费一次昂贵构建。

## 验证

- Output 缺失、空目录、路径拼错、完整所有权冲突、嵌套 mapping；
- 零 Remote Connect、零 Delete、State 不变；
- Initial Connect Retry、Build 后 Dirty Warning/Block；
- Python 3.11/3.12、真实 FTP/SFTP、Node/PHP/混合构建链；
- Ruff、ty、wheel/sdist 和隔离安装。
