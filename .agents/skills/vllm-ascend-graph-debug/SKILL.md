---
name: vllm-ascend-graph-debug
description: Diagnose vLLM Ascend cudagraph and ACL Graph compile, capture, replay, hang, and graph-versus-eager correctness problems. Use when eager passes but graph mode fails, hangs, or diverges, or when graph/eager intermediate tensors must be aligned. Do not use to plan a correctness matrix, after the failure is reduced to one operator, when eager itself fails, or for performance profiling or HBM attribution.
---

# NPU Graph Debug

用于排查 vLLM Ascend 图模式下的编译、捕获、重放和精度问题。核心方法是：记录已知信息，先定性问题阶段，再控制变量缩小范围；只有范围足够小时，才插入预分配 buffer，通过图内 `copy_` 和图外落盘对比 graph/eager 的中间状态。

## 结构化入口

从仓库根目录使用 `scripts/graph_debug_case.py`：

1. `init` 创建 `.vaws-local/graph-debug/<case-id>/case.json` 和 Run Manifest v1。
2. 每轮单变量实验后立即用 `record` 追加假设、预期、观测、结论和下一步。
3. 需要中间状态对拍时，用 `compare` 对齐 eager/graph JSONL snapshot 并找到首个分叉。
4. 修复后用 `finalize` 同时记录最小复现、原始复现和 instrumentation 清理状态。

按需读取：

- [Behavior contract](references/behavior.md)：case 生命周期、snapshot schema、比较语义和 Run Manifest 集成。
- [Command recipes](references/command-recipes.md)：可复制的 init、record、compare、finalize 命令。
- [Acceptance](references/acceptance.md)：结案前必须逐项满足的验收条件。

## 工作原则

1. 每轮实验只改变一个变量，实验前写明假设，实验后记录结论。
2. 优先证明“已经排除什么”和“问题首次出现在哪里”，避免重复回到已验证路径。
3. Eager 模式必须先正常；如果 Eager 本身失败，先修 Eager，不进入图模式排查。
4. 精度排查优先固定随机性和输入；性能、吞吐、并发压力只在问题需要时引入。
5. 图内只做设备侧、可 capture 的操作；同步、CPU 读回、文件写入统一放到图外。

## 记录要求

不要使用散落的临时笔记作为唯一事实来源。以 case 目录中的 `case.json` 为结构化记录；issue 或 PR comment 可以引用它的摘要。每次实验完成后立即运行 `record`，并保持“已知事实 / 已排除 / 当前嫌疑 / 下一步”与 case 内容一致。

## 总流程

1. 建立基线：同输入、同 seed、同采样参数分别跑 Eager 和 Graph。
2. 固定确定性：启动环境设置 `HCCL_DETERMINISTIC=true`；model runner 初始化时调用 `torch.use_deterministic_algorithms(True)`。
3. 阶段定性：先判断问题发生在 compile、capture、replay 还是 accuracy。
4. 控制变量缩小范围：模型规模、并行策略、请求 shape、capture size、特性开关、部署方式逐项收敛。
5. 静态审计：检查 graph replay 依赖的 metadata、padding、dummy run 与真实请求路径、自定义算子 capture/replay 约束。
6. 动态打点：在候选模块插入预分配 debug buffer，图内 `copy_`，图外 flush。
7. 对齐比较：按 step/layer/rank/tag 先比统计量，再比局部样本，定位首个分叉点。
8. 修复验证：关闭 debug 代码和同步逻辑，重跑最小复现和原始复现，更新记录。

## 阶段定性

| 阶段 | 典型现象 | 先问什么 | 下一步 |
|------|----------|----------|--------|
| Compile | 启动、加载或 `torch.compile` 阶段报错/卡住 | 是否是 Python/Dynamo/FX/算子前端问题 | 看堆栈；缩小到具体模块或算子；必要时图外打印 |
| Capture | dummy run、capture model 阶段报错/卡住 | 编译后静态图是否能在固定 shape 下执行 | 隔离 compile backend 与 graph capture；查同步、CPU 读回、自定义算子 |
| Replay | capture 成功，真实请求时报错/卡住 | 真实请求和捕获 shape/metadata/通信序列是否一致 | 查 padding、固定地址 metadata、event 配对、rank 间状态 |
| Accuracy | graph 输出与 Eager 非预期差异 | 是良性数值差异还是功能性错误 | 无 padding 对照；静态审计；中间 tensor 对比 |

