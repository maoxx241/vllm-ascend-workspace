---
name: vllm-ascend-multinode-serving
description: Plan and bring up one vLLM Ascend data-parallel service spanning several NPU nodes, deriving per-node rank offsets, headless roles, socket-interface and HCCL environment, port allocation, cross-node code-identity checks, and a staged readiness gate. Use when a topology needs more devices than one host provides, when a multi-node service hangs during collective init, or when nodes may be running different code. Do not use for single-node serving, prefill/decode disaggregation, generic Ray clusters, benchmarking, or diagnosing an already-reproduced distributed failure.
---

# vLLM Ascend Multi-Node Serving

Own the bring-up contract for a data-parallel service whose ranks span several
hosts. One node's launch command is not interesting; the contract between nodes
is, because that is where the recurring failures live.

## Use this Skill when

- the topology needs more devices than one host provides, so `tp x dp` spans nodes
- a multi-node service hangs during distributed init with no obvious error
- the master node reports a timeout and you need to find which node actually failed
- nodes may be running different code after a partial sync or a rebuild on one host

## Do not use this Skill when

- the topology fits one host; use `vllm-ascend-serving`
- the deployment is prefill/decode disaggregation; use `vllm-ascend-pd-serving`
- the failure is already reduced to a rank, collective, or process group; use `vllm-ascend-distributed-debug`
- the task is throughput measurement; use `vllm-ascend-benchmark`

## Workflow

1. Resolve one session per node with `session-management`, so each node has its
   own container endpoint and NPU lease. This Skill takes data-plane addresses
   only; it never duplicates connection state.
2. Establish code parity on every node with `remote-code-parity`, then confirm
   the runtime identity is byte-identical across nodes (step 5). A multi-node
   result computed on drifted nodes is not a result.
3. Run `scripts/multinode_plan.py plan` and read the derived rank map,
   `warnings`, and port allocation before consuming NPU time.
4. Launch every non-master node first, then the master. The master blocks until
   all declared data-parallel ranks report in, so a missing worker presents as a
   master-side hang rather than a worker-side error.
5. Run the identity probe on every node and diff the output. Stop on any
   difference; do not interpret results from mismatched nodes.
6. Walk the staged readiness gate in order. Record no measurement taken before
   the final stage passes.
7. Record the deployment in `vllm-ascend-experiment-ledger` so later runs can
   prove which code state and topology produced their numbers.

## Entry point

`scripts/multinode_plan.py` provides:

- `plan`: validate the topology, derive per-node rank offsets and roles, build
  the environment contract, allocate ports, and emit the identity probe and
  readiness gate;
- `render`: print one node's shell block, either the launch command
  (`--what launch`) or the identity probe (`--what identity`).

The planner is offline by design. It produces commands; the agent executes them
through the remote-dev companion tools. Nothing in this Skill needs an NPU to
be validated, which is why its rank arithmetic is unit-tested.

Read:

- [Behavior contract](references/behavior.md) for the topology invariants, load-balancing modes, and environment contract.
- [Command recipes](references/command-recipes.md) for concrete four-node and two-node examples.
- [Acceptance](references/acceptance.md) before claiming the deployment is up.

## Rules

- Declaration order of `--node` defines the data-parallel rank offsets. Keep it
  stable across restarts; a reordered node list silently remaps ranks.
- Exactly one load-balancing mode. Internal LB gives one API server on the
  master and headless engines elsewhere; hybrid LB gives every node an API
  server behind an external balancer. vLLM rejects the combination.
- Always set the data-plane socket interface explicitly. Left unset, gloo and
  HCCL pick an interface by heuristic and may select the management NIC, which
  connects and then performs badly rather than failing loudly.
- Allocate an explicit HCCL socket port range and keep the API and
  data-parallel RPC ports outside it. On a shared host, the default range
  collides with a co-tenant service.
- A 200 from `/health` is not readiness. It is reachable before graph capture
  finishes and before the engine can decode. Readiness requires `/v1/models`
  plus one real completion with non-empty output.
- Graph capture on large MoE weights can take tens of minutes. Waiting is not a
  failure; restarting a capturing service wastes the whole capture.
- On failure, read the failing node's own log and find the first fatal line.
  The master log shows a timeout, not a cause.
- Memory-utilization values are topology-bound. A value verified at one
  `tp x dp x ep` layout can leave a negative KV budget at another, because the
  expert shard per device changes. Re-derive it; do not carry it over.
- Stop only the process group you started, on the node you started it on, and
  confirm the devices are actually released afterwards. Engine and worker
  processes outlive the API process and keep holding HBM.
- Keep plan state under `.vaws-local/multinode-serving/`.
