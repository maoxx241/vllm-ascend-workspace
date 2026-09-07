# Command recipes

所有远端操作优先用 `.remote-dev` companion 工具，不要拼裸 SSH。以下命令都在会话容器内执行。

## 1. 装探针

把探针写进被测包，**路径必须是容器内可见的路径**。

```
remote.write
  path: /vllm-workspace/vllm-ascend/vllm_ascend/dump_probe.py
  content: <.agents/skills/ascend-tensor-dump/assets/dump_probe.py 的内容>
```

确认它在服务实际 import 的那棵树里：

```bash
python -c "import vllm_ascend, pathlib; print(pathlib.Path(vllm_ascend.__file__).parent)"
ls -l $(python -c "import vllm_ascend, pathlib; print(pathlib.Path(vllm_ascend.__file__).parent)")/dump_probe.py
```

两条路径不一致时停下来。写到宿主机 `/workspace` 而服务跑在容器另一棵树上，会让同步看起来成功但探针永远不执行。

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

`max_tokens=1` 就够定位首个分叉。先把输出长度压到最小，再考虑加长。

## 4. 探针开关对照

这一步不能跳。

```bash
# 关掉探针重跑同一条请求
DUMP_PROBE=0 curl -s ... | tee /tmp/probe-off.json
# 开着探针重跑
DUMP_PROBE=1 curl -s ... | tee /tmp/probe-on.json
diff /tmp/probe-off.json /tmp/probe-on.json
```

两次输出不同就说明探针改变了被测行为——回去检查 forward 内是否有同步或 D2H。

## 5. 读摘要

```bash
python .agents/skills/ascend-tensor-dump/scripts/dump_compare.py scan \
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
python .agents/skills/ascend-tensor-dump/scripts/dump_compare.py diff \
  --left  /vllm-workspace/dumps/eager/cmpl-abc-rank0.json \
  --right /vllm-workspace/dumps/graph/cmpl-abc-rank0.json
```

先看 `only_in_left` 和 `only_in_right`。两者非空说明两轮走的不是同一条代码路径，此时任何数值结论都不成立，先把路径对齐。

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

`DUMP_PROBE_ROWS` 必须设。不设会把整个 prefill 的 token 维度全存下来。

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

对照的 eager 一侧照常用 `capture()`，服务加 `--enforce-eager`。`finish()` 两侧都写同一套 key，`graph:` 前缀的条目只有图那一侧有，用 `diff` 的 `only_in_right` 可以确认。

## 9. 单算子回放

算子调用点存输入集：

```python
dump_probe.capture_inputs(
    "gmm1",
    hidden=quantized_hidden_states,
    weight=w1,
    group_list=group_list,
    scale=pertoken_scale,
    group_list_type=group_list_type,
)
```

拉到能跑算子的机器上，先看抓到了什么：

```bash
python replay_op.py --dump /vllm-workspace/dumps/gmm/cmpl-abc-rank0.pt --list
```

对拍参考实现：

```bash
python replay_op.py \
  --dump /vllm-workspace/dumps/gmm/cmpl-abc-rank0.pt \
  --stage gmm1 \
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

```bash
# 确认插桩已经清干净
remote.grep pattern: "dump_probe\.(capture|arm|finish|graph_slot)" path: /vllm-workspace/vllm-ascend
```

`capture_graph` 和 `graph_slot` 必须删除，不能只关环境变量：图内 copy 一旦 capture 进去，每次 replay 都在付这笔开销。

删完重启服务，重跑最小复现和原始复现，再把结论写回对应的 case。
