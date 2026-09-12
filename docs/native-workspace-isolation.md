# 原生客户端与编辑隔离

Status: current

统一入口在原生客户端启动前选择实际工作目录，并准备其进程环境：

```text
uv run --no-project python .agents/scripts/vaws_client.py codex
uv run --no-project python .agents/scripts/vaws_client.py kimi --workspace PATH
```

默认创建独立 Git 副本；显式目录已经存在时直接复用，不覆盖其中内容。
原生参数和恢复 ID 放在 `--` 之后。客户端必须已安装在当前操作系统中。
桌面客户端的 Local/Worktree 选择仍由其原生 UI 管理。

## 目录、身份与固定输入

| 对象 | 所有者与边界 |
|---|---|
| 本地文件、HEAD、index | 原生客户端选择的 Git 工作目录 |
| 原生会话与 task attachment | coordinator 的原生身份记录；目录名称不授予关联或资源权限 |
| 已接纳执行的源码、环境、资源 | coordinator；后续本地编辑不会改变固定输入 |
| 本地 Python 与客户端配置 | 消费者环境准备和 wiring |

SessionStart hook 可以记录实际 cwd 和关联来源，其子进程不能通过切换目录
移动父客户端。入口在第一条工具请求前设置 cwd，不依赖 Agent 事后切目录。
新客户端清除继承的父任务身份；恢复使用明确的原生会话 ID。缺失的旧目录、
冲突的身份和不支持的来源直接返回事实，不按最近会话猜测。

新副本有独立的 `.git`、文件和 index。复制保留 HEAD、暂存及工作区变更、
普通未跟踪文件、有效换行/文件模式设置和已初始化子模块的独立 Git 状态。
尚未初始化的空子模块保持未初始化，不拉取轻量任务不需要的源码。私有
运行状态和被忽略内容不复制。含非 Git 内容的未初始化 gitlink、冲突 index
或稀疏 index 等不支持的状态会明确失败；失败目录保留供检查。

已有 worktree 直接复用。Hook 通过 Git common directory 识别关联仓库，
不能用字符串父目录关系把嵌套的另一仓库当作同一项目。执行来源优先级为：
本次 run 指定的 sources、显式 task 默认值、该 attachment 的自动来源。
更新 attachment 的 cwd 不覆盖其他 attachment，也不改变已接纳执行。

## Windows 与 WSL

共享 Windows owner 使用它能访问的 mounted-drive 来源。Linux home 中的
原生副本不自动获得 Windows owner 可访问性。统一入口默认在项目所在盘
创建目录；挂载盘上的复制由准备好的 Windows Python、Git 和文件 API 完成。

这样保留 Windows 链接类型和 Git 语义，并避免让另一端解析包含操作系统
绝对路径的 linked-worktree 指针。它是一次有界复制操作，不新增工作树
管理服务。独立 Linux 或 macOS 工作区使用原生实现。

## 固定的本地环境

依赖环境由平台、架构、实际 Python/ABI、lock 和有效依赖选择决定内容键。
相同输入复用已经完成的环境；显式 sync 构建缺失环境。普通启动读取 ready
receipt，不安装依赖或运行全量 doctor。已发布环境不原地升级或搬迁。

原生客户端子进程的 PATH 前置该环境的 Scripts/bin，并设置 VIRTUAL_ENV、
清除冲突的 PYTHONHOME。裸 python 与公共 uv 入口因此使用同一套本地依赖，
不需要 PowerShell、bash 或 zsh 激活命令，也不修改用户全局环境。依赖变更
通过更新项目声明并 sync 准备另一环境，不能用 pip 原地修改共享已发布环境。

Hook/MCP 固定解释器和 receipt。WSL 原生 Python 与 Windows 托管 owner
分别固定，避免运行中修改 lock 后切换另一方版本。共用配置所需的相对
Windows Python 链接按内容键生成且不改向。编辑副本继承依赖选择，不复制
任务执行状态或资源记录。

客户端信任和审批遵循用户授权，由客户端自身配置。Setup 只生成当前平台
可用的本地配置，不默默更改这些策略。完整合同见
[platform-contract.md](platform-contract.md)。
