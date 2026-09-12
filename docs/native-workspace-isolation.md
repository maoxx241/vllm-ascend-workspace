# 原生会话、本地工作树与执行隔离

Status: current

不同会话拥有不同任务 ID，并不意味着它们拥有不同的本地工作目录。两个
会话从同一 checkout 启动，仍会修改同一份文件、Git index、锁文件和虚拟
环境。coordinator 0.4 已固定远端执行输入，但这只能保护已经接受的执行，
无法隔离提交前的本地编辑。本轮并行核心与知识任务实际遇到了这个问题。

## 隔离从客户端启动开始

原生客户端必须在第一条模型或工具请求之前选择独立 Git worktree，或者
以已经选择的 worktree 为进程 cwd。SessionStart hook 可以记录真实目录、
提供上下文和绑定来源；其子进程执行 `cd` 无法移动父客户端，也不能改变
所有原生 Read/Edit 工具的根目录。提示 Agent 后续切换目录不提供同等保证。

| 层次 | 所有者与边界 |
|---|---|
| 本地文件、HEAD、index、业务 checkout | 原生客户端的 worktree 能力；缺少该能力时，由启动入口使用普通 Git 并指定进程 cwd |
| 原生会话 ID、实际 cwd、默认来源 | coordinator 已有 attachment 记录；目录名称不授予任务关联或资源权限 |
| 接受后的固定源码、远端环境和执行 | coordinator；本地目录变化不自动重建或停止已有执行 |
| 本地解释器与生成的客户端配置 | 消费者依赖与客户端 wiring；不能通过修改会话 ID 获得环境隔离 |

