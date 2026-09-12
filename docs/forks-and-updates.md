# 个人 Fork 与主仓自动更新

Status: current

目标是首次真正使用仓库时即可建立个人开发配置，后续周期性跟上主仓。
不要求知道或调用某个 Skill。仅将身份校验、Git fast-forward、锁定依赖准备
这些边界明确的操作工具化。常规更新由入口和后台进程消化；只有需要处理的
特殊 remote、分叉历史或版本切换取舍才交给 Agent。遵循[九条设计原则](design-principles.md)。

## 首次使用入口

| 使用方式 | 首次发现 | 更新方式 |
|---|---|---|
| 直接打开仓库使用 Agent | 根 AGENTS.md 指向通用工具并说明一次身份确认 | 初始化接通客户端入口；没有可执行 hook 时，当前目录仍需显式维护 |
| 原生 CLI 入口 | 本地配置检查，缺身份时一次可见提示 | 后台准备，新建编辑副本可使用准备好的主仓提交 |
| 已配置的桌面客户端 | session hook 唤起已配置的更新器；AGENTS.md 负责身份问答 | 不改变正在打开的目录 |
| 无 Agent / 无 hook | 直接调用通用脚本 | 显式运行 watch，或另由本机进程管理器托管 |

Git clone 不会执行仓库代码；AGENTS.md 不是操作系统 hook，因此不声称仅 clone
就会运行程序。入口只做本地检查，下载和安装在独立进程中进行。身份待确认、
离线、更新失败都不妨碍轻量 Review、目录查询或其他独立本地工作。

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
uv run --no-project python .agents/scripts/workspace_update.py watch
uv run --no-project python .agents/scripts/workspace_update.py apply
```

配置个人身份后，原生入口唤起独立 watcher，默认约每 5 分钟检测官方仓库
default_branch 的最新提交（当前为 main），不依赖 tag 或 Release。
只有联网且进程运行时有该检测时效；睡眠、离线或进程终止后，由下次原生入口
补启动。它不是秒级推送，也没有声称已经安装开机启动服务。

后台在独立目录准备本轮取得的精确提交 SHA，调用该版本的既有
`vaws_deps.py sync --locked` 复用不可变环境，并缓存/验证 vaws-top wheel。
准备成功后，个人 Fork 默认分支仅 fast-forward 到该提交。主仓继续前进时，
下一轮准备新的提交；同一提交已准备完成则直接复用，不重复下载依赖。
沿用早期 `.vaws-local/updates/releases/<SHA>` 缓存目录名以复用已有准备结果，
目录名不代表必须有 Release。

watcher 不改现有工作目录。默认 CLI 新建编辑副本时，若现有来源是干净、
可快进的默认分支，可使用准备好的主仓提交作为新副本来源并运行其客户端接线。
已有目录的显式维护可用 apply：要求干净默认分支、没有 merge/rebase，
已初始化子模块无业务改动；只采用工作区固定的 gitlink，未初始化子模块保持原样。
有业务分支、脏文件或分叉时保留原来源，原因保存在更新状态中。正常任务无需
检查更新状态或运行 apply；仅在主动维护该目录或任务需要新版本时处理。

不自动 stash/reset/rebase/强推。运行中的 MCP、hook、coordinator 和服务
继续使用旧环境；新会话选择新版本。新 coordinator 客户端遇到较旧 daemon
时使用既有 restart-if-idle 自动切换；忙碌时保留旧实例，旧客户端不会将新版
降级。monitor 继续使用其既有实例管理机制。知识准备 pending 不阻止独立工具。

## 可观察性

`.vaws-local/updates/state.json` 保留检测到的默认分支、精确提交、当前步骤、准备结果和
未完成原因，watch 输出在同目录 watch.log；失败命令证据保留在 logs 子目录。
配置该目录 config.json 为 `{"enabled": false}` 暂停自动更新，watcher 下一轮退出。
显式命令仍可用于检查和修复。状态只记录安装结果，不接管任务和设备权属。

同一 Git 公共目录通过 OS 锁串行执行更新；Windows 挂载目录从 WSL 发起时
使用已有 Windows Python owner，避免 Windows/WSL 各持不同种类的文件锁。
实现具有三平台 CI 覆盖入口，实际跨系统验收仍以 CI/实机结果为准。

## 参考与取舍

GitHub 推荐 webhook。普通电脑没有固定公网接收端，第一版采用有界轮询，
不引入托管 GitHub App。明确需要秒级通知时再增加接收层。
[GitHub REST 最佳实践](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api)
给出条件请求和限流退避建议。

Fork 的 Actions 不会自动订阅上游提交，定时工作也可能延迟或停用。
参见 [Actions 事件文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)。
依赖组合和安装复用遵循 [uv locking/syncing](https://docs.astral.sh/uv/concepts/projects/sync/)，
运行进程由 workspace 已有不可变环境机制保护。
