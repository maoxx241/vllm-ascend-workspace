# Historical graph tensor capture observation

Status: historical, unverified in this change
Confidence: low

Source: prior ascend-tensor-dump SKILL.md, migrated 2026-09-11.
Exact runtime versions, raw trace and original run identifier are unavailable.

**图内的 `capture()` 在 replay 时根本不执行。** replay 只重放设备 kernel，不重入 Python，所以 forward 体内的打点只在 capture 那一次生效。实测同一份插桩在 Qwen3-0.6B 同一个 decode step 上：eager 出 114 条记录，aclgraph 只出 2 条——活下来的两条在 model runner 里，本来就在图外；而 28 个 `graph_slot` 缓冲全部带回了数据。所以图模式下**记录数骤降是缺数据，不是"两边一致"**。

The reported counts are contextual evidence only, not model-wide constants.
Reproduction needs fixed weights, token IDs, stage selectors and execution mode.
Graph copy slots and ordinary Python callbacks observe different execution events.
