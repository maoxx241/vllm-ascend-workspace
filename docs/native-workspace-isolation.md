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
| Codex App | 选定 local environment 的 setup 准备客户端新建 worktree；用户级 SessionStart 关联 VAWS | 一次选择 Worktree 和 VAWS 环境，使用 --codex-global-hooks 并原生审阅固定 hook。创建任务 API 使用原生保存的环境选择；仅写配置文件或 Git key 不等于选定。 |
| Cursor | worktrees.json 的 setup-worktree 准备新目录；sessionStart / preToolUse 自动关联 | 一次将 Default Environment 选为 New Worktree，并使用 --cursor-global-mcp 安装用户级 VAWS providers；新目录无需重复启用项目 MCP。 |
| Claude Code | WorktreeCreate 创建并准备目录；SessionStart 关联；MCP/Hook 启动时读取实际目录的固定环境 | 使用原生 worktree 模式。2.1.269 已验证新建与从母仓恢复；旧 2.1.143 跨目录恢复存在客户端问题。 |
| Grok | 原生 Git worktree 创建触发项目 post-checkout；SessionStart / PreToolUse 自动关联 | 一次将 cli.worktree_type 设为 git，new_session_worktree_mode / fork_worktree_mode 设为 always。它们是全局偏好，项目 setup 只说明选择。已有 Git hook 保留给其 owner 集成。 |
| Kimi Code | 官方 0.42.0 的 SessionStart 只关联；带 SessionSetup 扩展的个人 fork 能在创建 workspace/MCP 前准备目录 | 扩展需单独安装并显式启用，不能将官方版本描述为已支持。新建、恢复、Bash/MCP 与子 Agent 使用原生身份。 |

Codex 的[本地环境 setup](https://learn.chatgpt.com/docs/environments/local-environment)
和 Cursor 的[worktree setup](https://cursor.com/docs/configuration/worktrees)在工作开始前
准备目录。Claude 的[WorktreeCreate](https://code.claude.com/docs/en/hooks#worktreecreate)
返回客户端采用的路径；部分版本提前读取 MCP 配置，因此生成的薄启动入口只按
实际 cwd 读取已经选好的 receipt，再执行对应组件，不在启动时更新代码。

Codex 按配置来源和定义内容记录 hook 信任。初始化将本仓生成的 hook 迁移到
用户级固定入口，保留自定义 hook；新 worktree 的 setup 自动去掉重复的项目入口。
固定入口只处理同一 Git 公共目录的原生 cwd，读取该目录已经选好的环境和任务
设置，再运行组件 hook。新目录和依赖版本不会改变这条入口定义，正常会话无需
重复信任。配置生成本身不授予信任；新定义仍需按原生机制审阅。

Grok 的普通启动可使用其原生自动 worktree 偏好；Git 创建回调只处理 Grok
目录下刚创建的 linked worktree，普通 checkout 和其他客户端目录不受影响。
默认的复制模式不触发这个回调。正式版 1.0.30 在 worktree 内连续 /new 或
/fork 的目录判断仍有客户端缺陷；已安装的个人修复版基于公开源码 1.0.24，
保留官方二进制供回退。补丁来源与实测范围见验收记录。

Kimi 官方[会话 hook](https://moonshotai.github.io/kimi-code/en/customization/hooks)
执行时 cwd 已经确定，现有插件不能替换它。个人客户端扩展增加一个有界的
SessionSetup：在原生 workspace/MCP 创建前消费返回 cwd；恢复沿用原目录。
原生分叉使用新目录和新身份，保留当前暂存、未暂存、普通未跟踪内容、已初始化
子模块和会话历史。分叉沿用原 HEAD 与已选环境，不检查上游；普通新会话继续
执行启动时更新。历史位置分叉保持客户端原有的对话截断语义，不回滚当前文件。
消费端通过 `vaws_client_setup.py --client kimi --kimi-session-setup --apply`
显式接入，普通官方客户端配置不会包含未知事件。现有信任策略仍由客户端处理。
扩展模式同时配置用户级 VAWS providers，避免每个新目录重复进行项目 MCP
初始化。Cursor 的用户级入口通过原生 `${workspaceFolder}` 获得目录，Kimi
通过原生进程 cwd 获得目录；两者只读取该目录已准备的环境 receipt，再启动
固定的三个组件。自定义服务器保留，工作目录不会被用于推断任务身份。
实际通过的任务、所用版本和剩余边界见
[原生客户端验收](native-client-validation-2026-09-12.md)。

## Context in MCP and shell

MCP 工具参数和 shell 子进程环境是不同的入口。Claude、Cursor、Grok
的 task-tool hook 可在内部注入 context。Codex 的原生 MCP 调用通过
`_meta.x-codex-turn-metadata.thread_id` 传递真实调用者，coordinator 查找
SessionStart 已建立的关联；`functions.exec` 内的调用也无需手动传 context。
直接工具调用仍可使用已有的 task-tool hook。Kimi 扩展直接在 MCP tools/call 的
_meta 携带原生 session/agent ID，coordinator 查找已有的对应 attachment；
不使用共享 MCP 进程自己的 session，也不从目录猜测用户或任务。

普通 skill CLI 复用 VAWS_CONTEXT_FILE。Codex、Cursor、Grok、Kimi 扩展还能按
客户端提供的真实原生 ID 解析同一关联；Claude 通过 CLAUDE_ENV_FILE 导出
context 和固定 receipt。Kimi 字面 main 表示根 Agent，其余 child ID 必须精确
对应已存在的 attachment。Cursor shell 提供的 CURSOR_CONVERSATION_ID
支持原生 UUID 会话自动关联；被编码或截断的非 UUID 值直接返回事实，不猜测
原始身份。MCP 与 shell 的无参调用已分别验证，Agent 无需搬运 context 参数。

MCP 同时在 text 和 structuredContent 返回相同的紧凑事实；full=true 才展开
完整记录。只读取 text 的客户端也能看到任务、来源、失败原因和原始记录引用。

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
Kimi 用户级 provider 使用 Windows owner 的绝对解释器和入口路径，避免换
worktree 后解析母目录的相对链接；该配置供原生 Windows 客户端执行，不能
同时当作 WSL Linux 客户端的启动命令。路径生成测试不替代 Windows 实机验收。
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
