# vllm-ascend-workspace

**中文** | **[English](README.en.md)**

一个可组合的本地开发脚手架，让你在同一个工作区里同时开发 [vLLM](https://github.com/vllm-project/vllm) 和 [vLLM Ascend 插件](https://github.com/vllm-project/vllm-ascend)，并通过内置的 AI Agent 技能自动完成环境初始化、远程 NPU 机器管理和代码同步。

## 这个项目解决什么问题

vLLM Ascend 的开发通常需要在本地编辑代码、在远程昇腾 NPU 服务器上运行测试，同时还要跟踪上游 vLLM 的变化。手动维护这套工作流涉及大量重复的 Git、SSH 和环境配置操作。

`vllm-ascend-workspace` 把这些操作封装成三个 AI Agent 技能，你可以用自然语言让 Agent 代劳，也可以完全忽略这些技能、只把它当作一个普通的多仓库工作区。

## 快速开始

```bash
# 克隆仓库
git clone https://github.com/maoxx241/vllm-ascend-workspace.git
cd vllm-ascend-workspace

# 初始化子模块
git submodule update --init --recursive
```

如果你使用支持 Agent 的 IDE（Cursor、Windsurf 等）或终端工具（Claude Code、Codex CLI 等），可以直接用自然语言完成后续配置：

> "初始化这个工作区，帮我配好 vLLM Ascend 的开发环境。"

Agent 会自动检测你的环境、安装所需工具、配置 Git 远程仓库和 Fork。

## 内置技能


| 技能                     | 用途                                             | 何时使用               |
| ---------------------- | ---------------------------------------------- | ------------------ |
| **repo-init**          | 安装 GitHub CLI、登录 GitHub、初始化子模块、配置 Fork 和远程仓库拓扑 | 首次 clone 后初始化工作区   |
| **machine-management** | 添加、验证、修复或移除远程昇腾 NPU 服务器及其托管容器                  | 需要配置远程 NPU 开发机时    |
| **remote-code-parity** | 将本地工作区的完整状态（含未提交的修改）同步到远程容器                    | 在远程机器上运行测试或服务前自动触发 |


所有技能都是**可选的**。你可以只用其中的一部分，也可以完全不用。

## 使用示例

与 Agent 对话时，可以这样说：

```
# 初始化
"帮我初始化一下这个仓库"
"帮我配置一下这个仓库"

# 机器管理
"帮我添加一下这两台服务器，ip 是 x.x.x.1 和 x.x.x.2，密码是 xxxx"
"帮我配置一下这台服务器，ip 是 x.x.x.x，密码是 xxxx"
"帮我删除 x.x.x.x 服务器"

# 代码同步
"帮我同步代码到服务器上并重新编译"
```

## 仓库结构

```
.
├── vllm/                  # vLLM 上游（Git 子模块）
├── vllm-ascend/           # vLLM Ascend 插件（Git 子模块）
├── .agents/
│   ├── skills/
│   │   ├── repo-init/         # 工作区初始化技能
│   │   ├── machine-management/# 远程机器管理技能
│   │   └── remote-code-parity/# 代码同步技能
│   ├── lib/               # 共享本地状态库
│   └── scripts/           # 共享辅助脚本
├── .cursor/rules/         # Cursor IDE 专用规则
├── .trae/                 # TRAE IDE 专用规则与技能
├── AGENTS.md              # 跨工具 Agent 指令（AI Agent 读这个）
├── CLAUDE.md              # Claude Code 指令入口
└── README.md              # 你正在看的这个文件
```

## 设计原则

- **不强制任何流程** — 所有技能都可选，开发者自由选择使用哪些部分。
- **本地状态不入库** — 用户特定的远程仓库、认证信息、机器配置等只存在于本地未跟踪的 `.vaws-local/` 目录中。
- **子模块指向社区** — `.gitmodules` 始终指向 `vllm-project` 的官方仓库，个人 Fork 是本地运行时配置。
- **Agent 驱动，但不依赖 Agent** — 所有操作都可以手动完成，Agent 只是让流程更方便。

## 推荐的远程仓库拓扑

技能会推荐以下拓扑结构，但不强制要求：


| 仓库            | `origin`    | `upstream`                       |
| ------------- | ----------- | -------------------------------- |
| workspace     | 你的 Fork（可选） | `maoxx241/vllm-ascend-workspace` |
| `vllm`        | 你的 Fork（可选） | `vllm-project/vllm`              |
| `vllm-ascend` | 你的 Fork     | `vllm-project/vllm-ascend`       |


## 多工具支持

本仓库支持主流 AI 编程工具：


| 文件               | 覆盖工具                                    |
| ---------------- | ---------------------------------------- |
| `AGENTS.md`      | Codex CLI、GitHub Copilot、Cursor、TRAE、OpenCode |
| `CLAUDE.md`      | Claude Code                              |
| `.cursor/rules/` | Cursor                                   |
| `.trae/`         | TRAE                                     |


## 许可证

本脚手架仓库的许可证独立于子模块。`vllm/` 和 `vllm-ascend/` 各自遵循其上游项目的许可证。