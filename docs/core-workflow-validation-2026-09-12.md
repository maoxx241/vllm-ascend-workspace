# 核心执行流程与真实客户端验证

Status: dated validation evidence, 2026-09-12.

本轮按照[九条设计原则](design-principles.md)调整 remote-dev 0.7 与
coordinator 0.4。当前调用合同见 [coordinator-consumption.md](coordinator-consumption.md)
及 [remote-dev-consumption.md](remote-dev-consumption.md)；此记录不增加任务前置步骤。

## 改变及其原因

| 实际触发场景 | 结果 |
|---|---|
| 提交 A、B 后继续修改本地源码 | admission 固定每次输入；执行使用独立源码根，后续修改不影响已接受的执行 |
| 普通命令、远端阅读 | 按需使用原生工具或 remote-dev；托管命令可传 `sources={}`，默认零 NPU |
| Python-only、native 代码与环境变化 | 分别匹配源码、依赖和原生产物；复用检查由包完成，保留失效原因 |
| 四角色同时启动 | 并发准备、持续观察及统一启动门控，避免顺序推进耗尽预备期限 |
| 长编译期间查看或停止 | 控制请求保留容量；准备阶段保存远端进程身份，取消后确认退出再回收 |
| 环境初始化定义函数、PATH、shell 选项 | 初始化与用户命令在同一 Bash 中执行，保留正常 shell 启动语义 |
| Windows 传输较长生成脚本 | 使用二进制 stdin，避免命令行长度上限 |
| Grok 搜索遇到不支持的参数 | 明确返回错误及修正线索，精简重复连接策略参数 |
| Codex/Kimi 查询刚提交的执行 | 正常 queued/preparing 返回成功；健康 runtime 事实压缩，异常与完整记录保留 |
| Windows、WSL 共用工作区 | 托管入口使用同一 Windows 所有者；Kimi 共享配置可在两个系统实际启动 |
| 从未提交托管执行的本地任务结束 | 本地事务关闭 admission，与并发提交原子互斥；不导入 service 或启动 daemon |

旧产物不能仅凭路径或版本标签复用：候选必须匹配当前构建输入，复制后的
native 文件及生成 metadata 必须符合原始 manifest 的 hash。缺失证明会退回
依赖复用或重建。任务、角色的完整身份参与执行根命名，避免长角色名截断碰撞。

## 四机与执行语义

四个既有远端容器均完成实际 gcc 编译，输出 `42 中文` 和产物 SHA256。
随后 Python 验证执行目录与空设备列表；四角色均成功且资源释放。
首次尝试暴露的预备期限问题已保留为失败证据，未计入成功结果。

两个带源码执行提交后，本地文件改成 C，远端仍分别输出 `accepted-A` 和
`accepted-B`；本地 HEAD/index 未改。两次提交分别耗时 0.969 s、0.871 s。
退出码 7 保留 stderr；3 秒超时正确终止；长命令停止后取消并回收。

真实 C++ 编译开始后请求取消，相关 preparation job 的 fresh status 均为
quiet、无存活进程、descendants drained。独立观察中，停止至最终确认约
5.021 s；容器启动身份未变，取消前仍存活的既有长期进程全部保留。
最终独立审计的 31 个原生准备/业务作业均 fresh quiet，无存活或身份未知
的受管进程，descendants drained。原生实验涉及的两个既有容器身份保留。
完整验收 registry 的 22 次执行全部进入终态且资源释放，包含保留的失败和
取消记录；没有将这些终态记录都计为成功测试。

Windows 与 WSL 的安装版 remote-dev 均完成四容器环境函数、PATH、cwd、
管道退出码、PTY 输入及停止检查。300,055-byte 生成脚本两端均实际完成，
单次约 4.8 s。

另从已完成执行通过正式 artifact-pull 下载编译产物；远端执行记录、远端
manifest 和本地文件 SHA256 一致，文件为 AArch64 ELF。下载没有重放编译。

## 真实客户端负载

使用已登录的 Grok、Kimi Code 与 Codex CLI/ACP，没有模拟模型输出。
普通问答和本地阅读没有托管执行或环境准备步骤。以下是单次观测，不是
不同模型的排名，也不是延迟承诺；各客户端调用范围不同。

| 场景 | Grok WSL | Kimi Windows | Kimi WSL | Codex Windows |
|---|---:|---:|---:|---:|
| 简短解释，0 工具 | 6.38 s | 9.61 s | 8.90 s | 15.48 s |
| 读取本地短文件、识别 3 条待处理 | 10.24 s | 11.70 s | 7.94 s | 21.95 s |
| MCP 远端读取 | 16.20 s | 26.13 s | 28.90 s | 31.30 s |

Codex 远端场景另含本地摘要及远端 read/grep。Grok 综合负载执行了 8 个
并发短命令、2000 行有游标日志，以及两个真实 C++ 编译 worker；产生
35 个非空 object，运行中 status/stop 可用，停止后 quiet。该轮 138.12 s、
26 次工具调用。