如果阶段不清楚，先用最小请求复现，并记录最后一个成功阶段。不要一开始就改多处代码。

## 范围收敛

按从外到内、从便宜到昂贵的顺序缩小范围：

1. 环境：确认驱动、CANN、torch、torch_npu、vLLM、vLLM Ascend 版本一致且被记录。
2. 输入：固定 prompt 或 token ids、采样参数、seed、max tokens、batch、并发。
3. 模型：从原模型缩到同结构小模型；必要时屏蔽或替换可疑模块。
4. 并行：多机到单机，多卡到单卡，逐步恢复 TP/DP/EP/PP 等策略。
5. Shape：构造无 padding、少 padding、大 padding 的请求，观察差异是否跟 capture size 相关。
6. 特性：逐个关闭可选优化、特殊 decode 路径、自定义 kernel 或高级调度。
7. 部署：从在线服务缩到离线脚本，排除服务层、调度层和并发干扰。

每一步只改变一个变量。若实验结果不改变结论，记录为“已排除”，后续不要重复尝试。

## 精度排查

先判断差异性质：

| 类型 | 表现 | 判断方式 | 处理 |
|------|------|----------|------|
| 良性数值差异 | 数值有小幅偏移，输出整体合理 | 无 padding 或固定 shape 后差异明显缩小；任务级指标可接受 | 记录结论，通常不修 |
| 功能性错误 | 乱码、重复、答案坍缩、首个分叉后快速扩散 | Eager 正常，Graph 稳定异常；中间状态出现明确首个分叉 | 继续缩小到模块、rank、token、算子 |

常见嫌疑按优先级审计：

1. Graph replay 读取的 tensor 地址是否固定：input、position、slot/block、attention metadata、长度信息等都应预分配并复用。
2. Dummy run 与真实请求是否走同一逻辑：shape、分支、metadata、mask、cache 索引、空 tensor 情况是否一致。
3. Padding 是否被正确处理：算子是否读取 padding 区，统计是否包含无效 token，索引是否越界或错位。
4. Rank 间状态是否一致：每个 rank 的请求数、token 数、通信序列、metadata 更新顺序是否对齐。
5. 自定义算子是否支持 capture/replay：是否在图内分配内存、同步、读回 CPU 或依赖变化的 host 状态。

## 快照打点

当静态审计无法定位时，使用预分配 buffer 快照。原则：

1. Buffer 在初始化阶段创建并注册；不要在 forward 中惰性创建。
2. Forward 中只做 `copy_(..., non_blocking=True)` 和设备侧统计。
3. 图外统一 `synchronize`、CPU 读回和写文件。
4. 打点尽量少：先输入/输出，再在首个分叉窗口内增加更细 tag。
5. 每次新增 tag 都记录目的，定位后删除。

### 最小模板

按代码结构改名即可，不要照搬固定 rank、shape 或 tag；它们应由当前最小复现决定。

```python
DEBUG_ENABLE = True
DEBUG_MAX_LAYERS = 2
DEBUG_RANKS = None  # None 表示所有 rank；也可设置为 {0, 1}
_DEBUG_IMPLS = []

def flush_debug_buffers() -> None:
    if not _DEBUG_IMPLS:
        return
    if torch.npu.is_current_stream_capturing():
        return
    torch.npu.synchronize()
    lines = []
    for impl in _DEBUG_IMPLS:
        impl.flush_debug(lines)
    if lines:
        with open(debug_log_path(), "a") as f:
            f.write("\n".join(lines) + "\n")
```

