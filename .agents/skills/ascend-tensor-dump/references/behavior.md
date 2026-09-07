# Behavior contract

## 环境变量

探针在每次 `arm()` 时读环境变量，不缓存。

| 变量 | 默认 | 语义 |
|------|------|------|
| `DUMP_PROBE` | 未设置 | `1` 才启用。其他值一律禁用，所有入口是 no-op。 |
| `DUMP_PROBE_DIR` | `.` | 输出目录，启用时必须显式设置。 |
| `DUMP_PROBE_RANKS` | `0` | 逗号分隔的全局 rank。默认只武装 rank 0。 |
| `DUMP_PROBE_MATCH` | `1` | 1-based，第几个**不同的 label**。 |
| `DUMP_PROBE_OCCURRENCE` | `1` | 1-based，该 label 内第几次 `arm()` 调用。 |
| `DUMP_PROBE_SUMMARY` | 空 | 正则。空表示所有 stage 都算统计量。 |
| `DUMP_PROBE_TENSOR` | 空 | 正则。**空表示不落任何整张量**，这是代价闸门。 |
| `DUMP_PROBE_ROWS` | `0` | 整张量沿 dim 0 最多保留的行数，`0` 表示不限。**只作用于 2 维及以上的张量**——1 维张量的唯一那一维是通道/参数轴而非 token 轴，截断它不是省空间而是把权重改坏（回放时算子会报 shape 不符，或者更糟，广播出错误结果）。 |
| `DUMP_PROBE_ENABLE_FILE` | 空 | 哨兵路径。设置后文件不存在则保持禁用，可以不重启服务临时武装。 |

非法整数值退回默认值，不抛异常——调试探针不应该因为环境变量拼错而杀掉一次昂贵的启动。

## API

| 函数 | 调用位置 | 语义 |
|------|----------|------|
| `enabled()` | 任意 | 主开关加哨兵是否都允许。 |
| `armed()` | 任意 | 当前 forward 是否已武装，用于给昂贵调用方做前置判断。 |
| `arm(label, rank=None, **metadata)` | 调度边界，每次 forward 一次 | 命中选择器则武装并返回 `True`。同一 label 落盘过就不再武装。`rank` 显式传入时跳过自动解析。 |
| `capture(stage, tensor)` | 候选 stage | 未武装时立即返回。记 layout；命中 `SUMMARY` 记设备统计量；命中 `TENSOR` 才 clone。 |
| `capture_inputs(stage, **values)` | 算子调用点 | 总是 clone 全部张量入参（受 `ROWS` 限制），非张量原值保留。 |
| `graph_slot(name, shape, dtype, device=None)` | 模块 `__init__` | 预分配持久 buffer，返回它。**不要在 forward 里调。** |
| `capture_graph(name, tensor)` | forward 内、图内 | 只做 `copy_(..., non_blocking=True)`。**不受武装状态影响。** |
| `finish()` | logits 之后、图外 | 唯一同步和 D2H 的地方。返回 manifest 路径或 `None`。 |

### 参数求值陷阱

Python 在调用前就会求值实参，禁用状态也一样。写 `capture("q", q)`，不要写 `capture("q", q.index_select(0, idx))`——后者在探针关闭时仍然会执行 `index_select`，而图 capture 的 metadata 常把索引存成 2D 静态 buffer，NPU 的 `index_select` 又要求 1D 索引。

### `capture_graph` 为什么不受武装控制

图内的 copy 节点在 capture 时就被烧进图里。运行时再判断"是否武装"要么被忽略，要么改变图结构。所以图 slot 的代价是**每次 replay 一笔固定的设备间 copy**，定位完成后必须删除调用。

## 统计量

六个，全部在设备上算完后堆叠成一个张量，`finish()` 里一次读回：

| 名称 | 含义 |
|------|------|
| `nan_count` | NaN 元素个数 |
| `inf_count` | Inf 元素个数 |
| `max_abs` | 有限元素绝对值最大 |
| `min` / `max` | 有限元素最小 / 最大 |
| `mean` | 有限元素均值 |

非有限值在 `max_abs` / `min` / `max` / `mean` 中被置零后再统计，因此计数为 0 时这四个值才可信。所有 dtype 先转 `float32` 再统计，int8 和 bool 也走同一条路径。`numel() == 0` 的张量只记 layout，不算统计量。

