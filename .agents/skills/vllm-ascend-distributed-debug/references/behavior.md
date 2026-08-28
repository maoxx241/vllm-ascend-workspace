# Distributed debug behavior contract

## Case config

The config records the original failure topology. Every rank must contain:

```text
global_rank, node, device, local_rank,
tp_rank, pp_rank, dp_rank, ep_rank, pcp_rank, dcp_rank
```

Global ranks must be unique and contiguous. Process groups name their exact rank
members. Network endpoints declare a name, address, and port. Duplicate bindings
are rejected before evidence collection.

Environment snapshots must contain no credentials or tokens.

## Normalized rank event

```json
{
  "timestamp": "2026-07-25T12:00:00Z",
  "rank": 3,
  "phase": "model-execute",
  "event": "collective_enter",
  "group": "tp-1",
  "sequence": 42,
  "operation": "all_reduce"
}
```

All events require timestamp, rank, phase, and event. `collective_enter` and
`collective_exit` also require group, sequence, and operation.

## Finding confidence

- `confirmed`: explicit structured evidence violates topology, group, operation,
  participant, or endpoint invariants;
- `candidate`: evidence localizes a stall or cross-rank phase divergence but does
  not prove its cause;
- `incomplete`: required ranks or evidence are absent.

Missing evidence never becomes a confirmed finding.

## Case layout

```text
case/
├── manifest.json
├── case-config.json
├── topology.json
├── environment.json
├── process-tree.json
├── network-endpoints.json
├── events.jsonl
├── rank-logs/
├── stack-dumps/
├── metadata-samples/
├── analysis.json
├── report.md
└── reproduction.md
```
