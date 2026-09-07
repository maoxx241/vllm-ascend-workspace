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

`rank_complete` is the completion marker: a rank emits it as its final event
when the reproduction ran through on that rank. It is the only structural
evidence that separates "no mismatch was detected in what we captured" from
"every rank finished without a mismatch".

```json
{
  "timestamp": "2026-07-25T12:05:00Z",
  "rank": 3,
  "phase": "shutdown",
  "event": "rank_complete"
}
```

## Case config

`parent_run_id` is optional and names the change-validation plan this case is
evidence for; `change_validation.py link` refuses manifests without it.

## Finding confidence

- `confirmed`: explicit structured evidence violates topology, group, operation,
  participant, or endpoint invariants;
- `candidate`: evidence localizes a stall or cross-rank phase divergence but does
  not prove its cause;
- `incomplete`: required ranks or evidence are absent.

Missing evidence never becomes a confirmed finding.

## Analysis status and Run Manifest

`analyze` computes one analysis status and maps it onto the manifest:

| Analysis status | Meaning | Manifest |
|---|---|---|
| `diagnosed` | at least one `confirmed` finding | `failed` |
| `inconclusive` | an `incomplete` finding, or no events at all | `inconclusive` |
| `hypothesis` | only `candidate` findings | `inconclusive` |
| `no-mismatch-detected` | no findings, but some rank's last event is not `rank_complete` | `inconclusive` |
| `completed-without-mismatch` | no findings and every rank's last event is `rank_complete` | `passed` |

`passed` therefore requires positive evidence from every rank that the run
finished, not merely the absence of detected problems. `analysis.json` lists
`completed_ranks` and `incomplete_ranks`. Ingest all events, including the
completion markers, before calling `analyze`; the manifest becomes terminal on
the first `analyze`.

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