```python
class DebuggableImpl:
    def __init__(self, ...):
        self.debug_rank = current_rank()
        self.debug_enabled = (
            DEBUG_ENABLE
            and (DEBUG_RANKS is None or self.debug_rank in DEBUG_RANKS)
        )
        if self.debug_enabled:
            self.debug_step = 0
            self.debug_layer = -1
            self.debug_shapes = {
                "IN": (max_rows, max_cols),
                "OUT": (max_rows, max_cols),
            }
            self.debug_buffers = {
                tag: torch.zeros(shape, dtype=torch.bfloat16, device=current_device())
                for tag, shape in self.debug_shapes.items()
            }
            self.debug_stats = {
                tag: torch.zeros(4, dtype=torch.float32, device=current_device())
                for tag in self.debug_shapes
            }
            _DEBUG_IMPLS.append(self)

    def snapshot(self, tag: str, tensor: torch.Tensor) -> None:
        if not self.debug_enabled or tensor.numel() == 0:
            return
        buf = self.debug_buffers.get(tag)
        if buf is None:
            return
        sample = select_representative_slice(tensor).to(buf.dtype).contiguous()
        rows = min(sample.shape[0], buf.shape[0])
        cols = min(sample.shape[1], buf.shape[1])
        buf[:rows, :cols].copy_(sample[:rows, :cols], non_blocking=True)

        values = tensor.float()
        stat = self.debug_stats[tag]
        stat[0].copy_(values.min(), non_blocking=True)
        stat[1].copy_(values.max(), non_blocking=True)
        stat[2].copy_(values.mean(), non_blocking=True)
        stat[3].copy_(values.var(), non_blocking=True)

    def flush_debug(self, lines: list[str]) -> None:
        if not self.debug_enabled or not (0 <= self.debug_layer < DEBUG_MAX_LAYERS):
            return
        for tag, buf in self.debug_buffers.items():
            stat = self.debug_stats[tag].cpu().tolist()
            sample = buf.cpu().tolist()
            lines.append(format_debug_record(
                step=self.debug_step,
                layer=self.debug_layer,
                rank=self.debug_rank,
                tag=tag,
                stat=stat,
                sample=sample,
            ))
        self.debug_step += 1
```

在 model runner 或等价调度位置：

```python
def __init__(self, ...):
    torch.use_deterministic_algorithms(True)
    ...

def execute_model(self, ...):
    output = run_model(...)
    flush_debug_buffers()
    return output
```

启动脚本或服务环境：

```bash
export HCCL_DETERMINISTIC=true
```

## 对比方法

1. 两轮运行必须使用同一最小复现：一次 graph，一次 Eager。
2. 日志 key 至少包含 `step/layer/rank/tag`，确保能机械对齐。
3. 先比统计量：`min/max/mean/var` 一致时，通常不需要扩大样本。
4. 统计量首次不一致时，记录首个分叉窗口，再在该窗口内增加 tag 或扩大样本。
5. 若所有 rank 同时分叉，优先查共享输入、shape、padding、公共算子。
6. 若单个或少数 rank 分叉，优先查 rank-local metadata、通信顺序、分片索引。
7. 若只有特定 shape 分叉，优先查 capture size、padding、空 tensor、边界索引。
8. 每轮对比结束后更新“已知事实 / 已排除 / 当前嫌疑 / 下一步”。

## 工具选择

| 工具 | 适用阶段 | 注意 |
|------|----------|------|
| Python `print` | 构图、capture 前后 Python 逻辑 | Replay 不会重新执行 Python，不能证明 replay 内状态 |
| 图外设备打印 | Compile 卡住、Eager 调试 | 可能引入同步，不要放进 capture/replay 路径 |
| `copy_` 到预分配 buffer | Capture、Replay、精度对比 | 首选；只做设备侧 copy，图外落盘 |
| 图模式专用打印工具 | Capture、Replay 阻塞点 | 放在其支持的位置，避免破坏 compile |
| plog/运行日志 | Capture、Replay 报错或卡住 | 结合最后成功阶段和 rank 对齐信息看 |
| GDB/线程栈 | 卡死 | 用于确认底层等待、通信或事件阻塞 |

## 收尾

定位完成后：

1. 删除或关闭 debug buffer、同步、落盘、确定性调试开关。
2. 用最小复现验证修复。
3. 用原始复现验证问题不再出现。
4. 运行 `finalize`，写清根因、修复点、已验证场景和仍未覆盖的风险。
5. 对照 [Acceptance](references/acceptance.md) 验证 `case.json`、comparison artifacts 和 Run Manifest。
