# Multi-node serving behavior contract

## Contents

- [Topology invariants](#topology-invariants)
- [Load-balancing modes](#load-balancing-modes)
- [Environment contract](#environment-contract)
- [Port allocation](#port-allocation)
- [Identity probe](#identity-probe)
- [Readiness gate](#readiness-gate)
- [Failure attribution](#failure-attribution)

## Topology invariants

The planner refuses to emit a plan unless both hold:

- `devices_per_node == tensor_parallel_size * data_parallel_size_local`
- `node_count * data_parallel_size_local == data_parallel_size`

These are the two mistakes that cost the most time, because neither fails at
launch. A wrong rank map produces processes that start, connect partially, and
then block in collective init until the HCCL timeout expires — minutes later,
with a message that points at the wrong node.

`--dp-local` defaults to `data_parallel_size / node_count` and is rejected when
that division is not exact. `--devices-per-node` is optional; when supplied it
is checked against `tp * dp_local` rather than used to override it.

**Every device count here is a logical device count, not a physical card
count.** On A3 hardware one card presents two dies, so a node with eight cards
holds sixteen logical devices and `ASCEND_RT_VISIBLE_DEVICES` runs `0..15`.
Sizing the topology from the card count halves the width — for example
`tp=8, dp=2` where `tp=16, dp=4` was intended. That mistake does not surface as
a topology error: it surfaces as a fused-MoE shape error such as
`GroupedMatmulWeightNz ... dim num should be 2 ... now is 1`, which is
repeatedly misread as an operator bug. Confirm the logical device count with
`npu-smi info` on the host before sizing anything.

`--ep` is advisory. When it differs from `tp * dp` the plan carries a warning
instead of failing, because expert-parallel width is a model-level decision and
some layouts legitimately diverge from the device count.

Rank offsets follow `--node` declaration order: node at index `i` receives
`data_parallel_start_rank = i * data_parallel_size_local`. Reordering the node
list therefore remaps ranks. Keep the order stable across restarts of the same
deployment.

## Load-balancing modes

`--lb-mode internal` (default): the master node runs `vllm serve` with no
`--headless` and no `--data-parallel-start-rank`, and owns the single API
server. Every other node runs the same command plus `--headless` and its own
`--data-parallel-start-rank`. vLLM forces `api_server_count = 0` in headless
mode, so those nodes contribute engine ranks only.

`--lb-mode hybrid`: every node runs an API server and receives
`--data-parallel-hybrid-lb` together with its `--data-parallel-start-rank`.
vLLM load-balances across local ranks; an external balancer must sit in front
of the nodes. Use this only when that external balancer exists.

vLLM rejects combining hybrid load balancing with external load balancing
(`--data-parallel-rank`), so the planner exposes one choice and never mixes
them.

## Environment contract

Every node receives the same keys with node-specific values:

| Variable | Value | Why it is not optional |
|---|---|---|
| `ASCEND_RT_VISIBLE_DEVICES` | `0..devices_per_node-1` | pins the device set per node |
| `HCCL_IF_IP`, `VLLM_HOST_IP` | that node's data-plane address | binds collectives to the data network |
| `HCCL_SOCKET_IFNAME`, `GLOO_SOCKET_IFNAME`, `TP_SOCKET_IFNAME` | `--data-nic` | left unset, the stack picks an interface by heuristic and may choose the management NIC, which connects and then performs badly instead of failing |
| `HCCL_CONNECT_TIMEOUT` | `1800` by default | large MoE weights load slower than the stock timeout allows, producing init failures that look like topology bugs |
| `HCCL_NPU_SOCKET_PORT_RANGE` | `--hccl-port-range` | the default range collides with co-tenant services on a shared host |
| `PYTORCH_NPU_ALLOC_CONF` | `expandable_segments:True` | turns fragmentation OOM into a working topology |
| `OMP_PROC_BIND`, `OMP_NUM_THREADS` | `false`, `10` | avoids thread oversubscription across ranks |

`--extra-env KEY=VALUE` merges on top and can override any of these. Keys must
match `^[A-Z_][A-Z0-9_]*$`.

Serving flags that are not part of the node contract pass through the trailing
`--serve-args` section, which consumes the rest of the command line and is
appended verbatim to every node command. It must come last, and it is the only
way flag-shaped values survive argument parsing.

Two environment facts belong to the container, not to this plan, and are
enforced elsewhere: `PYTHONPATH` must be appended to rather than overwritten,
or the CANN `acl` module disappears; and the plugin list must be complete, or
model registration silently fails and surfaces as an unrelated attribute error
during model init. Both are recorded as failure signatures in
`.agents/knowledge/known-failure-signatures.yaml`.

## Port allocation

Three port groups must not overlap:

- the API port on the master (or on every node under hybrid LB);
- the data-parallel RPC port, shared by all nodes;
- the HCCL socket port range.

The planner rejects an API or RPC port that falls inside the HCCL range, and
rejects an API port equal to the RPC port. It does not probe the hosts for
occupancy, so a port that is free in the plan can still be taken at launch;
`Address already in use` at launch means pick another port, not retry.

## Identity probe

The probe prints, for every declared repository path: the commit, the dirty
file count, the resolved `vllm` and `vllm_ascend` module paths, the native
extension module path, and the SHA256 of every `vllm_ascend_C*.so` it finds.

Run it on every node and compare. All nodes must agree on every field. A
difference means the nodes are not running the same code, which invalidates any
cross-node result. The common causes are a rebuild performed on one host only,
a sync that dropped a nested submodule, and a service that was never restarted
after the source changed.

Run the probe before the first workload request, not after. Its value is
proving what produced a result, and that proof has to exist before the result
does.

## Readiness gate

Four stages, in order:

1. **processes-alive**, on every node. A worker failure appears on the master
   only as a timeout, so aliveness is checked per node.
2. **health**, on the master. Proves the HTTP server is up and nothing more.
3. **models**, on the master. Proves engine core finished registering the model.
4. **real-completion**, on the master. One deterministic request with non-empty
   output. This is the first stage that proves the deployment can decode.

No measurement taken before stage 4 passes may be recorded. Stages 2 and 3
being reached is not partial success for measurement purposes; it is a service
that is still starting.

## Failure attribution

When the master reports a timeout, the master log contains the symptom and not
the cause. Read each node's own log and find the first fatal line, earliest
timestamp first. A single failing rank takes down every other rank, so the
loudest log is rarely the origin.

Distinguish three failure shapes before changing anything:

- **never connected**: ranks missing from init. Check the rank map, the socket
  interface, and whether a node was launched at all.
- **connected then blocked**: all ranks present, no progress. Check collective
  ordering and whether one node is running different code.
- **started then died**: check that node's log for OOM or an engine-core fault,
  and confirm no orphan process from a previous run is holding devices.