## Manifest schema

`{safe_label}-rank{rank}.json`，`safe_label` 是 label 里非 `[A-Za-z0-9_.-]` 字符替换成 `_` 的结果。

```json
{
  "probe": "ascend-tensor-dump/1",
  "label": "cmpl-abc",
  "rank": 0,
  "match_index": 1,
  "occurrence": 1,
  "metadata": {"num_computed_tokens": 7680},
  "stats": ["nan_count", "inf_count", "max_abs", "min", "max", "mean"],
  "records": [
    {
      "stage": "layers.23.self_attn.kv",
      "order": 12,
      "shape": [8, 3584],
      "dtype": "torch.bfloat16",
      "numel": 28672,
      "device": "npu:0",
      "stride": [3584, 1],
      "contiguous": true,
      "storage_ptr": 140234567890,
      "storage_offset": 0,
      "npu_format": 2,
      "summary": {"nan_count": 448.0, "inf_count": 0.0, "max_abs": 12.5,
                  "min": -12.5, "max": 12.5, "mean": 0.01},
      "tensor_saved": true
    }
  ],
  "graph_slots": {"layers.0.attn.out": {"shape": [32, 4096], "dtype": "torch.bfloat16"}},
  "tensor_file": "cmpl-abc-rank0.pt"
}
```

字段说明：

- `records` 按 `capture()` 调用顺序，`order` 是同一次 forward 内的全局序号。
- `summary` 缺失说明该 stage 没命中 `SUMMARY` 正则，或统计失败（此时有 `summary_error`）。
- `tensor_file` 只在有整张量、输入集或图 slot 时出现。
- `capture_inputs` 产生的记录带 `inputs` 字典，每个入参各有自己的 layout 或 `value` 表示。

## 张量 payload schema

`{safe_label}-rank{rank}.pt`：

```python
{
    "probe": "ascend-tensor-dump/1",
    "label": str,
    "rank": int,
    "metadata": dict,
    "tensors": [(stage, cpu_tensor), ...],
    "inputs": [(stage, {name: cpu_tensor_or_value}), ...],
    "graph_slots": {name: cpu_tensor},
}
```

比较器把三者展平成统一 key 空间：

| 来源 | key 形式 |
|------|----------|
| `tensors` | `{stage}#{occurrence}` |
| `inputs` | `{stage}:{name}#{occurrence}` |
| `graph_slots` | `graph:{name}#{occurrence}` |

`occurrence` 是同名 key 在文件内的出现次序，从 0 开始。两侧按同一规则编号，因此不需要任何 hash 就能机械配对。

## 比较语义

### `scan`

对每个 manifest 输出：

- `first_nonfinite`：`nan_count + inf_count > 0` 的最早记录。
- `first_over_limit`：`max_abs` 超过 `--max-abs-limit` 的最早记录，未指定该参数时为 `null`。
- `records_without_summary`：没有统计量的记录 key，用来发现正则写窄了。
- `storage_aliases`：按 `storage_ptr` 分组，只报跨越多个 stage 的组。`stride_conflict` 为真表示同一块存储被以不同 stride 或 offset 访问。

`storage_ptr` 为 0 或缺失的记录不参与别名分析。

### `diff`

按 key 配对两个 manifest，只用 statistics：

- `shape_match` / `dtype_match` 逐条给出。
- `nonfinite_introduced`：右侧非有限计数大于左侧。
- `stat_divergence`：逐个统计量按 `abs(left - right) > atol + rtol * abs(right)` 判断，默认 `atol=0`、`rtol=0`，即要求逐位相同。两侧同为 NaN 视为一致。
- `first_divergent`：按左侧顺序最早满足 shape 不匹配、引入非有限、或统计量分叉的记录，带 `reasons` 列表。
- `only_in_left` / `only_in_right`：单侧独有的 key。**两者非空时先怀疑两轮跑的不是同一条路径**，而不是急着看数值。

`verdict` 取三值：

