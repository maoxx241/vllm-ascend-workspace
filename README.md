# vllm-ascend-workspace

**中文** | **[English](README.en.md)**

完全面向 Agent 的 [vLLM](https://github.com/vllm-project/vllm) 与 [vLLM Ascend](https://github.com/vllm-project/vllm-ascend) 开发工作区。人表达目标、决定实质取舍；Agent 完成代码修改、环境准备、实验执行和证据整理。

## 从任务开始

在 Agent 客户端中打开工作区，直接提出：

> 初始化这个工作区，配好 vLLM Ascend 的开发环境。

`repo-init` 技能复用已有配置，安装锁定依赖并接通所选客户端。Windows PowerShell 与 WSL 可以共享同一个检出目录，启动入口自动选择各自的平台环境。Agent 使用的安装方法见 [dependency-plane.md](docs/dependency-plane.md) 和 [windows-installation.md](docs/windows-installation.md)。

日常工作只需说明目标和影响结果的输入，例如：

- 用这份模型权重和启动参数拉一个四卡推理服务。
- 比较这两个 baseline/candidate 工作树的吞吐。
- 为这个 workload 采集 profiling，分析耗时算子。
- 找出 graph 与 eager 输出首次分歧的位置。
- 拉起本地 NPU 集群监控页面。

Agent 按任务选择工具或技能；执行引用、状态推进和报告由工具根据实际结果生成。缺失证据保留为未知或无法下结论。

## 设计与职责

后续变更以[核心设计原则](docs/target-state.md#11-agent-only-design-principles)为依据：

- 代码与命令入口完全围绕 Agent 使用设计。
- 封闭世界故障进入所属组件代码与回归测试；有用经验可用普通 Markdown 留存，保留条件、证据和不确定性。知识按需参考，查库和录入不成为任务步骤。
- 生命周期、校验和记录在各 owner 内部完成；业务入口接收业务输入和真实证据。
- 从完整任务衡量简化效果。未正式发布的接口直接替换，删除旧入口与兼容别名。

工作区负责项目材料、客户端接线和业务技能。`remote-dev` 负责明确 endpoint 的远程 I/O，`vaws-coordinator` 负责托管源码、环境、NPU 与执行，`vaws-knowledge` 负责 Markdown 知识查询和捕获，`vaws-top` 负责集群观察。观察不分配设备；任务身份来自原生客户端关联。已有容器和无关工作树继续保留。

## 业务技能

| 技能                       | 用途                                             | 何时使用               |
| ------------------------ | ---------------------------------------------- | ------------------ |
| **repo-init**            | 安装 GitHub CLI、登录 GitHub、初始化子模块、安装锁定的平台依赖、配置 Fork 和远程仓库拓扑 | 首次 clone 后初始化工作区   |
| **npu-fleet-monitor**    | 从独立 vaws-top 仓库构建、拉起、检查或停止本地 NPU 监控页面            | 需要持续查看设备、主机和历史资源状态时 |
| **modelscope**           | 下载、续传、查看进度并 SHA256 校验 ModelScope 模型权重                  | 需要把模型权重下载到明确目录时 |
| **vllm-ascend-serving**  | 在远程容器上一键拉起 vLLM Ascend 推理服务，由 coordinator 管理执行和资源 | 需要在远程机器上起推理服务时     |
| **vllm-ascend-benchmark** | 在远程容器上运行 `vllm bench serve` 性能基准测试，支持多轮预热和统计聚合     | 需要测量吞吐或延迟时 |
| **ascend-memory-profiling** | 采集并分析昇腾 NPU 的 HBM 显存占用，按组件拆分并溯源 | 需要分析 vLLM 推理服务的显存占用时 |
| **ascend-profiling-collection** | 采集 Ascend torch profiler：起服务、控制 profile 窗口、运行 workload、远端 analyse 并写 manifest | 需要采集 kernel_details/trace_view 时 |
| **ascend-profiling-analysis** | 分析已采集的 profiler root/manifest，生成 step/layer/operator/cross-rank 诊断报告 | 需要分析 profiling 结果或生成报告时 |
| **vllm-ascend-graph-debug** | 定位图编译、捕获、重放及 graph/eager 正确性分歧 | 图模式失败或与 eager 结果不一致时 |
| **vllm-ascend-correctness-validation** | 对比 baseline/candidate、eager/graph、离线/在线和 AISBench 正确性 | 需要精度验证或输出对拍时 |
| **vllm-ascend-change-validation** | 对照代码 diff 汇总已执行的验证证据和报告 | 需要实验验证或正式验证报告时；普通 PR 阅读和 review 直接使用原生工具 |
| **vllm-ascend-performance-regression** | 运行交替 A/B 实验并分析波动和回退阈值 | 判断吞吐或延迟是否回退时 |
| **vllm-ascend-distributed-debug** | 从拓扑、端点、collective 和逐 rank 事件诊断分布式故障 | 故障依赖多卡、多机或 rank 时 |
| **ascend-tensor-dump** | 有界采集中间张量并定位首个数值分叉的 stage，覆盖 eager 与图模式 | 输出错误或两个配置结果不一致，需要定位到层、stage 或单算子时 |
| **ascend-operator-debug** | 将模型故障缩减为单算子并运行 dtype/shape/layout/mode 矩阵 | 需要最小化算子复现时 |
| **ascend-triton-operator-development** | 从 PyTorch 或 GPU Triton 语义生成首个正确的 Ascend Triton 实现 | 新建或迁移 Triton 算子时 |
| **ascend-triton-kernel-validation** | 检测 PyTorch fallback 并执行显式正确性矩阵 | 验证 Triton 候选实现时 |
| **ascend-triton-kernel-optimization** | 在正确性门禁后执行 profiler 驱动的优化实验 | 优化已正确的 Triton kernel 时 |
| **ascend-triton-workflow** | 编排开发、验证、优化和 Run Manifest 证据 | 交付完整 Triton 算子生命周期时 |
| **vllm-ascend-pd-serving** | 启动和观察一个 prefill/decode 拓扑，并做 HTTP smoke | 部署 PD 分离服务时 |

技能按任务选用。详细输入和方法位于对应 `SKILL.md` 的参考资料；普通本地文件与 Git 操作使用原生工具。[AGENTS.md](AGENTS.md) 是客户端入口，[文档索引](docs/README.md) 区分当前契约和历史验收证据。

## 仓库与本地状态

规范仓库是 `vllm-ascend-workspace/vllm-ascend-workspace`。`vllm/`、`vllm-ascend/` 是指向社区上游的 Git 子模块；个人 Fork 作为开发 remote，初始化保留已有远程配置。

`.agents/skills/` 保存业务技能，`.agents/lib/` 保存共享消费代码，`.agents/scripts/` 保存客户端接线和维护工具。客户端投影统一指向规范技能。运行状态和私人配置放在未跟踪的 `.vaws-local/`，凭据不入库。公开知识只使用包生成的脱敏副本。

工作区许可证独立于子模块；两个子模块分别遵循其上游许可证。
