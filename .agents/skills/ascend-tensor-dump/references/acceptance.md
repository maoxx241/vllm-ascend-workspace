# Acceptance

用 dump 结论下判断之前，逐项确认。任何一项不满足，结论都不成立。

## 采集前

1. 复现是确定性的：权重目录、token id 数组、采样参数、seed、并发、topology、特性开关全部记录在案。**长度相同不等于 token 相同**，比对两个引擎或两轮实验时要比 token id 数组本身。
2. 更便宜的手段已经用完：单算子开关消融、首个坏 token 的输出对拍都做过，或有明确理由跳过。
3. 已有 artifact 无法证伪当前假设。能证伪就不要加钩子。
4. 探针文件在服务实际 import 的那棵树里，路径已经用 `vllm_ascend.__file__` 确认过。

## 采集后

5. 做过探针开关对照，dump 开和关时症状一致。不一致则结论作废，先修探针。
6. `scan` 的 `records_without_summary` 已经检查过，没有本该被统计却漏掉的 stage。
7. manifest 的 `label`、`match_index`、`occurrence`、`metadata` 确认选中的是预期的那个请求，不是它的某个 prefill chunk。
8. 落了整张量时，`DUMP_PROBE_ROWS` 有明确取值，dump 总量可控。

## 比较

9. `diff` 的 verdict 不是 `COVERAGE_MISMATCH`，或已经解释清楚为什么单侧独有。覆盖不对称说明两轮跑的不是同一条路径，此时数值比较无意义。图模式下 `capture()` 记录数远少于 eager 是这条的典型表现——换 `graph_slot` + `capture_graph`，别去解读残缺的记录。
10. shape 或 dtype 不匹配的条目已经单独处理，没有被当成"数值差异"。
11. 容差是显式选定的。`tensors` 的默认 `1e-2` 只用于粗筛，任何"精度对齐"或"逐位一致"的结论都必须附上实际使用的 `atol` 和 `rtol`。
12. 出现非有限值时，结论落在"哪个 stage 先出现 NaN/Inf"，而不是被平均过的 diff 指标。
13. 有 `storage_aliases` 且 `stride_conflict` 为真时，已经检查过是否为共享存储冲突，而不是直接归因到数值误差。

## 图模式

14. 明确说明这一轮测的是图路径还是 eager 路径。如果为了拿到数据强制走了 eager（`skip_compiled`、`--enforce-eager`），必须写明"本轮结论不覆盖图路径"。
15. 图内没有 D2H：forward 路径里没有 `.cpu()`、`.item()`、`.tolist()`、设备值 `print`。
16. `graph_slot` 在模块构造期分配，不在 forward 内。
17. `finish()` 在图外、replay 之后调用。

## 单算子

18. 参考实现是 CPU FP32 或规范公式，不是另一条历史兼容代码路径。
19. 输入集包含算子需要的全部参数，非张量参数（标量、布尔、group 类型）也在内。
20. 内部格式一致：需要 NZ 时两侧都已转换，比较发生在同一格式上。
21. 单算子结论和服务级现象已经连上：能说明这个算子的偏差如何产生观测到的输出错误。

## 结案

22. 结论形式是"写者 → 地址 → 读者"或"算子合同不符"，不是"某层数值偏大"。
23. 所有 `capture`、`capture_inputs`、`capture_graph`、`graph_slot` 调用已删除，不是仅关闭环境变量。
24. 删除插桩后重跑过最小复现和原始复现。
25. dump 路径作为证据引用进 `vllm-ascend-graph-debug` 的 case 或 `ascend-operator-debug` 的矩阵。这个 skill 不新建 Run Manifest。
26. 新根因经过验证后，由正常任务总结捕获；需要共享时使用包生成的脱敏副本。

## 不需要做的事

- 不需要给 dump 文件算 sha256，也不需要 manifest 签名。要钉身份的是**输入**：权重目录、token id 数组、算子 `.so`。
- 不需要为探针本身写校验或自检。探针出错的表现是没有数据，不是错误数据。
- 不需要把全量 `.pt` 拉回本地。`scan` 和 `diff` 在远端读 JSON 就能出结论。
