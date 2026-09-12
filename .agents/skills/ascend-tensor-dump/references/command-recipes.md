# Command recipes

在实际业务 worktree 中插桩，再通过 coordinator 的 sources 绑定运行。
显式远端文件操作使用 remote-dev companion。探针与算子回放在管理的 Ascend
Python 环境中执行；比较命令在持有 dump 文件的环境中执行，本地示例从仓库根目录运行。

## 1. 装探针

把 `assets/dump_probe.py` 复制到实际业务 worktree 的被测包中，并绑定这棵源码。

确认它在服务实际 import 的那棵树里：

```bash
python -c "import vllm_ascend, pathlib; print(pathlib.Path(vllm_ascend.__file__).parent)"
ls -l $(python -c "import vllm_ascend, pathlib; print(pathlib.Path(vllm_ascend.__file__).parent)")/dump_probe.py
```

路径不一致时修正源码绑定或导入路径。宿主机与容器中的同名路径不一定是同一棵源码。

## 2. 插桩

调度边界，每次 forward 武装一次：

```python
from vllm_ascend import dump_probe

# model_runner.execute_model 开头附近
for request_id in scheduler_output.batch_request_ids:
    if dump_probe.arm(request_id,
                      num_computed_tokens=num_computed,
                      num_scheduled_tokens=num_scheduled):
        break
```

按 request id 武装，`DUMP_PROBE_MATCH` 才是"第几个请求"而不是"第几个 prefill chunk"。

候选 stage：

```python
dump_probe.capture(f"{prefix}.hidden_input", hidden_states)
dump_probe.capture(f"{prefix}.qkv", qkv)
dump_probe.capture(f"{prefix}.attn_out", attn_output)
```

落盘，放在 logits 已经存在之后：

```python
# execute_model 返回前
logits = self.model.compute_logits(hidden_states)
dump_probe.capture("model.logits", logits)
dump_probe.finish()
```

## 3. 只跑摘要的第一轮

```bash
export DUMP_PROBE=1
export DUMP_PROBE_DIR=/vllm-workspace/dumps/baseline
export DUMP_PROBE_RANKS=0
export DUMP_PROBE_MATCH=1
# DUMP_PROBE_TENSOR 留空：这一轮不落任何整张量
```

发一条确定性请求：

```bash
curl -s http://127.0.0.1:8000/v1/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"'"$SERVED_MODEL_NAME"'","prompt":"who are you","max_tokens":1,
       "temperature":0,"seed":0}'
```

首 token 已复现的问题可以用 `max_tokens=1`。若问题发生在后续 decode，保留到故障位置所需的输出长度。

## 4. 探针开关对照

怀疑插桩的同步或复制会改变症状时，比较探针关闭和开启时的相同请求。
开关必须作用于服务进程：可以使用 `DUMP_PROBE_ENABLE_FILE` 哨兵；改变
`DUMP_PROBE` 则需要在服务启动环境中设置。给 curl 设置环境变量不会修改已运行服务。
比较响应中的 token/text 和关键数值，不要直接 diff 含随机请求 ID 的完整响应。
输出差异也可能来自原问题的非确定性，应结合重复观测区分。

## 5. 读摘要

```bash
uv run --no-project python .agents/skills/ascend-tensor-dump/scripts/dump_compare.py scan \
  --manifest /vllm-workspace/dumps/baseline/*.json \
  --max-abs-limit 1e4
```

关注三个字段：

- `first_nonfinite`：第一个出现 NaN/Inf 的 stage，通常就是答案所在层。
- `storage_aliases` 里 `stride_conflict` 为真的组：同一块存储被不同 stride 访问，缓存或 block table 踩踏的签名。
- `records_without_summary`：非空说明 `DUMP_PROBE_SUMMARY` 正则写窄了，有 stage 没被统计到。

## 6. 两个配置对拍

跑两轮，只改一个变量（graph/eager、特性开关、prefix on/off、baseline/candidate 代码）。

```bash
uv run --no-project python .agents/skills/ascend-tensor-dump/scripts/dump_compare.py diff \
  --left  /vllm-workspace/dumps/eager/cmpl-abc-rank0.json \
  --right /vllm-workspace/dumps/graph/cmpl-abc-rank0.json
```

先看 verdict。`COVERAGE_MISMATCH` 表示两侧记录集合不同（`only_in_left` /
`only_in_right` 给出 stage），可能来自路径或捕获方式不同。公共 stage 的数值仍可用于
局部诊断；缺失 stage 不能证明一致，需按当前问题决定是否补采。

再看 `first_divergent.reasons`。

需要在脚本里 gating 时：

```bash
python .../dump_compare.py diff --left a.json --right b.json --fail-on-divergence
```

## 7. 取那一个 stage 的张量

摘要指向 `layers.23.self_attn.kv` 之后，第二轮只落这一个 stage：

```bash
export DUMP_PROBE=1
export DUMP_PROBE_DIR=/vllm-workspace/dumps/layer23
export DUMP_PROBE_TENSOR='layers\.23\.self_attn'
export DUMP_PROBE_ROWS=32
```

