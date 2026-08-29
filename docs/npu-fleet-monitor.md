# NPU Fleet Monitor 本地部署

监控前端和采集后端维护在独立分支 `vaws-top`。该分支的项目文件位于分支根目录，拥有独立的项目历史；`main` 保存部署入口和操作说明。

## 一键拉起

在主工作区执行：

```bash
python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py ensure
```

该命令会：

1. 首次使用时从已配置的 Git 远端获取 `vaws-top`，然后复用对应 worktree；若不存在，则创建到 `~/vaws-worktrees/<仓库名>/npu-fleet-monitor`。
2. 确认监控 worktree 没有未提交的源码修改；被项目忽略的运行时数据不受影响。
3. 提交变化时执行 `npm ci`、后端测试和生产构建；相同提交和完整构建会直接复用。
4. 安装、启用并重启 `npu-fleet-monitor.service` 用户服务。
5. 绕过系统 HTTP 代理检查 `http://127.0.0.1:8789/api/health`，最终只在标准输出打印一条 JSON 结果。

浏览器入口为 <http://127.0.0.1:8788>。Web 和 API 均固定在回环地址，不提供用户登录，也不应通过端口转发或反向代理对外开放。

## 数据与设备发现

监控 worktree 的 `data/` 是忽略目录，保存 SQLite 历史库、专用 Ed25519 密钥和独立 `known_hosts`。重新构建和重启不会删除这些文件。

服务根据 Git common-dir 找到主工作区，读取主 worktree 的共享 `.vaws-local/machine-inventory.json`，复用 `machine-management` 的密钥引导和 Ascend NPU 解析。其他 linked worktree 读取同一库存，但 session、serving、benchmark、profiling 等执行状态仍保存在各自 worktree。若监控 worktree 不属于同一个 Git 公共目录，可在服务环境中设置 `NFM_SOURCE_WORKSPACE=/绝对/主工作区/路径`。

浏览器没有活动页面时，采集器默认每 120 秒执行一次低频巡检；页面打开后由可见页面选择的 1、5、10 或 30 秒频率控制，多个页面采用最快频率。磁盘、挂载点和 Docker 使用独立的低频采集周期，历史数据最短每 30 秒落库一次。

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
