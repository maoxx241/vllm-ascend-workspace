# 原生客户端与编辑隔离

Status: current

默认接入客户端自己的会话生命周期。初始化时一次选好原生 Worktree 模式和
环境；之后用户正常创建会话，客户端创建目录并运行 setup，再让 Agent 开始
操作。setup 为符合条件的新目录检查主仓、采用准备好的代码，并固定配套依赖
和客户端接线。SessionStart 自动关联实际会话与 cwd，Agent 不需要先调用
VAWS 启动 CLI 或填写会话记录。已有目录和恢复会话保留代码、任务身份和环境。

普通 Local 会话仍在客户端选定的目录中，不会由 hook 强制变成 worktree。
客户端选目录，setup 准备这个尚未开始工作的目录，session hook 记录原生身份；
三个时点各自承担明确职责。

## 原生接入范围

| 客户端 | 已接入的生命周期 | 当前边界 |
|---|---|---|
| Codex App | 选定 local environment 的 setup 回调准备客户端新建的 worktree；SessionStart 关联 VAWS | 初始化时需选 Worktree 模式和该环境；接线与契约测试已实现，真实 GUI 新会话尚未验收 |
| Cursor | 生成的 `<project>/.cursor/worktrees.json` setup-worktree 回调准备新目录；sessionStart / preToolUse 幂等关联并内部注入 context | 使用客户端 Worktree 模式；接线与契约测试已实现，真实 GUI 新会话尚未验收 |
| Claude Code | 现有 SessionStart 关联及工具输入注入；原生提供 WorktreeCreate 扩展 | 本轮未接入整套版本更新；创建回调后的新版 MCP 配置重载尚未确认 |
| Grok 1.0.25 | 现有 SessionStart 关联和 PreToolUse 输入注入；原生 /new、/fork 可设自动 worktree | 未找到创建前更新回调；相关偏好为全局配置，不在项目初始化中静默修改 |
| Kimi Code 0.42.0 | 普通 startup/resume 的 SessionStart 关联；UserPromptSubmit 提供 context | 未提供 worktree 创建回调或工具输入注入；fork 不发 SessionStart，子 Agent hook 缺稳定 child ID，不能声称已覆盖 |

