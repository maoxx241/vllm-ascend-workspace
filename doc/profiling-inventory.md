# Profiling Data Inventory

> Last updated: 2026-04-14

远程机器上采集的所有 Ascend NPU profiling 数据索引。

---

## Machine 1: `173.131.1.2` (container port 46000, ~55 GB)

### `/tmp/prof_0.8b_tp1` — Qwen3.5-0.8B TP=1

| 场景 | Rank | CSV 大小 | 采集时间 | 说明 |
|------|------|----------|----------|------|
| scenario_1 | rank0 | 26M | Apr 13 20:51 | 早期命名 |
| s1 | rank0 | 18M | Apr 13 21:24 | pure_prefill |
| s2 | rank0 | 3.3M | Apr 13 21:29 | pure_decode |
| s3 | rank0 | 30M | Apr 13 21:35 | chunk_mixed |
| s4 | rank0 | 11M | Apr 13 21:40 | decode_mixed_batch |

### `/tmp/prof_0.8b_tp2` — Qwen3.5-0.8B TP=2

| 场景 | Rank | CSV 大小 | 采集时间 |
|------|------|----------|----------|
| s1 | rank0, rank1 | 19M each | Apr 13 21:45 |
| s2 | rank0, rank1 | ⚠️ 已 `analyse`，无 `kernel_details.csv`（设备侧 msprof 数据几乎为空，见「补解析记录」） | Apr 13 21:51 |
| s3 | rank0, rank1 | 37M each | Apr 13 21:58 |
| s4 | rank0, rank1 | 11M each | Apr 13 22:05 |

### `/tmp/prof_35b_tp4` — Qwen3.5-35B TP=4

| 场景 | Rank | CSV 大小 | 采集时间 |
|------|------|----------|----------|
| s1 | rank0–3 (×4) | 24M each | Apr 13 22:11 |
| s2 | rank0–3 (×4) | 1.9M each | Apr 13 22:23 |
| s3 | rank0–3 (×4) | 34M each | Apr 13 22:32 |
| s4 | rank0–3 (×4) | 38M each | Apr 13 22:45 |

### `/tmp/prof_35b_dp2tp4` — Qwen3.5-35B DP=2 TP=4

| 场景 | Rank | CSV 大小 | 采集时间 |
|------|------|----------|----------|
| s1 | 8 ranks (dp0/dp1 × tp0–3) | 20–22M each | Apr 13 22:55 |
| s2 | 8 ranks | ⚠️ 已 `analyse`，无 `kernel_details.csv`（同上） | Apr 13 23:11 |
| s3 | 8 ranks | 31–32M each | Apr 13 23:21 |
| s4 | 8 ranks | 22M each | Apr 13 23:41 |

### `/tmp/prof_qwen3_8b` — Qwen3-8B TP=1 (graph mode)

| 场景 | Rank | CSV 大小 | 采集时间 |
|------|------|----------|----------|
| s1 | rank0 | 541K | Apr 14 00:37 |
| s2 | rank0 | 50M | Apr 14 00:44 |
| s3 | rank0 | 43M | Apr 14 00:52 |
| s4 | rank0 | 33M | Apr 14 01:01 |

### `/tmp/prof_qwen3_8b_eager` — Qwen3-8B TP=1 (eager mode)

| 场景 | Rank | CSV 大小 | 采集时间 |
|------|------|----------|----------|
| s1 | rank0 | 725K | Apr 14 01:02 |
| s2 | rank0 | 23M | Apr 14 01:11 |

### `/tmp/prof_dsv2lite` — DeepSeek-V2-Lite TP=1

| 场景 | Rank | CSV 大小 | 采集时间 |
|------|------|----------|----------|
| s1 | rank0 | 1.6M | Apr 14 01:15 |
| s2 | rank0 | ⚠️ 已 `analyse`，无 `kernel_details.csv`（同上） | Apr 14 01:18 |
| s3 | rank0 | 134M | Apr 14 01:28 |
| s4 | rank0 | 31M | Apr 14 01:34 |

### `/tmp/prof_qwen3_30b` — Qwen3-30B-A3B TP=2

| 场景 | Rank | CSV 大小 | 采集时间 |
|------|------|----------|----------|
| s1 | rank0, rank1 | 1.4M each | Apr 14 01:39 |
| s2 | rank0, rank1 | ⚠️ 已 `analyse`，无 `kernel_details.csv`（同上） | Apr 14 01:45 |
| s3 | rank0, rank1 | 141M each | Apr 14 02:00 |
| s4 | rank0, rank1 | 19M each | Apr 14 02:16 |