当前可使用的入口有：Codex desktop 的原生 Worktree 模式；Codex CLI 的
`-C <worktree>`；Grok 的原生 `--worktree` 或指定启动 cwd；Kimi 的启动
进程 cwd 或 ACP `session/new.cwd`。本机 Codex CLI 未提供创建 worktree
的参数，Kimi Code 未提供 `--cwd` / `--worktree`，因此不能为它们虚构统一
命令。[Codex Worktree 文档](https://learn.chatgpt.com/docs/environments/git-worktrees)
说明的是 desktop 能力，不代表每个 CLI 都有相同入口。

Grok 的两次并行真实请求确认首条原生工具已位于不同 worktree；从父目录
按原 ID resume 会回到原 worktree，父目录 HEAD、index 和文件内容保留。
这是客户端目录隔离证据，不是 Windows 所有者读取 Linux-home 来源的证据。
本机 Grok 生成的副本有自己的 `.git`，不是共享 common directory 的 Git
linked worktree；不能仅凭产品中都叫 worktree，就视作已关联的同一仓库。
[Grok worktrees](https://docs.x.ai/build/features/worktrees)、
[Kimi ACP](https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-acp.html)。

Cursor 的 Agents Window 和 CLI 提供原生 worktree 入口；IDE 内的 `/worktree`
是另一种流程，不能推断它在首条工具调用前已隔离目录。其 SessionStart
为异步观察入口，不能承担强制切目录的职责。
[Cursor worktrees](https://cursor.com/docs/configuration/worktrees)、
[Cursor CLI](https://cursor.com/docs/cli/reference/parameters)、
[Cursor hooks](https://cursor.com/docs/hooks#sessionstart)。

Claude Code 文档提供 `--worktree` 和返回新目录的 `WorktreeCreate` hook；
这是创建边界，与普通 SessionStart 不同。本轮未安装或运行 Claude，不能
把其文档合同算作实测。[Claude worktrees](https://code.claude.com/docs/en/worktrees)、
[Claude hooks](https://code.claude.com/docs/en/hooks#worktreecreate)。

## 已有工作树与来源绑定

用户选择的已有 worktree 应直接复用。恢复必须使用明确的原生会话 ID 和
该会话实际目录；旧目录缺失或发生迁移时显式报告，不按最近会话或主目录
猜测。客户端已经选定目录后，VAWS 不再建立第二套本地 worktree 管理服务。

hook 对配置项目的识别需要比较 Git common directory，支持仓库目录之外
的同仓库 worktree。纯字符串父目录关系不足以识别 worktree，也不能证明
子目录里的另一仓库属于该项目。恢复 attachment 时同步实际 cwd；来源
优先级为：本次 `run(sources=...)`、显式任务默认来源、该 attachment 的
自动来源。自动来源不覆盖同一任务的其他 attachment，已经接受的执行
快照不变。旧记录没有来源 provenance 时，不能猜测它原本属于自动发现。

hook 和 attachment 接纳已经选择的工作目录。消费者现在还提供统一的
CLI 启动入口，在客户端进程创建前完成隔离：

```text
uv run --no-project python .agents/scripts/vaws_client.py codex
uv run --no-project python .agents/scripts/vaws_client.py kimi --workspace PATH
```

默认创建新目录；显式目录已经存在时直接复用，不再复制。原生参数和恢复
ID 放在 `--` 之后。新客户端不继承父进程的任务关联，真实 native hook
负责其任务身份。桌面客户端的 Local/Worktree 选择仍由原生 UI 控制。

新目录使用普通 Git clone 的独立 `.git`，本地对象可以通过 Git 复用，
文件和 index 各自独立。入口保存当前 HEAD、staged、unstaged、普通
untracked 内容和已初始化的递归 submodule，再比较真实 diff、status 和
文件内容。暂存区冲突、稀疏 index 或未初始化的 submodule 会明确失败。
被忽略的内容和私有 `.vaws-local` 运行状态不复制。已有目标绝不覆盖；
失败的未完成目录保留供检查，不标记为 ready。

Windows/WSL 仍有可访问性边界。共享 Windows 所有者支持 mounted-drive
来源；Grok 自己在 Linux home 下创建的 worktree 不属于该范围。新的统一
CLI 默认在项目的 mounted drive 下创建目录，不依赖 Grok 的默认位置。

把 linked worktree 放到挂载盘也不自动解决问题。Windows Git 写入的
`D:/...` 内部指针与 Linux Git 写入的 `/mnt/d/...` 指针，可能不被另一端
理解。本机两端 Git 均未提供创建相对路径 worktree 的 CLI 选项。因此当前
统一入口会把 mounted-drive 上的复制交给准备好的 Windows 解释器、Git
和文件 API，再返回 WSL 可用的独立目录。这样也保留 Windows 目录符号
链接的类型，避免 Linux 创建的 reparse point 在 Windows 下不可读。
复制固定有效的 Git 换行和文件模式设置，防止两个平台把同一文件识别为
不同修改。这里只代理一次有明确输入输出的复制，不启动管理服务。

## 本地依赖环境

环境现在按平台、架构、真实 Python 版本与 ABI、lock 和有效依赖选择
确定内容键，保存在用户目录中。相同输入复用同一完成环境。显式 `sync`
构建缺失环境；普通入口只读取 ready receipt，不运行安装或 doctor。

安装使用固定读取的输入，在最终路径构建，验证后发布 ready 标记。并发
构建只按内容键加构建锁。解释器先解析到实际的完整版本路径，避免 uv 的
可变 minor-version 别名在以后升级时改变旧环境。失败的未发布环境可以
重试；已发布环境不原地升级、不搬迁，也不由任务结束自动删除。

hooks 和 MCP 固定解释器与 receipt。Windows/WSL 共用配置需要相对命令时，
使用按内容键建立且永不改向的目录 junction。项目只保留显式 setup 的
平台选择配置，不记录任务租约。WSL 读取实际 Windows receipt，不用 Linux
ABI 猜 Windows 环境。新本地目录只继承这些依赖选择，不复制运行状态。

测试覆盖首次子进程 cwd、并行 index 和同名文件、明确目录恢复、两端 Git
状态一致、目录链接实际可读，以及依赖变化后旧进程和子进程仍加载旧环境。
完整跨平台合同见 [platform-contract.md](platform-contract.md)。
