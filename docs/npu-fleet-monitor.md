# NPU Fleet Monitor 本地运行

Status: current

监控前端和采集后端维护在独立仓库 [`vllm-ascend-workspace/vaws-top`](https://github.com/vllm-ascend-workspace/vaws-top)，并以 pip 包 `vaws-top`（console script `vaws-top`）发布。脚手架只保留一个薄入口 `npu-fleet-monitor`，用 `uvx` 把固定 tag 的包拉起为本机后台进程；运行时、高级 Agent Skill 和观测契约由该仓库自己拥有。观测记录明确携带 `allocation_authority: false`，不能用来分配 NPU、选择任务身份、杀进程或清理容器。Coordinator execution leases 仍是权威。

## 规范命令

所有调用都固定为：

```bash
uvx --from "git+https://github.com/vllm-ascend-workspace/vaws-top@v0.1.0" vaws-top <子命令>
```

tag 是 `.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py` 里的常量 `VAWS_TOP_REF`；升级监控就是改这一个常量。不再有依赖 pin 文件、本地 checkout、`npm` 构建或用户级服务单元。`uvx` 自己负责拉取、构建和缓存：从 git 构建时机器上需要一次 Node.js 22.13+（前端会编进 wheel），之后直接复用缓存。

`vaws-top` 的子命令：`serve`（在回环地址上同时提供 HTTP API 和静态前端）、`mcp`（stdio MCP）、`servers`、`npu`、`status`、`mounts`、`capacity`。`serve` 默认绑定 `127.0.0.1:8788`。

## 一键拉起

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py deploy   # 仅解析/构建/缓存包，不起服务
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py start    # 后台启动 vaws-top serve
```

`start` 会：

1. 以 `uvx --from … vaws-top serve --bind 127.0.0.1 --port 8788` 启动一个独立进程组，stdout/stderr 追加到 `serve.log`，pid、端口和 spec 写入 `serve.json`。
2. 通过环境变量传入 `NFM_BIND=127.0.0.1`、`NFM_PORT`、`NFM_STATE_DIR`，以及消费者拥有的 `NFM_INVENTORY_FILES`、可选 `NFM_HOST_POOL_FILES`、`NFM_BOOTSTRAP_COMMAND`。优先级：显式 CLI 覆盖（`--inventory-files`、`--host-pool-files`、`--bootstrap-command`）> 调用者环境 > 脚手架默认（主 worktree 的 `.vaws-local/machine-inventory.json`、存在时的 `hosts.txt`、`machine-management` 的 `bootstrap-host-key --password-stdin`）。
3. 绕过系统 HTTP 代理轮询 `http://127.0.0.1:8788/api/health`（`--wait-seconds`，默认 90 秒；进程提前退出时立即返回并附日志尾部），最终只在标准输出打印一条 JSON。

若 `serve.json` 里的 pid 仍存活，`start` 直接报告现有实例而不重复启动。首次在新机器上建议先跑 `deploy`，把 git 构建的耗时和 Node.js 缺失这类错误从服务启动中分离出来。

浏览器入口为 <http://127.0.0.1:8788>。Web 和 API 固定在回环地址（脚本总是传 `--bind 127.0.0.1`，没有改绑定的参数），不提供用户登录，也不应通过端口转发或反向代理对外开放。

## 数据与设备发现

运行时状态全部位于主 worktree 的未跟踪目录 `.vaws-local/npu-fleet-monitor/`：

- `serve.json`：pid、端口、spec、启动时间
- `serve.log`：服务标准输出/错误
- `data/`（`NFM_STATE_DIR`）：SQLite 历史库、专用 Ed25519 密钥、独立 `known_hosts`、SSH 控制套接字

`restart` 和升级 tag 都不会删除这些文件。监控按 `(host, port, user)` 合并观测，不会获得分配权。不要打印清单内容、凭证或密钥。

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
