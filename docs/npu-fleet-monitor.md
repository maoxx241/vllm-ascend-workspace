# NPU Fleet Monitor 本地运行

Status: current

监控前端和采集后端维护在独立仓库 [`vllm-ascend-workspace/vaws-top`](https://github.com/vllm-ascend-workspace/vaws-top)，并以 pip 包 `vaws-top`（console script `vaws-top`）发布。脚手架只保留一个薄入口 `npu-fleet-monitor`，用 `uvx` 把固定版本的 Release wheel 拉起为本机后台进程；运行时、高级 Agent Skill 和观测契约由该仓库自己拥有。观测记录明确携带 `allocation_authority: false`，不能用来分配 NPU、选择任务身份、杀进程或清理容器。Coordinator execution leases 仍是权威。

## 规范命令

所有调用都固定为 GitHub Release wheel：

```bash
uvx --from "https://github.com/vllm-ascend-workspace/vaws-top/releases/download/v0.1.0/vaws_top-0.1.0-py3-none-any.whl" vaws-top <子命令>
```

版本只有一个常量：`.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py` 里的 `VAWS_TOP_REF`（tag），wheel 文件名和下载 URL 都从它派生，不会出现 tag 与 wheel 版本各写各的。升级监控就是改这一个常量。不再有依赖 pin 文件、本地 checkout、`npm` 构建或用户级服务单元。

**为什么是 wheel 而不是 `git+https://…@v0.1.0`**：vaws-top 带 JS 前端，构建产物只存在于 Release wheel 里（`vaws_top/static` 不入库）。从 git 安装会在本机跑 hatch 构建 hook，需要 Node.js 22.13+；机器上没有 Node 时它会**静默**构建出一个没有前端的 wheel，直到 `serve` 才报错。这一条只针对 vaws-top；其余纯 Python 的外部包继续用 `git+tag`。

**运维契约**：vaws-top 发布新版本时必须把 wheel 上传为 Release 资产，否则本入口装不上监控。改 `VAWS_TOP_REF` 之前先确认对应 tag 的 Release 里有 `vaws_top-<版本>-py3-none-any.whl`。

开发者可以用 `--from <spec>` 或环境变量 `VAWS_TOP_FROM=<spec>` 覆盖安装来源（本地 wheel、源码树，或 `git+https://…`，后者需要 Node.js）。默认始终是 wheel。

`vaws-top` 的子命令：`serve`（在回环地址上同时提供 HTTP API 和静态前端）、`mcp`（stdio MCP）、`servers`、`npu`、`status`、`mounts`、`capacity`。`serve` 默认绑定 `127.0.0.1:8788`。

## 一键拉起

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py deploy   # 安装/缓存并验证包内前端存在，不起服务
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py start    # 后台启动 vaws-top serve
```

`deploy` 在已安装的包环境里执行 `require_static()`——与 `serve` 启动时相同的检查——并核对返回的 `vaws_top/static/index.html` 确实存在。没有前端的安装会在这里用包自己的报错失败，而不是等到第一次 `start`。新机器上先跑 `deploy`。

`start` 会：

1. 以 `uvx --from <wheel> vaws-top serve --bind 127.0.0.1 --port 8788` 启动一个独立进程组，stdout/stderr 追加到 `serve.log`，pid、端口和 spec 写入 `serve.json`。
2. 通过环境变量传入 `NFM_BIND=127.0.0.1`、`NFM_PORT`、`NFM_STATE_DIR`，以及消费者拥有的 `NFM_INVENTORY_FILES`、可选 `NFM_HOST_POOL_FILES`、`NFM_BOOTSTRAP_COMMAND`。优先级：显式 CLI 覆盖（`--inventory-files`、`--host-pool-files`、`--bootstrap-command`）> 调用者环境 > 脚手架默认（主 worktree 的 `.vaws-local/machine-inventory.json`、存在时的 `hosts.txt`、`machine-management` 的 `bootstrap-host-key --password-stdin`）。
3. 绕过系统 HTTP 代理轮询 `http://127.0.0.1:8788/api/health`（`--wait-seconds`，默认 90 秒；进程提前退出时立即返回并附日志尾部），最终只在标准输出打印一条 JSON。

若 `serve.json` 里的 pid 仍存活，`start` 直接报告现有实例（含它当时的 spec）而不重复启动。

浏览器入口为 <http://127.0.0.1:8788>。Web 和 API 固定在回环地址（脚本总是传 `--bind 127.0.0.1`，没有改绑定的参数），不提供用户登录，也不应通过端口转发或反向代理对外开放。

## 数据与设备发现

运行时状态全部位于主 worktree 的未跟踪目录 `.vaws-local/npu-fleet-monitor/`：

- `serve.json`：pid、端口、spec、启动时间
- `serve.log`：服务标准输出/错误
- `data/`（`NFM_STATE_DIR`）：SQLite 历史库、专用 Ed25519 密钥、独立 `known_hosts`、SSH 控制套接字

`restart` 和升级版本都不会删除这些文件。监控按 `(host, port, user)` 合并观测，不会获得分配权。不要打印清单内容、凭证或密钥。

## 日常操作

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py status
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py restart
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py stop
```

`status` 读取 `serve.json` 并探活；`stop` 向记录的整个进程组发 SIGTERM，超时后 SIGKILL，然后删除 `serve.json`。查看日志直接 `tail -f .vaws-local/npu-fleet-monitor/serve.log`。

进程不再由用户级服务管理器托管：宿主重启或登录会话结束后需要重新 `start`。这是从常驻用户服务换成 uvx 本地进程后有意接受的退化。

## Agent 查询入口

JSON 结果里的 `cli_prefix` 即上面的 `uvx` 命令前缀，`mcp_command` 是对应的 stdio MCP 命令（`… vaws-top mcp`，通过 `VAWS_TOP_URL` 指向 `http://127.0.0.1:8788`）。完整的 CLI/MCP 参数、`observation` 信封和高级选机指引见独立仓库 pinned tag 下的 [Agent CLI 与 MCP 文档](https://github.com/vllm-ascend-workspace/vaws-top/blob/v0.1.0/docs/agent-access.md) 与 [vaws-top Agent Skill](https://github.com/vllm-ascend-workspace/vaws-top/blob/v0.1.0/.agents/skills/vaws-top/SKILL.md)。

需要新增、修复或移除远程服务器时使用 `machine-management`，监控服务本身不创建远程容器、不启动任务，也不占用 NPU lease。