### 其他

| 路径 | 说明 | 大小 |
|------|------|------|
| `/tmp/prof_test` | 早期测试数据 | 258M |
| `/tmp/prof_0.8b` | 0.8B 早期测试；`scenario_1` 已补解析出 `kernel_details.csv`（24M），其余子目录未逐项核对 | 588M |
| `/tmp/mem_pathB_test` | HBM 内存 profiling 测试 | 微量 |

---

## Machine 2: `173.125.1.2` (container port 46000, ~3.1 GB)

### `/tmp/prof_qwen35_vlm` — Qwen3.5-VL TP=4

| 场景 | Rank | CSV 大小 | 采集时间 |
|------|------|----------|----------|
| s1 | rank0–3 (×4) | 4.3–4.4M each | Apr 14 01:36 |
| s2 | rank0–3 (×4) | 3.7–3.8M each | Apr 14 01:46 |

### 其他用户数据（非 vLLM，仅记录）

| 路径 | 项目 | 说明 |
|------|------|------|
| `/home/g00576449/.../Helios/profiles` | Helios 视频生成 | 1npu 384×640 33f profiling |
| `/home/lanwangli/.../MindIE-SD/profile_outputs_round1` | Stable Diffusion | baseline vs optimized 对比（CSV 可能为空） |
| `/home/xlm/.../QuantConv3D/phase_2_3_profile` | 量化 Conv3D | SeedVR/InvSR 推理 profiling |

---

## 场景编号说明

| 编号 | 场景 | 特征 |
|------|------|------|
| s1 | pure_prefill | 长 prompt，max_tokens=1 |
| s2 | pure_decode | 短 prompt，长生成 |
| s3 | chunk_mixed | 并发长短混合请求 |
| s4 | decode_mixed_batch | 稳态 decode，中等输出长度 |

## 本地已下载分析的数据

| 本地路径 | 远程来源 | 模型 |
|----------|----------|------|
| `/tmp/prof_analysis_35b/` | 173.131.1.2:`/tmp/prof_35b_tp4/s3/rank0` | Qwen3.5-35B TP=4 s3 |
| `/tmp/prof_qwen3_8b/` | 173.131.1.2:`/tmp/prof_qwen3_8b/s1/rank0` | Qwen3-8B s1 |
| `/tmp/prof_dsv2lite/` | 173.131.1.2:`/tmp/prof_dsv2lite/s1/rank0` | DSV2-Lite s1 |
| `/tmp/prof_08b_tp1/` | 173.131.1.2:`/tmp/prof_0.8b_tp1/s1/rank0` | Qwen3.5-0.8B TP=1 s1 |

## 补解析记录（2026-04-14）

在 `173.131.1.2:46000` 容器内执行：`source /usr/local/Ascend/ascend-toolkit/latest/bin/setenv.bash` 后，用 `/usr/local/python3.11.14/bin/python3` 调用 `torch_npu.profiler.profiler.analyse(<*_ascend_pt 目录>)`。

| 远程路径 | 结果 |
|----------|------|
| `/tmp/prof_0.8b/scenario_1`（rank0） | ✅ 已生成完整 `ASCEND_PROFILER_OUTPUT`，含 `kernel_details.csv`（约 24M） |
| `/tmp/prof_0.8b_tp2/s2`（rank0、rank1） | ⚠️ `analyse` 完成但**无** `kernel_details.csv`；解析日志含 `Failed to get acl to npu flow events`、`torch op data is empty`；原始目录中 `FRAMEWORK/` 仅有 `torch.op_mark`、无 `torch.op_range`，`PROF_*/device_0` 体量约 72K（对比正常采集约百 MB 级），判定为**采集阶段设备侧数据未落盘/会话过短**，非单纯缺 `analyse` |
| `/tmp/prof_35b_dp2tp4/s2`（8 个 rank） | ⚠️ 同上，仅部分产物（如 `trace_view.json`、`api_statistic.csv`、`*.db`），无 `kernel_details.csv` |
| `/tmp/prof_dsv2lite/s2`（rank0） | ⚠️ 同上 |
| `/tmp/prof_qwen3_30b/s2`（rank0、rank1） | ⚠️ 同上 |

**结论**：除 `prof_0.8b/scenario_1` 外，上述 s2 类目录无法仅靠离线 `analyse` 补出 `kernel_details.csv`；需在相同软硬件版本上**重新采集** profiling（保证 profiler 正常结束、设备侧 `device_0/data` 与 `torch.op_range` 等完整写入）。
