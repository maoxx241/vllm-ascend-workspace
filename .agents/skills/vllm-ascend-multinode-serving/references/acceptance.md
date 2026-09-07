# Multi-node serving acceptance

A multi-node deployment may be called up only when every item below holds.
Partial satisfaction is a deployment that is still starting.

## Identity

- [ ] The identity probe ran on **every** node, before the first workload request.
- [ ] All nodes report the same commit for every declared repository.
- [ ] All nodes report the same dirty-file count.
- [ ] All nodes resolve `vllm` and `vllm_ascend` to the same module paths.
- [ ] All nodes report the same native-extension SHA256, or none of them has a
      native extension.
- [ ] The service was started **after** the last source change on every node.

A single mismatch here invalidates every number the deployment produces. Fix
the drift and restart; do not annotate the result as approximate.

## Topology

- [ ] The device counts were derived from the **logical** device count confirmed
      by `npu-smi info`, not from the physical card count.
- [ ] `world_devices` in the plan equals the devices actually held.
- [ ] Each node's `data_parallel_ranks` matches the ranks it reports at init.
- [ ] `--node` order is the same as the previous start of this deployment, or
      the rank remap is intentional and recorded.
- [ ] Exactly one load-balancing mode is in effect.
- [ ] The memory-utilization value was derived for **this** topology, not
      carried over from another.

## Readiness

- [ ] Every node's process group is alive.
- [ ] `/health` returns 200 on the master.
- [ ] `/v1/models` lists the served model on the master.
- [ ] One real completion returned non-empty output.
- [ ] No measurement in the record predates the completion check.

## Evidence

- [ ] The plan JSON is stored under `.vaws-local/multinode-serving/`.
- [ ] Every node's log path is recorded, and the logs are retained on failure.
- [ ] The identity probe output is retained alongside the plan.
- [ ] The deployment is registered in `vllm-ascend-experiment-ledger`, so a
      later run can prove what produced its numbers.

## What this Skill does not establish

State these explicitly rather than letting them be assumed:

- Correctness. A deployment that decodes is not a deployment that decodes
  correctly; that is `vllm-ascend-correctness-validation`.
- Performance. Throughput and latency belong to `vllm-ascend-benchmark`, and a
  regression verdict belongs to `vllm-ascend-performance-regression`.
- Graph-mode health beyond capture completing. Divergence between graph and
  eager is `vllm-ascend-graph-debug`.
- Root cause for a reproduced distributed failure. Once a failing rank,
  collective, or process group is identified, hand off to
  `vllm-ascend-distributed-debug`.

## On failure

- [ ] The first fatal line was located in the **failing** node's own log, not
      inferred from the master timeout.
- [ ] The failure shape is classified as never-connected, connected-then-blocked,
      or started-then-died before any configuration is changed.
- [ ] No orphan process from a previous run is holding devices.
- [ ] If the observed signature has no verified match in
      `.agents/knowledge/`, that is stated explicitly rather than implied.
