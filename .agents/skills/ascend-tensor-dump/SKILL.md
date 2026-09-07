---
name: ascend-tensor-dump
description: Capture and compare bounded intermediate tensor dumps on Ascend NPU to find the first stage where numbers diverge. Use when output is wrong, non-finite, or differs between two configurations and the divergence must be localized to a stage, layer, rank, or single operator, in eager or graph mode. Do not use for performance profiling, HBM attribution, debug case bookkeeping, or before a deterministic reproduction with fixed weights and token ids exists.
---

# Ascend Tensor Dump

采集和比较 Ascend NPU 上的中间张量，回答一个问题：**数值第一次在哪里出错**。

这个 skill 只做两件事：**有界采集**和**机械比较**。它不管 case 记账，不建 Run Manifest，也不做完整性校验。工具本身必须保持"笨"——为了 dump 框架自身的安全性反复校验，是这类调查里最常见的时间浪费。

## 什么时候不要用

1. 还没有确定性复现。权重路径、token id 数组、采样参数、并发、topology 没固定之前，任何 dump 都不可比。**先固定输入身份，再谈数值。**
2. 更便宜的手段还没用完。按代价排序：单算子开关消融 → 首个坏 token 的输出对拍 → 摘要 dump → 全量张量 → 单算子回放。跳过前两级直接插桩，是最常见的绕路。
3. 问题已经缩到一个算子。那时直接用 `ascend-operator-debug` 的输入矩阵。
4. 现有 artifact 已经能证伪当前假设。先问"我手上的东西能不能分辨这个假设"，不能再加钩子。

## 结构化入口

从仓库根目录：

| 文件 | 位置 | 用途 |
|------|------|------|
| `assets/dump_probe.py` | 复制进被测包（如 `vllm_ascend/dump_probe.py`） | 探针模块，环境变量武装 |
| `scripts/dump_compare.py` | 在 dump 所在机器上运行 | `scan` / `diff` / `tensors` 三级比较 |
| `assets/replay_op.py` | 复制到远端 | 单算子输入回放 + 参考实现对拍 |

按需读取：

- [Behavior contract](references/behavior.md)：环境变量语义、manifest schema、比较语义、图模式约束。
- [Command recipes](references/command-recipes.md)：可复制的插桩、启动、比较、回放命令。
- [Acceptance](references/acceptance.md)：用 dump 结论下判断前必须满足的条件。

## 三条铁律

1. **摘要优先，张量按需。** `DUMP_PROBE_TENSOR` 默认为空，即不落任何整张量。先用 6 个统计量（`nan_count` / `inf_count` / `max_abs` / `min` / `max` / `mean`）定位到 stage，再收紧正则取那一个 stage 的张量。
2. **一次 D2H。** forward 内不出现 `.cpu()`、`.item()`、`.tolist()`、`print` 设备值。统计量在设备上算完堆叠，只在 `finish()` 里读回一次。**一个会同步的 dump 可能替你完成了 bug 依赖的异步 copy，症状就消失了。**
3. **图内只做 `copy_`。** 图模式用 `graph_slot()` 预分配 + `capture_graph()` 图内设备间 copy，`finish()` 在 replay 之后读回。capture 窗口内做 D2H 会改变被测对象本身。

## 采集流程

1. 复制 `assets/dump_probe.py` 进被测包。**用 `remote.write` 写到容器内可见的路径**——写到宿主机 `/workspace` 会让同步看起来成功，但服务永远跑不到探针。
2. 在调度边界调 `arm(label, **metadata)`，`label` 用 request id。在候选 stage 调 `capture(stage, tensor)`。在 logits 已经存在之后调 `finish()`。
3. 只开摘要跑一轮：`DUMP_PROBE=1`，`DUMP_PROBE_DIR`，`DUMP_PROBE_RANKS=0`。
4. **做一次探针开关对照。** dump 关掉时症状还在吗？不在，说明探针在测自己，回到铁律 2。
5. `dump_compare.py scan` 找首个非有限 stage 和共享存储的 stage。
6. 收紧 `DUMP_PROBE_TENSOR` 到那一个 stage，设 `DUMP_PROBE_ROWS` 限行，再跑一轮。
7. 定位到写者-地址-读者，或缩到单算子，就**停止加钩子**，转单变量修复实验。

## 选择器：按 label 去重，不要数调用次数

chunked prefill 会为同一个请求调多次 forward。按"第几次 forward"选择，会把一个请求的三个 prefill chunk 当成三个请求，整轮 dump 作废。

探针因此分开两个维度：

- `DUMP_PROBE_MATCH` 选**第几个不同的 label**（默认 1）。
- `DUMP_PROBE_OCCURRENCE` 选该 label 内**第几次调用**（默认 1）。

`label` 传 request id，`MATCH=1` 就锁定第一个真实请求，`OCCURRENCE=3` 才是它的第三个 chunk。

## 比较三级

