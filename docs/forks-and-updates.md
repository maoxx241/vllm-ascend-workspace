# 个人 Fork 与主仓自动更新

Status: current

目标是首次真正使用仓库时即可建立个人开发配置，在原生客户端的新 worktree
交给 Agent 前跟上主仓。
不要求知道或调用某个 Skill。仅将身份校验、Git fast-forward、锁定依赖准备
这些边界明确的操作工具化。常规更新由客户端生命周期回调消化；只有需要处理的
特殊 remote、分叉历史或版本切换取舍才交给 Agent。遵循[九条设计原则](design-principles.md)。

## 首次使用入口

| 使用方式 | 首次发现 | 更新方式 |
|---|---|---|
| 首次直接打开仓库使用 Agent | 根 AGENTS.md 说明一次身份确认 | 初始化接通所选客户端的 Worktree 模式和环境；已打开的目录保持原版本 |
| 已配置的 Codex / Cursor 原生 Worktree 会话 | 客户端创建目录并调用 setup | Agent 开始前检查主仓、准备新目录及配套环境；SessionStart 自动关联 VAWS |
| 已有目录、恢复会话或普通 Local 会话 | 客户端提供原生 ID 和实际 cwd | 保留代码和环境；不会由 session hook 另建目录或切换目录 |
| 可选 CLI 入口 | 本地配置检查，缺身份时一次可见提示 | 新建编辑副本前检查和准备一次；已有目录复用原版本 |
| 无 Agent / 无 hook | 直接调用通用脚本 | 显式 check、prepare 或 apply，无常驻进程 |

Git clone 不会执行仓库代码；AGENTS.md 不是操作系统 hook，因此不声称仅 clone
就会运行程序。初始化时一次选好原生 Worktree 模式和环境，后续新会话由客户端
自动执行 setup，不要求 Agent 调用启动脚本。原生配置文件、客户端的环境选择与
MCP 启用是不同的初始化状态，不能仅凭写文件宣称接通。实际验收记录见
[原生客户端验收](native-client-validation-2026-09-12.md)，各客户端的能力边界见
[原生客户端与编辑隔离](native-workspace-isolation.md)。
身份待确认、离线或准备失败时，保留可用的本地版本；轻量 Review、目录查询
和其他独立本地工作不需要先完成更新。

首次确认个人 GitHub 用户名。`gh` 登录是候选，不能静默代替用户选择。
确认结果位于未跟踪的 `.vaws-local/github.json`，不包含凭据。
coordinator 自动读取它并绑定 native session 的用户归属，SSH 仍使用 root；
具体行为见[用户与协调](identity-and-agent-coordination.md)。GitHub 为新 Fork
分配不同仓库名时，配置保存实际地址，新编辑副本和更新器沿用同一地址。

## 个人 Fork

```text
uv run --no-project python .agents/scripts/workspace_forks.py
uv run --no-project python .agents/scripts/workspace_forks.py --github-user USER --apply
```

默认只读计划；用户接受后 apply。默认覆盖 workspace、vLLM、vLLM-Ascend；
`--repo workspace` 可只配置主仓。入口为纯标准库，不依赖 Skill 或 VAWS runtime。

校验当前认证 User、实际 full_name、owner.type、fork 标记和 canonical
parent/source network，拒绝组织 Fork、指向其他所有者的 redirect、同名独立
仓库和不相关 Fork。`origin` 是个人 Fork，`upstream` 是官方来源。
`.gitmodules` 保留社区 URL；先初始化子模块再配置其 remotes，防止误改父仓库。

重复执行复用正确 Fork，保留额外 remote、脏内容和 Git HEAD。复杂 fetch/push
配置报告具体差异，显式替换时保存备份。组件只在需要贡献时 fork；安装使用
工作区锁定提交。本次 coordinator 测试版暂时锁定个人 Fork 的 `0.4.1.dev1`
提交，正式组合再由维护者更新为官方版本。知识公开贡献由 knowledge package 管理，配置代码 Fork 不启用
公开知识发布；它也必须遵循个人 Fork 约束。

本轮自动校验覆盖上述三个开发仓库。外部组件包自己的 Fork/贡献入口还需要由
各 owner 接入同样的校验；这里没有改其已发布代码，不能声称所有外部入口已强制执行。

这是项目入口和工具的规则，不是劫持用户任意终端 Git 命令。
组织级强制治理需要另配置 GitHub 权限和分支规则。

## 配套组件随工作区提交更新

主仓默认分支的每个提交通过 pyproject.toml、uv.lock、vaws-top wheel pin 和
submodule gitlink 一起确定版本，不另复制一套 SHA 清单。组件更新后，维护者
更新 workspace 锁定输入并验证；用户随主仓自动取得这套配套版本，不必等待
workspace Release，也不各自追逐所有组件仓库的最新分支头。
共享知识内容的 Release 继续由 knowledge package 自己同步。

正式 Release 仍可作为版本里程碑。`.github/workflows/release.yml`
在官方仓库收到 `vMAJOR.MINOR.PATCH` tag 时，
复用三平台消费者 CI，验证 monitor wheel 后才公开 Release。
仅为审核过的官方主线提交打 tag，不移动已发布 tag；推荐启用 immutable releases。
个人 Fork 不承担官方发布工作。