Codex 通过[本地环境 setup](https://learn.chatgpt.com/docs/environments/local-environment)
配置准备操作，目录由其[原生 worktree 功能](https://learn.chatgpt.com/docs/environments/git-worktrees)
创建。Cursor 的[worktree setup](https://cursor.com/docs/configuration/worktrees)
和[preToolUse 返回值](https://cursor.com/docs/hooks#pretooluse)分别提供准备时点
和参数注入。Claude 的[WorktreeCreate](https://code.claude.com/docs/en/hooks#worktreecreate)
允许客户端消费返回的路径；这项能力本身不证明新版依赖和 MCP 已接通。

Grok 边界依据该版本内置 help 和用户文档：SessionStart 输出被忽略，
`new_session_worktree_mode` 只描述 /new 的偏好，不能当作所有启动方式的默认值。
Kimi 边界依据 [0.42.0 的 session hook 实现](https://github.com/MoonshotAI/kimi-code/blob/6954d2c8bf94a5c7fc29cc6ae35b15d042cc4dcb/packages/agent-core-v2/src/features/externalHooks/session/sessionExternalHooksService.ts)
及[官方 hook 合同](https://moonshotai.github.io/kimi-code/en/customization/hooks)：
hook 在 cwd 确定后执行，完成结果不能替换 cwd。未支持的能力保留为客户端
适配缺口，不通过要求 Agent 手动搬目录、轮询或额外填表来补齐。

## Context in MCP and shell

MCP 工具参数和 shell 子进程环境是不同的入口，自动接入程度不能混为一谈。
Codex、Claude、Cursor、Grok 的 task-tool hook 可在内部注入 context；
Kimi Code 当前只把关联文本提供给 Agent，没有工具参数改写能力。

普通 skill CLI 复用 `VAWS_CONTEXT_FILE`；Codex 还可按真实原生 thread ID
解析同一关联，Claude 的 SessionStart 可通过 `CLAUDE_ENV_FILE` 导出环境。
Cursor 的 MCP 参数注入不会修改 shell 环境，Grok/Kimi 的现有接线也没有
等价的 shell 注入能力。这些客户端的 shell CLI 自动关联仍是适配缺口；
已有明确 context 可用于显式调用，但不能从 cwd、最近任务或任意用户名称推断。

## 目录、身份与固定输入

| 对象 | 所有者与边界 |
|---|---|
| 本地文件、HEAD、index | 原生客户端选择的 Git 工作目录 |
| 原生会话与 task attachment | coordinator 的原生身份记录；目录名称不授予关联或资源权限 |
| 已接纳执行的源码、环境、资源 | coordinator；后续本地编辑不会改变固定输入 |
| 本地 Python 与客户端配置 | 消费者环境准备和 wiring |

SessionStart hook 可以记录实际 cwd 和关联来源，其子进程不能通过切换目录
移动父客户端。原生 setup 只准备客户端传入的新目录，不另建第二份编辑副本。
Cursor 的 preToolUse 可在 SessionStart 尚未完成时幂等建立同一个关联，并为
托管调用注入 context；先后顺序无需 Agent 排错。恢复使用明确的原生会话 ID。
缺失的旧目录、冲突的身份和不支持的来源直接返回事实，不按最近会话猜测。

客户端拥有 worktree 的 Git/index 与创建方式。setup 只采用能快进的准备版本，
保留显式旧提交、业务来源和脏内容；未初始化的空子模块保持未初始化，不拉取
轻量任务不需要的源码。当前回调要求来源和目标是同一 Git 公共目录下的不同
工作目录，并且目标是新 worktree 根目录。条件不满足时返回具体原因。

Hook 通过 Git common directory 识别关联仓库，不能用字符串父目录关系把
嵌套的另一仓库当作同一项目。执行来源优先级为：本次 run 指定的 sources、
显式 task 默认值、该 attachment 的自动来源。更新 attachment 的 cwd 不覆盖
其他 attachment，也不改变已接纳执行。SessionStart 创建本地 VAWS task，
不因此占用远端容器、设备或端口；这些能力在实际执行需要时参与。

## 固定的本地环境

依赖环境由平台、架构、实际 Python/ABI、lock 和有效依赖选择决定内容键。
相同输入复用已经完成的环境；显式 sync 构建缺失环境。原生新 worktree 的
setup 可在 Agent 开始前准备新环境；已有目录和恢复会话读取原环境的 ready
receipt，不安装依赖或运行全量 doctor。已发布环境不原地升级或搬迁。

新目录中的 Hook/MCP 配置固定解释器和 receipt；业务入口从该目录的环境选择
读取依赖，无需 Agent 执行 shell 激活。不将 setup 子进程的环境变量当成已经
传回父 GUI 客户端的事实。依赖变更通过更新项目声明并 sync 准备另一环境，
不能用 pip 原地修改共享已发布环境。

WSL 原生 Python 与 Windows 托管 owner 分别固定，避免运行中修改 lock 后
切换另一方版本。共用配置所需的相对 Windows Python 链接按内容键生成且不改向。
新编辑目录继承依赖选择，不复制任务执行状态或资源记录。客户端信任和审批
遵循用户授权，由客户端自身配置；setup 不默默更改这些策略。

## Windows 与 WSL

共享 Windows owner 使用它能访问的 mounted-drive 来源。原生新 worktree
setup 需要由 Windows owner 执行；当前从 WSL 的 /mnt 目录调用会返回该边界，
不自动混用两端的 Git linked-worktree 指针。Linux home 中的原生目录不自动
获得 Windows owner 可访问性。本轮不扩大混合系统原生 worktree 的支持承诺。
独立 Linux、macOS 和 Windows 的行为以相应测试与实机证据为准。完整合同见
[platform-contract.md](platform-contract.md)。

## 可选 CLI 便利入口

`vaws_client.py` 适用于希望从终端启动已安装 CLI 的用户，不是原生客户端或
Agent 日常创建会话的前置步骤：

```text
uv run --no-project python .agents/scripts/vaws_client.py codex
uv run --no-project python .agents/scripts/vaws_client.py kimi --workspace PATH
```

此入口创建独立 Git 副本，在创建前使用同一更新器；已有显式目录直接复用。
原生参数和恢复 ID 放在 `--` 之后；恢复须带原 `--workspace PATH`，入口不按
ID 猜测历史目录。复制保留 HEAD、暂存与工作区变更、普通未跟踪文件、有效
换行/文件模式及已初始化子模块的独立 Git 状态；不复制私有运行状态和被忽略
内容。含非 Git 内容的未初始化 gitlink、冲突或稀疏 index 等状态明确失败。

这些副本有独立 `.git`，区别于客户端自己的 linked worktree。挂载盘复制由
已有 Windows owner 完成，避免两端解释绝对 Git 指针。入口在启动子 CLI 前
设置 cwd、PATH 和 VIRTUAL_ENV，并清除父任务身份；这是该便利入口的能力，
不代表任意 GUI setup 子进程可以改动父应用的环境。