| 命令 | 输入 | 需要 torch | 回答什么 |
|------|------|-----------|----------|
| `scan` | 一个或多个 `.json` manifest | 否 | 首个 NaN/Inf 出现在哪个 stage；哪些 stage 共享同一块设备存储 |
| `diff` | 两个 `.json` manifest | 否 | 两个配置的首个分叉 stage，只看统计量 |
| `tensors` | 两个 `.pt` | 是（仅加载） | 逐张量 `max_abs_diff` / `cosine` / `rel_l2` / `allclose`，给 `first_mismatch` |

绝大多数结论出在 `scan` 和 `diff`，两者都不需要张量，也不需要把大文件拉回本地。**摘要留远端看，全量 `.pt` 按需拉。**

## 物理 layout 必须一起看

manifest 每条记录同时带**逻辑信息**（shape / dtype）和**物理信息**（`stride` / `storage_offset` / `storage_ptr` / `npu_format`）。

`contiguous().cpu()` 会抹掉后者，而缓存踩踏、block table 错位、KV 复用冲突这类 bug 的证据恰好只在后者里：**不同 stage 共享同一 `storage_ptr` 却用不同 stride**。比 `max_abs` 之前先看 `scan` 输出的 `storage_aliases`，`stride_conflict` 为真的那组就是嫌疑。

## 图模式与 eager 的分工

| 目的 | 模式 | 做法 |
|------|------|------|
| 查数学、缓存逻辑、状态读写 | eager | `--enforce-eager`，正常 `capture()` |
| 查 capture/replay 是否改了行为 | graph | `graph_slot()` + `capture_graph()`，`finish()` 在 replay 之后 |

两者不要混。**在一次带 D2H 的 capture 里同时想回答两个问题，得到的是探针自己的行为。**

如果只能靠让 dump 强制走 eager 才能拿到数据（例如 `skip_compiled`），那这一轮测的就不是图路径，必须在结论里写明这一点。

**图内的 `capture()` 在 replay 时根本不执行。** replay 只重放设备 kernel，不重入 Python，所以 forward 体内的打点只在 capture 那一次生效。实测同一份插桩在 Qwen3-0.6B 同一个 decode step 上：eager 出 114 条记录，aclgraph 只出 2 条——活下来的两条在 model runner 里，本来就在图外；而 28 个 `graph_slot` 缓冲全部带回了数据。所以图模式下**记录数骤降是缺数据，不是"两边一致"**。

另一个推论：`capture_graph()` 不能用 `armed()` 之类的运行时条件去 gate。copy 节点在 capture 时就固化了，运行时的判断根本不参与。

## 单算子回放

1. 在算子调用点用 `capture_inputs(stage, **named)` 存下完整输入集。非张量参数按原值保留。**插在分支判断之前**——vllm-ascend 的算子包装常按 `enable_custom_op()` 在 `torch.ops._C_ascend` 融合 kernel 和 `torch_npu` 回退之间二选一，插到没走的那一支上会一无所获，而这种"空"很容易被误诊成选择器问题。不确定就先在容器里求一次 `enable_custom_op()`。
2. `replay_op.py --list` 看抓到了什么，包括每个 stage 被打了多少次。
3. `replay_op.py --stage X#N --candidate <算子> --reference <参考实现>` 生成 `candidate.pt` 和 `reference.pt`。同名 stage 通常每层一次，必须用 `#N` 指定是哪一次；只出现一次时可以省略。
4. `dump_compare.py tensors` 出指标。

参考实现优先选 CPU FP32 或规范公式，不要选"另一条历史兼容路径"——那条路径可能本身就不是 golden。

服务级 dump 给的是"哪一层先坏、哪块地址被踩"；单算子回放给的是"算子合同对不对"。**一份 `.pt` 不会自动变成整模型的 repro，这是两段独立工作。**

## 不做的事

1. **不做 sha256 校验。** 需要钉身份的是**输入**：权重目录、token id 数组、算子 `.so`。dump 文件自身不校验，也不用 hash 配对——配对靠 stage 名和 occurrence 下标。
2. **不建 Run Manifest。** 采集属于某个调查时，把 dump 路径记进 `vllm-ascend-graph-debug` 的 case 或 `ascend-operator-debug` 的矩阵；这个 skill 不新建 manifest。
3. **不做 schema 协商和自检。** manifest 里 `probe` 字段只是标记，不校验版本。

## 收尾

定位完成后：

1. 删掉 `capture*` 调用和 `graph_slot` 分配。`capture_graph` 尤其要删——图内 copy 一旦被 capture，每次 replay 都在付这笔固定开销。
2. 关掉 `DUMP_PROBE`，重跑最小复现和原始复现。
3. 把"写者 → 地址 → 读者"或单算子合同结论写进对应 case，dump 路径作为证据引用。
4. 新根因经过验证后，用 `.agents/scripts/knowledge_capture.py` 记候选。
