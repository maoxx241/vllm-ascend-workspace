# Agent CLI 与 MCP

`vaws-top` 将 Agent 查询统一交给常驻采集器。默认 `cache` 模式立即读取内存快照，不建立 SSH；显式 `live` 模式由采集器发起一次集中探查并等待新快照。Agent 无需自行拼接 SSH 命令，也不会接触远程密码或监控密钥。

## CLI

在独立 worktree 根目录运行：

```bash
python3 scripts/vaws-top.py servers
python3 scripts/vaws-top.py npu 10.18.4.21
python3 scripts/vaws-top.py npu 10.18.4.21 --live
python3 scripts/vaws-top.py npu 10.18.4.21 --ultra-compact
python3 scripts/vaws-top.py --json npu 10.18.4.21 --processes
python3 scripts/vaws-top.py status 10.18.4.21
python3 scripts/vaws-top.py mounts 10.18.4.21 --live
python3 scripts/vaws-top.py capacity --min-idle 4 --max-age 180 --tag A3
```

默认输出只保留状态、缓存年龄、忙闲卡数、AICore、HBM、进程数和归属：

```text
10.18.4.21 online age=4s npu=8 busy=2 util=24.5% hbm=96.0G/512.0G
0 busy util=91% hbm=41.2G/64.0G proc=1 owner=q00946761,wbj
1 idle util=0% hbm=5.9G/64.0G proc=0 owner=-
```

需要机器可读结果时使用 `--json`。`--processes` 加入精简进程记录；`--process-details` 进一步加入 pwd 和启动命令。`--max-age 180` 可约束缓存新鲜度，超限时保持输出并以退出码 `3` 标记陈旧。需要重新采样时显式使用 `--live`，实时请求由服务内采集队列执行，调用端不会直接 SSH。

`status` 汇总 NPU、CPU、内存、磁盘、Docker、占用进程/容器及可能的工号或姓名缩写；`mounts` 返回挂载源、文件系统、容量，并标出可能存放模型权重的挂载点；`capacity` 从新鲜缓存中筛选满足空闲 NPU 数量和标签的机器，低优先级服务器排在最后。

默认 API 是 `http://127.0.0.1:8789`，可通过 `--url` 或 `VAWS_TOP_URL` 修改为其他回环端口。为避免误将无认证接口暴露到网络，非回环 URL 默认拒绝。

## MCP

MCP server 使用标准输入输出传输，后端仍通过同一回环缓存 API 取数：

```toml
[mcp_servers.vaws_top]
command = "python3"
args = ["/absolute/path/to/npu-fleet-monitor/scripts/vaws-top-mcp.py"]
env = { VAWS_TOP_URL = "http://127.0.0.1:8789" }
```

Windows 可将 `command` 换成已安装的 `python.exe` 绝对路径。服务提供五个只读工具：

- `npu_status(host, mode?, include_processes?, process_details?, max_age_seconds?)`
- `server_status(host, mode?, process_details?, timeout_seconds?)`
- `list_mounts(host, mode?, timeout_seconds?)`
- `find_npu_capacity(min_idle_npus?, max_age_seconds?, tags?, include_disabled?)`
- `list_npu_servers()`

查询工具的文本结果与 CLI 一样精简，同时提供 `structuredContent`。`mode=cache` 适合快速初筛，`mode=live` 适合实验启动前最终确认。MCP server 兼容当前无握手请求和常见的旧版 `initialize` 客户端；stdout 只写逐行 JSON-RPC 消息，诊断只写 stderr。

## 只读 HTTP API

```text
GET /api/agent/servers
GET /api/agent/npu?host=10.18.4.21&mode=cache
GET /api/agent/npu?host=10.18.4.21&mode=live&processes=1&details=1
GET /api/agent/server?host=10.18.4.21&mode=cache
GET /api/agent/capacity?min_idle_npus=4&max_age_seconds=180&tags=A3
```

`host` 精确匹配服务器 IP、显示名称、远端 hostname 或内部 id。响应中的 `source=cache` 表示数据最终来自采集器快照；`age_seconds` 表示返回时的快照年龄。实时模式只是要求采集器先生成新快照，不会把 SSH 能力暴露给调用端。同一地址存在多个 SSH 端点时返回冲突错误，避免 Agent 猜测目标。
