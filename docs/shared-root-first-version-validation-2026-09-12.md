# 个人 Fork、更新和共享开发机第一版验证

Status: dated validation evidence — 2026-09-12

第一版已实现并安装到本地工作区。初始化确认用户后，身份、固定用户容器、
留言收取及编译缓存复用接入既有工具；没有新增独立鉴权、共享权限配置、
产物发布或 Agent 日常登记。当前证据覆盖本地控制面和真实文件复用，
服务器上的 NPU 执行仍需后续实测。

## 被测版本

- workspace 基于官方提交 `b63c78269549e0f16ae6c4554f0b47b5e774a3da`。
- coordinator `0.4.1.dev1`，个人 Fork 提交
  [`0d0e814dc673677be62d2fa60e204552d57e22ce`](https://github.com/maoxx241/vaws-coordinator/commit/0d0e814dc673677be62d2fa60e204552d57e22ce)。
- workspace 的 `pyproject.toml`、`uv.lock` 固定上述测试提交，使用既有不可变
  环境机制安装。remote-dev 保持锁定的 `0.7.0`，knowledge 和 monitor 未改版本。
- 本轮没有创建正式 Release。个人 Fork 分支用于试用和后续审阅。

## 实际初始化和安装

真实 GitHub API 确认已保存用户与认证账户一致，创建/复用个人 workspace Fork。
原仓库名重定向到组织上游时，GitHub 返回带后缀的个人仓库名；工具验证并保存
实际地址，重复执行沿用它。开发 `origin` 已切为个人 Fork，canonical 保留为
`upstream`。子模块 gitlink 和社区 `.gitmodules` URL 未变。

coordinator 从固定 Git 提交构建、安装成功，安装包消费者测试未使用源码目录
覆盖导入路径。已有 Codex 的 VAWS 任务配置和 session hooks 已更新到选定环境，
保留私有备份及其他客户端配置。现有会话的 MCP 进程需在客户端重新加载后采用
新入口，文件写入本身不声称重启了客户端。

本机旧 daemon 从 coordinator `0.3.2` 经既有 `restart_if_idle` 自动升级为
`0.4.1.dev1`，耗时约 1.2 秒；前后 ping 记录确认已加载版本及提交。
同一进程中的 remote-dev 从旧 `0.5.1` 随环境切为已锁定的 `0.7.0`。
没有强制终止执行或重建远端容器。

## 测试结果

| 范围 | 结果 | 证据重点 |
|---|---|---|
| coordinator 全套本地测试 | 377 passed、6 skipped、47 subtests passed | 用户绑定与恢复、已有生命周期、留言、缓存和空闲升级 |
| workspace 受影响的 9 个测试文件 | 138 passed、3 skipped、32 subtests passed | Fork/更新/原生入口、环境固定、安装包消费者与官方 MCP SDK stdio |
| 文档和仓库检查 | 通过 | Skill 投影一致、tracked path 检查、Git diff 格式 |

跳过项保留各平台适用条件；本轮在 macOS 执行，没有据此声称完成 Windows
原生 IPC 或 Linux/NPU 实机验证。子测试数单独列出，不重复计入 passed。

留言测试使用两个独立客户端存储和同一宿主机 SQLite 数据库，验证线程/回复、
持久游标、自动归属、重复读取和后台收件；正常调用不等待 SSH，普通本地任务
不扫描集群。消息不执行命令，也不改变既有资源队列的分配事实。

编译缓存测试包括真实 CPython native 扩展的保存、复制和加载，相关构建输入
或兼容条件变化时重新编译，以及缓存加载失败后的原有准备路径。
取消或状态不确定的准备不会被缓存回退吞掉；命中后 import 检查使用当前任务
路径，不误用镜像内旧包；命中成功不再次发布整份产物。
权重复用验证了既有共享路径的挂载命令生成，没有新增权重扫描或全量哈希。

## 更新和实机边界

真实 Release 查询返回 `pending / no_stable_release`。新版本发现、按 Release
SHA 准备依赖、个人 Fork 快进、脏目录/分叉保留和新编辑副本采用准备结果，
通过本地 Git 仓库及 API fixture 验证；尚无真实官方 Release 可验证完整传播。
后台约五分钟检查一次，现有编辑目录不被后台替换。

本轮未执行多用户真实 SSH 留言、Ascend 算子编译命中、模型权重加载或 NPU
资源协调验收。第一版也不包含客户端主动唤醒、跨 GitHub 改名的容器迁移、
自动搜索历史编译目录和任意模型名解析。

完整原始日志、JUnit XML、依赖安装结果、配置备份引用和 daemon 前后版本
保存在本地未跟踪的 `.vaws-local/implementation/20260912-shared-root/`。
公开记录只描述结果和边界，不复制服务器、客户端路径或凭据。
