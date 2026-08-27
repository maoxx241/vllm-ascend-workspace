# Windows 原生部署

Windows 部署使用当前用户的“任务计划程序”在登录后自动启动服务，不依赖 NSSM、Docker Desktop 或 WSL。任务只以当前用户的有限权限运行，前端和 API 始终绑定 `127.0.0.1`。

## 前置条件

- Windows 10/11 或 Windows Server 2019+；
- PowerShell 5.1+ 或 PowerShell 7；
- Node.js 22.13+；
- Python 3.11+；
- Windows OpenSSH Client（需要 `ssh.exe` 与 `ssh-keygen.exe`）；
- 能访问待监控 NPU 服务器的网络。

在“设置 → 系统 → 可选功能”中可以安装 OpenSSH Client。安装脚本会检查版本和命令是否可用，不会自动修改 Windows 系统功能。

## 安装

在项目根目录打开普通 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\install-windows-service.ps1
```

若监控 worktree 不在主工作区的 Git worktree 集合中，显式指定设备管理能力来源：

```powershell
.\scripts\install-windows-service.ps1 `
  -SourceWorkspace 'D:\work\vllm-ascend-workspace'
```

安装器会依次完成依赖安装、生产构建、后端测试、配置写入、当前用户登录触发器注册和健康检查。它不要求管理员权限，也不会把服务暴露到局域网。

常用可选参数：

```powershell
.\scripts\install-windows-service.ps1 `
  -WebPort 8788 `
  -ApiPort 8789 `
  -StateDir 'D:\npu-fleet-monitor-data' `
  -IdleInterval 120 `
  -HistoryInterval 30 `
  -InfrastructureInterval 60
```

服务启动后访问 `http://127.0.0.1:8788`。

## 管理

```powershell
.\scripts\manage-windows-service.ps1 status
.\scripts\manage-windows-service.ps1 restart
.\scripts\manage-windows-service.ps1 stop
.\scripts\manage-windows-service.ps1 start
.\scripts\manage-windows-service.ps1 logs
```

卸载只移除计划任务，保留 SQLite 历史、专用 SSH 密钥和配置：

```powershell
.\scripts\manage-windows-service.ps1 uninstall
```

运行状态保存在 `data\`（或 `-StateDir` 指定目录），任务配置保存在 `data\windows-service.json`，日志保存在 `data\logs\windows-service.log`。这些文件均在 Git 忽略范围内。

## Windows 行为差异

- Windows OpenSSH 不使用 Unix-domain `ControlMaster` 控制套接字；采集仍使用同一专用 Ed25519 密钥。
- 首次生成私钥后，服务使用 `icacls` 移除继承权限并只授权当前 Windows 用户读写，满足 Windows OpenSSH 的私钥权限检查。
- 登录触发器适合本机个人部署：用户登录后自动拉起，异常退出最多每分钟重启一次、连续尝试十次。
- 一次性密码引导仍依赖 `NFM_SOURCE_WORKSPACE` 指向的主工作区设备管理脚本；已有密钥和手动添加服务器不依赖该入口。