Grok 搜索单次失败恢复在修复前后分别为 60.54 s / 16 次工具和
23.52 s / 2 次工具。MCP schema 从 48,056 降至 25,664 bytes，减少 46.6%；
单次模型耗时变化不用于推断统计显著的性能提升。

最终 Codex 托管 CPU 流程 86.292 s，7 次 MCP 调用均 completed，确认
running、日志标记、cancelled 与资源释放。Kimi Windows 同类流程
113.73 s；Kimi WSL 的较早一轮 338.07 s 包含修复前 `topology.host`
被忽略后的返工。该问题及一致的 `devices=[]` / `npu_count=0` 输入
被错误拒绝的问题均已修复。

从实际 Codex 回复投影得到的健康 runtime 部分为 1875 → 86 bytes，
整个结构为 2859 → 1070 bytes。异常、版本差异、完整记录和 target
请求继续保留原始细节。

五客户端 session hook 在 Windows/WSL 完成真实往返；同一份 Kimi
MCP 配置经过 Windows → WSL → Windows 启动、环境变量、session 及远端
读取验证。production 再次预览均无变更，原有用户配置与信任设置保留。

## 原生编译与验证范围

两台机器的较早基线通过实际 vLLM/vllm-ascend/native 扩展导入，以及
NPU 张量 `[2,4,6]` 烟测；导入路径来自各自执行根。最终严格证明下的
完整基线耗时 832.058 s，记录 1103 个产物 hash；Python-only 修改耗时
228.367 s，加载新 Python 标记而所有产物 hash 与基线相同，无 venv 或
install 阶段。native 修改耗时 840.821 s，实际扩展返回新增常量 `7`，
主 `.so` hash 和 native 构建键改变，依赖键保持相同，使用独立解释器。
切回基线耗时 220.215 s，SCM 与源码 ID 恢复，全部 1103 个产物 hash 及
主 `.so` 与基线完全一致，无 venv 或 install 阶段。四轮均成功并释放资源。
这些是各一次真实运行的耗时；缓存复用结论来自构建阶段与完整产物身份，
不是仅根据时间更短作推断。

vLLM 的源码 SCM 版本及 `vllm.__version__` 为 0.27.1；既有
`VLLM_TARGET_DEVICE=empty` 安装配方将 distribution 版本写为
0.27.1+empty。记录保留这个差异，不把它误报为丢失源码身份。
这些检查不等于完整模型服务、四节点推理或吞吐回归。

## 本地工作区隔离

真实 Grok 并行请求的首条工具命令位于两个不同目录；按原 ID 恢复会回到
原目录，父 checkout 的 HEAD、index 和文件内容未变。实际客户端目录隔离
与 VAWS 仓库关联是不同事实：Grok 本机创建的是独立 Git 副本，默认 Linux
home 路径也不在本轮 Windows 所有者的 mounted-drive 来源范围内。

本轮修正 owner-accessible linked worktree 的 hook 范围及 attachment
cwd/自动来源更新；没有宣称 hook 可以移动客户端目录。固定平台路径的
本地 venv 仍会被同目录会话共享，内容键不可变环境尚属后续改造。
完整边界和方案见 [原生工作区隔离](native-workspace-isolation.md)。

## CI

保留跨平台行为测试、干净 wheel 安装和消费者完整测试。真实 GitHub
Windows CI 揭示相同计时 tick 下的 LRU 淘汰错误，已改为完成顺序并保留
固定时钟回归。测试所需 jsonschema 显式声明为 test extra。
最后 attachment/source 补丁的协调器完整测试为 Windows 326 passed、
7 skipped，WSL 331 passed、2 skipped；两端均另含 50 个通过的 subtests。

独立 Windows 消费者工作树完整运行 57 个 suites，1610 个 JUnit cases，
0 failures/errors、3 skipped；Linux 消费者 CI 通过。首轮 Windows CI
暴露测试把临时目录的 8.3 短路径与已规范化长路径作字符串比较，已改为
检查实际 context 文件及其归属目录，相关 39 项测试通过。

移除重复 compile/schema gate、只检查声明存在的测试，以及把源码 pin
字符串再抄一遍的测试。公共 CLI 表删除选项和引用数量，保留入口与职责
校验；增删内部测试不再强制修改用户文档。消费者 CI 使用已有完整 runner，
继续覆盖 20 个 skill suites、生成投影、真实追踪文件泄漏扫描及资产导入；
边界、路径与 shell 语法检查保留。

包变更： [remote-dev PR #10](https://github.com/vllm-ascend-workspace/remote-dev/pull/10)、
[coordinator PR #19](https://github.com/vllm-ascend-workspace/vaws-coordinator/pull/19)。
原始日志和本地身份信息留在未追踪的本地验证目录；公开材料只包含汇总证据。