## 检测、准备和采用

```text
uv run --no-project python .agents/scripts/workspace_update.py check
uv run --no-project python .agents/scripts/workspace_update.py prepare
uv run --no-project python .agents/scripts/workspace_update.py apply
```

配置个人身份和客户端后，原生客户端创建新 worktree，再在 Agent 首次操作前
调用 `vaws_worktree_setup.py`。Codex 使用选定本地环境的 setup；Cursor 使用
生成的 `<project>/.cursor/worktrees.json` 中的 setup-worktree。两者把实际源目录和新目录传给
回调，由回调检测一次官方 default_branch 的最新提交（当前为 main），不依赖
tag 或 Release。原生回调机制分别见
[Codex 本地环境](https://learn.chatgpt.com/docs/environments/local-environment) 和
[Cursor worktrees](https://cursor.com/docs/configuration/worktrees)。

回调只处理客户端刚创建的目录，不另建一份 worktree，也不通过 SessionStart
切换父客户端 cwd。采用新版后，在该目录固定不可变环境和所选客户端的 MCP/hook
接线，再交还客户端。已有 setup 命令继续保留，VAWS 准备置于其前，使安装和
构建脚本读取本次选择的版本。SessionStart 负责自动建立或恢复 VAWS 关联；Cursor 的
preToolUse 在内部补入 context，先后触发的关联操作保持幂等，无需 Agent 处理
hook 顺序。已有环境选择的目录重复 setup 时只复用和修复接线，不再检查更新。
恢复原会话使用原生客户端的恢复功能，保留原目录和版本。没有每五分钟轮询、
常驻 watcher 或工作中的版本切换。

`check` 只检查版本；`prepare` 在独立目录准备本轮取得的精确提交 SHA，调用该版本的既有
`vaws_deps.py sync --locked` 复用不可变环境，并缓存/验证 vaws-top wheel。
准备成功后，个人 Fork 默认分支仅 fast-forward 到该提交。主仓继续前进时，
下次新建会话再检查；同一提交已准备完成则直接复用，不重复下载依赖。
沿用早期 `.vaws-local/updates/releases/<SHA>` 缓存目录名以复用已有准备结果，
目录名不代表必须有 Release。

准备过程不改源工作目录。若来源是干净、可快进的默认分支，新 worktree
与来源 HEAD 一致且没有本地改动，setup 可将这个尚未交给 Agent 的目录快进到
准备好的主仓提交。客户端显式选择旧提交、业务分支或带改动来源时保留该选择；
未初始化的子模块不因启动会话而拉取。回调再次检查新目录，避免覆盖准备期间
发生的编辑。更新不可用时保留本地代码；若连本地依赖或客户端接线也无法准备，
setup 返回失败事实，由原生客户端显示，不报告为已就绪。
已有目录的显式维护可用 apply：要求干净默认分支、没有 merge/rebase，
已初始化子模块无业务改动；只采用工作区固定的 gitlink，未初始化子模块保持原样。
不适合自动更新的来源跳过用不上的依赖准备，返回保留原因。正常任务无需检查
更新状态或运行 apply；仅在主动维护该目录或任务需要新版本时处理。

`vaws_client.py` 保留为可选 CLI 便利入口。它在创建独立编辑副本前使用同一
更新器；恢复时必须带原来的 `--workspace PATH`，原生 resume ID 只透传给
客户端，不用于猜测历史目录。这不是日常原生会话的前置步骤。

不自动 stash/reset/rebase/强推。运行中的 MCP、hook、coordinator 和服务
继续使用旧环境；新的原生 worktree 可以采用准备好的新版本，恢复会话沿用原环境。
新 coordinator 客户端遇到较旧 daemon 时使用既有 restart-if-idle 自动切换；
忙碌时保留旧实例，旧客户端不会将新版
降级。monitor 继续使用其既有实例管理机制。知识准备 pending 不阻止独立工具。

## 可观察性

`.vaws-local/updates/state.json` 保留检测到的默认分支、精确提交、当前步骤、准备结果和
未完成原因；命令返回本轮结果，失败命令证据保留在 logs 子目录。
配置该目录 config.json 为 `{"enabled": false}` 暂停新建会话时的自动更新。
显式命令仍可用于检查和修复。状态只记录安装结果，不接管任务和设备权属。

同一 Git 公共目录通过 OS 锁串行执行更新。共享 Windows 挂载目录的原生 setup
需要由 Windows owner 执行；本轮不支持从 WSL 的 /mnt 路径运行这项回调，
也不扩展混合系统 linked-worktree 承诺。已有更新器的 Windows owner 转发
不等于原生 GUI 回调已通过跨系统验收。

## 参考与取舍

新会话需要选择代码和环境，因此更新放在客户端创建目录之后、交给 Agent 之前；
会话开始工作后版本固定。
这个边界无需定时进程、通知接收层或 Agent 轮询，也不增加每任务维护命令。
依赖组合和安装复用遵循 [uv locking/syncing](https://docs.astral.sh/uv/concepts/projects/sync/)，
运行进程由 workspace 已有不可变环境机制保护。