| verdict | 含义 |
|---------|------|
| `DIVERGENT` | 公共 stage 上出现形状不符、新增非有限值或统计量超差 |
| `COVERAGE_MISMATCH` | 公共 stage 全部对齐，但存在单侧独有的 stage |
| `ALIGNED` | 两侧 stage 集合相同且全部对齐 |

单侧独有的 stage **不是**一致性证据，最常见的原因是两轮没跑同一条插桩路径——例如图模式 replay 直接跳过了 Python 打点。所以覆盖不对称单独成一档，不会被折叠进 `ALIGNED`。

### `tensors`

按展平 key 配对两个 `.pt`，逐张量：

| 情况 | 结果 |
|------|------|
| 任一侧不是张量 | `comparable: false`，附原因 |
| shape 不同 | `comparable: false`，`reason: shape mismatch`，**不计算 diff** |
| 元素数超过 `--max-elements` | `comparable: false`，`reason: too large` |
| 任一侧含非有限值 | `comparable: false`，只报两侧非有限计数 |
| 整数或 bool dtype | `exact_equal` + `mismatch_count` |
| 浮点 | `exact_equal`、`mismatch_count`、`max_abs_diff`、`mean_abs_diff`、`allclose`、`cosine`、`rel_l2` |

`first_mismatch` 是最早失败的条目。`verdict` 同样是三值：`FAIL`、`COVERAGE_MISMATCH`、`PASS`，判定优先级与 `diff` 一致。

非有限值不参与任何 diff 指标：一侧出现 NaN 时答案已经是"就是这个 stage"，把它平均进去只会藏住结论。

`tensors` 的默认 `atol` 和 `rtol` 都是 `1e-2`，这是 bf16 端到端比较的宽松档。**要下"逐位一致"的结论必须显式传更紧的值**，不要用默认容差声称精度对齐。

所有指标用纯 Python 计算，torch 只用于 `torch.load`。因此 dump 的大小就是唯一需要预算的东西：超过 `--max-elements`（默认 200 万）会明确拒绝并提示降低 `DUMP_PROBE_ROWS`，而不是静默跑很久。

## 退出码

| 码 | 含义 |
|----|------|
| 0 | 执行成功。有无分叉看 `verdict` 字段。 |
| 1 | 仅当传了 `--fail-on-divergence` 且 `verdict` 为 `DIVERGENT`、`FAIL` 或 `COVERAGE_MISMATCH`。 |
| 2 | 输入不可用：文件缺失、JSON 非法、没有 `records`、`tensors` 子命令缺 torch。 |

发现分叉默认**不**改变退出码——分叉是结论，不是错误。需要 gating 时才加 `--fail-on-divergence`。

## 图模式约束

1. `graph_slot()` 只能在模块构造期调用。forward 内惰性分配会在 capture 时产生新地址，replay 读到的是别的内存。
2. `capture_graph()` 内部只有 `copy_`。它按两侧行数和列数取小值裁剪，形状不匹配时静默跳过而不是抛异常——图内抛异常会毁掉整次 capture。
3. `finish()` 检测到 `torch.npu.is_current_stream_capturing()` 为真时直接返回 `None`。这是最后一道防线，不要依赖它，正确做法是把 `finish()` 放在图外。
4. 图 slot 的读回和 eager 张量共用同一个 `.pt`，key 前缀为 `graph:`。

## 已知限制

1. 探针不做跨 rank 聚合。多 rank 时每个 rank 写自己的文件，`diff` 一次只比一对。跨 rank 求和或 concat 的重建需要自己写脚本。
2. NZ 等非 ND 内部格式只记录 `npu_format` 编号，不做转换。`capture()` 保存的是 `contiguous().clone()` 的结果，比较前两侧格式必须一致。单算子回放里需要 NZ 输入时，在 `replay_op.py` 里显式 `npu_format_cast`。
3. `storage_ptr` 只在同一进程同一次运行内可比。跨进程比较地址无意义，只能比较"是否共享"这一结构性事实。
4. 探针不限制总输出大小。`DUMP_PROBE_TENSOR` 加 `DUMP_PROBE_ROWS` 是唯一的预算手段。
5. `arm()` 的 label 计数不跨进程共享。多引擎进程各自独立计数，需要对齐时用 `DUMP_PROBE_RANKS` 锁定单个 rank。
