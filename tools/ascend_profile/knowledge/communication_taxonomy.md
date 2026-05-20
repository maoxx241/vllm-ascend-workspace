# HCCL Communication Taxonomy

This file is the contract every analysis stage in this repo uses when it
talks about HCCL collective operators, communication-side fused kernels,
and per-task synchronization primitives.

Sources:
- CANN HCCL user guide §"通信算子下发" / §"通信算子执行" / §"典型算子行为分析"
  ([hcclug_000017](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/900/API/hcclug/hcclug_000017.html),
   [hcclug_000018](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/900/API/hcclug/hcclug_000018.html),
   [hcclug_000019](https://www.hiascend.com/document/detail/zh/CANNCommunityEdition/900/API/hcclug/hcclug_000019.html))
- HCCL collective API references: `HcclAllReduce`, `HcclAllGather`,
  `HcclReduceScatter`, `HcclAlltoAllV`, `HcclAlltoAllVC`, `HcclBatchSendRecv`.

## 1. Two layers of HCCL events in profiling

Profiling captures HCCL traffic at **two** layers; the analyzer must
keep them separate.

| Layer | Where it appears | What it represents |
|---|---|---|
| Op-level | `kernel_details.csv` rows whose `Accelerator Core` column is `COMMUNICATION` (top-level collective task) | The user-issued collective (`hcom_allReduce_xx`, `hcom_alltoallv_yy`, ...) on a Plane stream. Always available. |
| Task-level | `communication.json` (a.k.a. profiling **level 1**) sub-task records *inside* each top-level collective | The notify / memcpy / RDMASend tasks the collective expanded into. Only available when profiling was captured at `level >= 1`. |

The op-level layer is what `operator_summary.csv` aggregates.  The
task-level layer is consumed by the cross-rank diagnoser when
`communication.json` is present; if the source profile only captured
level 0 the analyzer must skip the task-level breakdown rather than
fabricate it.

## 2. Op-kind taxonomy (from `task_type`)

The `task_type` column carries the collective's HCCL kind verbatim.
Mapping to canonical labels used in this repo:

| `hccl_op_kind` | `task_type` patterns | Notes |
|---|---|---|
| `allreduce` | `HCOM_ALLREDUCE_` | sum / prod / max / min reduce + broadcast |
| `allgather` | `HCOM_ALLGATHER_` | concat across ranks |
| `reducescatter` | `HCOM_REDUCESCATTER_` | reduce + scatter (TP partial sum splits) |
| `alltoallv` | `HCOM_ALLTOALLV_`, `HCOM_ALLTOALLVC_` | EP dispatch / combine raw transport |
| `broadcast` | `HCOM_BROADCAST_` | parameter / KV cache broadcast |
| `send_recv` | `HCOM_SEND_`, `HCOM_RECEIVE_`, `HCCL_BATCHPUT_`, `HCCL_BATCHSENDRECV_` | point-to-point / micro-batched send-recv |
| `barrier` | `HCOM_BARRIER_` | rendezvous / sync only |
| `comm_aiv_fused` | any `HCOM_*` whose row is `op_type == mix_comm_aiv` | dispatch / combine / distribute kernels with AIV compute fused |
| `other` | anything else with a `HCOM_` / `HCCL_` prefix | catch-all; surfaces in the report so we can extend the table |

The `comm_aiv_fused` label is **not** a task_type in CANN; it is
synthesized by the normalizer when an op carries
`Accelerator Core = COMMUNICATION` together with non-zero AIV stage
time (see `pipeline_taxonomy.md` § 4).  Typical members: vLLM-Ascend's
`MoeDistributeDispatch`, `MoeDistributeCombine`, `DispatchFFNCombine`.

## 3. Sub-task primitives (level-1 only)

When `communication.json` (level 1) is present, every collective expands
into a sequence of per-stream tasks.  We use CANN's terminology
verbatim:

| Sub-task | Role | What its duration measures |
|---|---|---|
| `Notify Record` | sync | Setting the local notify register to 1 (front-of-collective barrier). |
| `Notify Wait` | sync | Spinning for the peer's notify register to become 1, then resetting it.  This is the dominant "exposed wait" primitive — long durations indicate the local rank is **waiting on a slower peer**. |
| `RDMASend` (sync, 4-byte payload) | sync | Inter-node `notify` performed via RDMA. |
| `RDMASend` (data, payload > 4 B) | data | Inter-node WQE submission for a data transfer.  The recorded duration is *not* the wire transfer time — it is just WQE-submit; the actual transmission is reflected in the *next* `Notify Wait` task. |
| `Memcpy` | data | Intra-node / on-chip memory copy. |
| `Reduce_Inline` | data | Memcpy + on-the-fly reduce (`AllReduce` ring stage). |

Per-task fields exposed by level-1 profiling:
`duration_us`, `notify_id`, `src_rank`, `dst_rank`, `size_bytes`,
`bandwidth_GBps`, `plane_id` (which Plane / sub-stream).

## 4. Plane / Group structure

- **Group** — one HCCL communication domain (one `HcclComm`).
- **Plane** — one logical sub-stream inside a group; HCCL splits
  collectives across multiple Planes to use HCCS bandwidth in parallel.
  Plane id is recorded as `Stream Id` for HCCL events.
- A single user-issued `hcom_allReduce_xx` op may decompose into many
  Plane events, all with the **same** `op_id` / `op_name` but different
  `stream_id`.

## 5. Diagnostic implications

Common failure patterns we infer from this taxonomy:

| Symptom | Evidence required |
|---|---|
| `communication_collective_slow` | The same collective (matching `op_name` modulo the trailing counter) shows large duration skew (`max - min ≥ 30 % of mean`) across ranks within an aligned step window. |
| `comm_aiv_compute_imbalance` | `mix_comm_aiv` op's AIV pipeline time differs by > 30 % across EP peers — usually EP-routing imbalance. |
| `notify_wait_dominant` (level-1) | One rank's `Notify Wait` total > 50 % of the collective's wall-time → that rank is waiting on a slower peer. |
| `rdma_bandwidth_underused` (level-1) | Inter-node `RDMASend`(data) reports `bandwidth_GBps` < 30 % of the link's nominal capacity. |
| `host_dispatch_bound` | Many short collectives back-to-back with high CPU `Notify Wait` and idle device — host-side `aclrtLaunch` ordering is the bottleneck.  Mitigation: bind CPU cores, or `export HCCL_OP_EXPANSION_MODE=AIV` (see hcclug_000017). |

## 6. What this repo emits

| File | Schema | Source data required |
|---|---|---|
| `operator_summary.csv` | One row per `(rank_id, name, task_type)`, with `op_type ∈ {communication, mix_comm_aiv}` rows for HCCL kernels. | level 0 (always available). |
| `operator_class_summary.csv` | Rank-merged version of the same aggregate; one row per `(name, task_type)` with `rank_count` and per-rank `duration_skew_ratio`. | level 0. |
| `hccl_op_summary.csv` | One row per `(hccl_op_kind, rank_id)` with total wall, call count, p50 / p90 / max wall, total wait, and `comm_aiv_share` (fraction of duration that came from `mix_comm_aiv` rows). | level 0. |
| `report.md` § "Operator View" | top compute operators table + HCCL summary table + per-rank skew. | level 0. |
| `cross_rank_alignment.csv` | Aligned collectives across ranks with duration skew. | level 0. |
| (future) `hccl_task_summary.csv` | Per `(collective, sub_task_type)` totals for `Notify Wait` / `RDMASend(data)` etc. | level 1 only. |

## 7. Limitations

- We cannot diagnose **which peer** slowed a collective unless the
  source profile captured level 1; level 0 only tells us *that* the
  duration skew exists.
- For `comm_aiv_fused` ops the pure-comm portion of wall time is not
  decomposable from the AIV portion using level 0 data; we report the
  combined wall time and surface AIV pipeline stages separately so the
  user can see the AIV contribution.
- `Stream Id` ↔ Plane id mapping is product-dependent; we keep
  `stream_id` in the raw kernel index but do not assume Plane semantics
  unless `communication.json` confirms it.
