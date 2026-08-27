# NPU Fleet Monitor

面向本地独立部署的 Ascend NPU 服务器监控台。它通过宿主机 SSH 无代理采集，不提供任务启动或调度能力，重点展示 NPU 利用率、HBM、CPU、系统内存、磁盘、挂载点和 Docker 容器，并以 SQLite 保存历史数据。

## 主要能力

- 批量添加服务器；先尝试项目专用密钥，再按顺序尝试本次请求中的密码候选。
- 启动时自动发现 Git 公共目录对应主工作区的 `.vaws-local/machine-inventory.json` 和完整 `hosts.txt` 主机池，导入全部宿主机；只读取 `hosts.txt` 每行第一个地址字段，忽略其余字段。未进入活动 inventory 的设备自动获得“低优先级”标签。
- 密码只通过请求内存和标准输入传给工作区设备管理脚本，不写数据库、文件、命令参数或应用日志。
- 成功认证后自动安装专用 Ed25519 公钥；后续采集使用密钥和复用的 OpenSSH 控制连接。
- Host Key 使用独立 `known_hosts`，首次连接记录指纹，指纹变化时 OpenSSH 会拒绝连接。
- 在裸机宿主机运行 `npu-smi`，可跨容器观察占用；复用工作区 `npu_occupancy.py` 中经过测试的多种 Ascend 输出解析。
- 判忙优先进程与 AICore 利用率，HBM 作为兜底；默认 8 GB 阈值避开当前 A3 机器约 6 GB 的驱动常驻占用，可通过 `NFM_HBM_BUSY_THRESHOLD_MB` 调整。
- 页面可选 1、5、10、30 秒实时刷新；多个页面同时打开时采用最快频率。
- 页面关闭或心跳过期后自动回到默认 120 秒巡检；磁盘、挂载和 Docker 默认 60 秒一次。
- 交互态高频结果保留在内存，SQLite 默认最短每 30 秒落一条，避免历史库随 1 秒刷新膨胀。
- 固定服务器侧栏汇总在线状态、NPU 数量、HBM 和 CPU；总览展开物理 die 展示逐 die HBM 占用（A3 为 8 张逻辑卡、16 个 die），0%–50% 由浅蓝渐变、50% 以上固定深蓝，AICore 非零时以右上角红色闪光点提示。点击服务器可按逻辑卡分组查看两个 die，以及逐卡 AICore/HBM、温度、功耗、进程、磁盘与 Docker 明细。
- 服务器标签可在管理页新增、编辑并通过侧栏搜索；主工作区状态派生的“低优先级”标签在后续同步中自动维护，低优先级服务器排在普通服务器之后。
- 历史报表覆盖 1 小时到 90 天，包含聚合趋势，以及按日期和 2 小时时段排列的 CPU、内存、NPU、HBM 与逐卡 AICore 热力图；原始数据默认保留 90 天。
- 默认只监听 `127.0.0.1`，不含登录功能，也不应直接暴露到外网。

## 目录和依赖

项目依赖 Node.js 22.13+、Python 3.11+、系统 OpenSSH 客户端和 `ssh-keygen`。后端只使用 Python 标准库。支持 Linux systemd user service 和 Windows 原生任务计划程序两种持续运行方式。项目分支只包含监控服务本身，适合以独立 Git worktree 部署。服务通过 worktree 的 Git common-dir 自动定位主工作区，也可用 `NFM_SOURCE_WORKSPACE` 显式指定，然后复用：

- `.agents/skills/machine-management/scripts/manage_machine.py`：一次性密码引导和公钥安装；
- `.agents/skills/machine-management/scripts/npu_occupancy.py`：Ascend NPU 解析；
- 工作区的 SSH preflight、宿主机优先探测和 HBM 兜底判忙约定。

运行时私有状态保存在 `data/`：SQLite、专用密钥、`known_hosts` 和 SSH 控制套接字。该目录已忽略，不会进入 Git。

自动导入只读取机器清单中的宿主机地址、端口、用户和机器类型；不会采用容器端口，也不会创建或修改容器。已有数据库记录优先，不会在每次启动时重命名用户维护的服务器。

## 本地运行

```bash
npm install
npm run build
npm run test:backend
npm run serve:local
```

浏览器访问 `http://127.0.0.1:8788`。前端服务将 `/api/*` 固定代理到回环地址的后端端口 `8789`。

开发时分别运行：

```bash
PYTHONPATH=backend python3 -m npu_fleet_monitor
npm run dev -- --hostname 127.0.0.1
```

## 持续运行

若当前 Linux 用户已启用 systemd user manager：

```bash
./scripts/install-user-service.sh
```

服务安装后可用以下命令检查：

```bash
systemctl --user status npu-fleet-monitor
journalctl --user -u npu-fleet-monitor -f
```

环境变量示例见 `.env.example`。部署脚本不会修改远程服务器上的系统配置，只会在目标 SSH 用户的 `authorized_keys` 中幂等加入监控公钥。

### Windows

在项目根目录的普通 PowerShell 中运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-windows-service.ps1
.\scripts\manage-windows-service.ps1 status
```

Windows 版本使用当前用户的任务计划程序在登录后启动，异常退出自动重试，仍然只监听 `127.0.0.1`。无需 NSSM、Docker Desktop 或 WSL；完整安装、参数、日志和卸载说明见 [Windows 原生部署](docs/windows-deployment.md)。

## 批量格式

界面中每行一台服务器：

```text
名称, 主机, SSH端口, SSH用户, 标签1|标签2
atlas-a3-01, 10.18.4.21, 22, root, A3|训练
```

密码候选一行一个。服务对每台尚未配置密钥的主机按顺序尝试，成功即停止；请求完成后不保留候选密码。

## 设计来源

产品交互参考了 [RackTop](https://github.com/Tongzh-SEU/RackTop) 的多服务器资源总览、空闲算力发现和历史热力图思路；实现代码为独立编写，并针对当前工作区的 Ascend 设备管理和无代理宿主机探测进行了适配。