按张量大小和回放语义选择 `DUMP_PROBE_ROWS`。它只作用于 2 维及以上的张量；
`0` 保留完整张量。裁剪会改变算子输入，应确认所需 token 范围和对应参数仍然匹配。

比较：

```bash
python .../dump_compare.py tensors \
  --left  /vllm-workspace/dumps/eager/cmpl-abc-rank0.pt \
  --right /vllm-workspace/dumps/graph/cmpl-abc-rank0.pt \
  --atol 1e-3 --rtol 1e-3
```

默认容差是 `1e-2`，属于 bf16 宽松档。要声称"逐位一致"必须显式收紧。

## 8. 图模式采集

模块构造期分配 slot：

```python
class AscendAttentionImpl:
    def __init__(self, ...):
        from vllm_ascend import dump_probe
        self._dump_slot = dump_probe.graph_slot(
            f"{self.prefix}.attn_out", (32, self.hidden_size), torch.bfloat16
        )
```

forward 内，图内 copy：

```python
dump_probe.capture_graph(f"{self.prefix}.attn_out", attn_output)
```

图外读回，`execute_model` 返回前：

```python
dump_probe.arm(request_id)   # 图 slot 已经在写，arm 只决定这次是否落盘
dump_probe.finish()
```

启动服务保持图模式，不要加 `--enforce-eager`。图 slot 走的就是真实 replay 路径。

对照的 eager 一侧照常用 `capture()`，服务加 `--enforce-eager`。

两侧配对时注意捕获路径：

1. **图 slot 不进 `records`。** 它们落在 manifest 的 `graph_slots` 字典和 `.pt` 的 `graph:` key 空间里，所以 `diff`（只配对 `records`）看不到它们。用 `scan` 看声明了哪些 slot，用 `tensors` 比数值。
2. 图内的 Python `capture()` 在 replay 时不执行，可能导致 eager 和 graph 的记录集合不同。图外共同 stage 或语义对齐的 graph slot 可以比较；单侧缺失的记录保留为覆盖差异。
3. **`graph:` key 和 `capture()` 的 stage key 不会自动配对**，前缀不同，即使名字取一样也不会。跨模式的整张量对拍需要自己后处理。`tensors` 更适合用在同模式的两轮之间：graph 比 graph（换 build、换配置），eager 比 eager。

## 9. 单算子回放

算子调用点存输入集。**插在分支判断之前**，不要插进某一支里：

```python
if residual is not None:
    from vllm_ascend import dump_probe

    dump_probe.capture_inputs(
        "add_rms_norm",
        x=x,
        residual=residual,
        gamma=self.weight,
        epsilon=self.variance_epsilon,
    )
    if enable_custom_op():
        ...   # torch.ops._C_ascend 融合 kernel
    else:
        ...   # torch_npu 回退
```

vllm-ascend 的算子包装常按 `enable_custom_op()` 二选一，走哪一支取决于 build 而不是模型或请求。插到没走的那一支上会一无所获，而且 `py_compile` 查不出来——它只验语法。不确定就先求一次：

```bash
python -c 'from vllm_ascend.utils import enable_custom_op; print(enable_custom_op())'
```

顺带一句：在死分支里写 `from vllm_ascend import dump_probe` 这种内联 import，会把"模块级 import 漏了"的问题一起藏到运行时。

拉到能跑算子的机器上，先看抓到了什么。同名 stage 每层一次是常态，`--list` 会报出次数和寻址范围：

```bash
python replay_op.py --dump /vllm-workspace/dumps/gmm/cmpl-abc-rank0.pt --list
# {"stages": {"gmm1": {"occurrences": 56, "inputs": [...],
#                      "addressable_as": "gmm1#0 .. gmm1#55"}}}
```

对拍参考实现：

```bash
python replay_op.py \
  --dump /vllm-workspace/dumps/gmm/cmpl-abc-rank0.pt \
  --stage gmm1#10 \
  --candidate torch_npu.npu_grouped_matmul \
  --reference my_refs.grouped_matmul_reference \
  --arg-order hidden,weight,group_list \
  --out-dir /tmp/replay-gmm1
```

```bash
python dump_compare.py tensors \
  --left /tmp/replay-gmm1/reference.pt \
  --right /tmp/replay-gmm1/candidate.pt \
  --atol 0 --rtol 0
```

参考实现选 CPU FP32 或规范公式。不要拿"另一条历史兼容路径"当 golden——那条路径本身可能就是错的那一方。

算子要求 NZ 权重时在 `--reference` 指向的函数里显式转换，或直接编辑 `replay_op.py`。它是 asset，就是用来改的。

## 10. 收尾

检查实际业务 worktree 的 diff，移除本次临时插桩。原有长期调试代码不属于本次清理。

`capture_graph` 和 `graph_slot` 必须删除，不能只关环境变量：图内 copy 一旦 capture 进去，每次 replay 都在付这笔开销。

若继续使用已捕获图的服务，重启以移除旧图中的 copy 节点。验证受本次改动影响的复现，
把有用证据和剩余限制写入正常任务总结；无需另建 case 记录。
