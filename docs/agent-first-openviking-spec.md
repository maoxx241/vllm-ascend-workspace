# Agent/OpenViking 接入历史记录

Status: dated design history (2026-09-11; superseded 2026-09-12)

原方案将 Markdown 捕获、OpenViking 本地检索、公共贡献和预建分发接入 VAWS。
本页保留这一阶段的事实和证据入口，已不承担实施规范。旧版的分阶段计划、
自动审核与冲突处理流程不再构成当前要求或后续实施承诺。

当前知识约定统一见 [target-state.md 的知识部分](target-state.md#54-knowledge)：
按需参考，普通 Markdown，复用已有总结，保留条件、证据和不确定性。
日常工作不需要查库、录入、发布或复核清单。职责和执行入口见
[当前组件合同](target-state.md)，整体设计原则见 [design-principles.md](design-principles.md)。

## 当时完成的工作与证据

- 本地内容转为标题和正文的 Markdown；OpenViking 索引可由原文重建。
  本地经验和公共内容一同作为参考，公开审核不赋予技术结论权威。
- 2026-09-11 记录了公共 fork PR、人工合并、Release 构建、后台更新和重启查询的验证。
  对应公开记录为 [corpus PR #1](https://github.com/vllm-ascend-workspace/vaws-knowledge-corpus/pull/1)
  和 [Release 构建](https://github.com/vllm-ascend-workspace/vaws-knowledge-corpus/actions/runs/34523521667)。
- Windows/WSL 的入口与本地验收见 [Agent-only 验收](agent-only-validation-2026-09-11.md)；
  Windows 包、进程及分发检查见 [Windows 验收](windows-validation-2026-09-11.md)。
- 当时的摘要适配器完成了客户端事件回放；原生 hook 的启用和信任仍由客户端管理。

上述记录只说明当时的代码、平台和样本。它们不代替当前安装状态或新任务的证据，
也不表示完整模型业务、其他平台、大规模分发或自动审核已经验证。
当前依赖由 `pyproject.toml` 和 `uv.lock` 标识，知识工具的用法由已安装包提供。
