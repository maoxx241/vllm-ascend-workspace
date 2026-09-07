# NPU Fleet Monitor 本地部署

监控前端和采集后端维护在独立仓库 [`vllm-ascend-workspace/vaws-top`](https://github.com/vllm-ascend-workspace/vaws-top)。脚手架只保留部署入口 `npu-fleet-monitor`；运行时、高级 Agent Skill 和观测契约由该仓库自己拥有。观测记录明确携带 `allocation_authority: false`，不能用来分配 NPU、选择任务身份、杀进程或清理容器。Coordinator execution leases 仍是权威。

## 一键拉起

在主工作区执行：

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py ensure
```

该命令会：

1. 按显式路径或文档化默认目录定位独立仓库 checkout：`--clone-dir`、`VAWS_TOP_ROOT`，否则 `~/vaws-worktrees/<仓库名>/npu-fleet-monitor`。首次在空目录克隆 `https://github.com/vllm-ascend-workspace/vaws-top`，并检出 `.agents/deps/vaws-top.json` 中的精确 commit。SSH 与 HTTPS 源视为同一仓库。
2. 若默认目录仍是脚手架遗留的 `vaws-top` worktree（与当前仓库共用 Git 目录，且可能含私有运行时数据），则失败并要求另选目录；不会改 origin、reset、删除、detach、搬移数据或导入密钥。
3. 确认 checkout 没有未提交的源码修改；被项目忽略的 `data/` 和 `.env` 不受影响。脏树、分叉 checkout 或缺失 pin 都会失败，而不是静默前进。
4. 提交变化时执行 `npm ci`、后端测试和生产构建；相同提交和完整构建会直接复用。只有 `ensure` 会做这些事。
5. 原子更新 clone 根目录 `.env` 中由消费者拥有的 `NFM_INVENTORY_FILES`、可选 `NFM_HOST_POOL_FILES`、`NFM_BOOTSTRAP_COMMAND`；保留 `NFM_STATE_DIR`、监听地址和其他已有覆盖。systemd unit 通过 `EnvironmentFile` 读取该文件；`start.sh` 不会 source 它。
6. 安装、启用并重启 `npu-fleet-monitor.service` 用户服务。
7. 绕过系统 HTTP 代理检查 `http://127.0.0.1:8789/api/health`，最终只在标准输出打印一条 JSON 结果。

浏览器入口为 <http://127.0.0.1:8788>。Web 和 API 均固定在回环地址，不提供用户登录，也不应通过端口转发或反向代理对外开放。

`status`、`restart`、`stop` 和帮助不会 clone、fetch、checkout、install 或 build。

## 数据与设备发现

监控 clone 的 `data/` 是忽略目录，保存 SQLite 历史库、专用 Ed25519 密钥和独立 `known_hosts`。重新构建和重启不会删除这些文件。

vaws-top 不再搜索 Git 或 `NFM_SOURCE_WORKSPACE`。脚手架通过当前共享状态助手解析清单路径，并只把**文件路径**写入 `.env`：

- `NFM_INVENTORY_FILES`：主 worktree 的 `.vaws-local/machine-inventory.json`；若存在，可附加仓库根目录的兼容 `.machine-inventory.json`
- `NFM_HOST_POOL_FILES`：仅当 `hosts.txt` 存在时写入
- `NFM_BOOTSTRAP_COMMAND`：当前 `machine-management` 的 `manage_machine.py bootstrap-host-key`，密码只经 stdin，不出现在模板里

显式 CLI 覆盖优先于已有 `.env` 值，再才是上述默认。监控按 `(host, port, user)` 合并观测，不会获得分配权。不要打印清单内容、凭证或完整 `.env`。

## 日常操作

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py status
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py restart
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py stop
```

直接查看服务日志：

```bash
systemctl --user status npu-fleet-monitor.service
journalctl --user -u npu-fleet-monitor.service -f
```

首次执行和涉及 systemd/OpenSSH 的操作应在宿主执行面运行。需要新增、修复或移除远程服务器时使用 `machine-management`，监控服务本身不创建远程容器、不启动任务，也不占用 NPU lease。
