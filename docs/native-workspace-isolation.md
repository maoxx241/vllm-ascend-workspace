# 原生会话、本地工作树与执行隔离

Status: proposed target design, 2026-09-12. Current behavior and remaining work are identified below.

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

这些 hook 和 attachment 修复属于本轮包变更；它们只接纳和准确记录已由
客户端选择的 worktree，并不自动把所有新会话移动到独立目录。

顶层 workspace 的 worktree 也不自动保证 `vllm` / `vllm-ascend` 业务仓库
已经独立初始化。绑定必须指向实际业务 checkout；不能共享另一个会话的
index。[Git 官方文档](https://git-scm.com/docs/git-worktree)仍明确指出
submodule 的 worktree 支持不完整。复制当前工作状态时，需要保留 staged、
unstaged 和所需 untracked 内容，并验证原始 index 未变；不支持的递归状态
应明确失败，不能悄悄降为只复制 HEAD。

Windows/WSL 还有可访问性边界。本轮共享 Windows 所有者支持 mounted-drive
来源；Grok WSL 默认在 Linux home 下创建的 worktree 不属于这个已支持范围。
这里的缺口是路径适配和来源关联，不代表 Windows 完全不能读取 Linux
文件：独立 Git 副本可通过 WSL UNC 读取，但本轮适配器没有这个合同。

把 linked worktree 放到挂载盘也不自动解决问题。Windows Git 写入的
`D:/...` 内部指针与 Linux Git 写入的 `/mnt/d/...` 指针，可能不被另一端
理解。本机两端 Git 均未提供创建相对路径 worktree 的 CLI 选项。因此当前
确定支持的是所有者原生 Git 能读取的 linked worktree；不能把“WSL Grok
加一个 `--cwd`”写成已贯通的双平台隔离方案。后续需选定 Git 发现/捕获
所有者，或实现有原生 provenance 的独立副本方案，并验证两端操作。外层
路径字符串转换不足以替代这项验证，也不能启动第二个协调器绕开所有权。

## 本地依赖环境仍需要独立改造

当前 `.vaws-local/venvs/<platform>` 位于各 checkout 内。入口确实指向各自
worktree 时，它们可以隔离；两个会话使用同一目录或生成配置共同指向主目录
解释器时，依赖仍共享。当前 `sync` 会原地更新这个环境，单凭包可 import
也不能证明它匹配当前 lock。此部分尚未改为不可变环境。

后续改造应按平台、架构、Python ABI、完整 lock 内容和依赖 group/extra
选择内容键环境，使用共享包下载缓存。新环境在最终路径构建，验证后发布
ready 标记；虚拟环境通常不可搬迁，不能直接重命名带旧 shebang 的安装目录。
并发构建由短暂构建锁协调，不建立任务租约系统。

hooks 和 MCP 进程启动后固定使用所选绝对解释器。新 lock 创建或选择另一
环境，保留运行进程和 daemon 正在引用的旧环境；不原地升级已发布环境，
不因新客户端版本不同而替换活跃 daemon。生成的客户端配置、WSL Windows
所有者选择与解释器检查应一起迁移。该方案不要求每次任务启动执行 sync、
doctor 或完整检查；相同输入只需查找已完成环境。

验收应覆盖两客户端首条工具命令的不同 cwd/index、同名文件与锁文件并行
编辑、明确 ID 恢复原目录、多个 attachment 的默认来源互不覆盖，以及依赖
变化时旧进程模块身份不变。没有完成这些验证之前，不宣称“每个 session
自动获得完全独立的 workspace”。
