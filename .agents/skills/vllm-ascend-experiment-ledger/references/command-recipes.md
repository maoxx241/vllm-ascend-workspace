# Experiment ledger command recipes

## What is in the tree

```bash
python3 .agents/skills/vllm-ascend-experiment-ledger/scripts/experiment_ledger.py index \
  --output .vaws-local/experiment-ledger/index.json
```

Read three fields first:

- `run_count` — how many runs are actually recoverable;
- `unindexed` — files that look like runs but are not;
- `runs_without_identity` — runs whose results cannot be traced to a code state.

The last two are the interesting ones. Report them rather than only the count.

## Narrow to one kind of run

```bash
python3 .agents/skills/vllm-ascend-experiment-ledger/scripts/experiment_ledger.py index \
  --run-type performance --status passed
```

Filters apply to the `runs` list only; `unindexed` still covers the whole tree.

## Trace a result back to its code state

```bash
python3 .agents/skills/vllm-ascend-experiment-ledger/scripts/experiment_ledger.py show \
  --run-id perf-20260901-abcdef
```

The `identity` map is the answer: repository commits, container and versions,
weights, and device layout, as recorded when the run was created. If it is
empty, the run's numbers cannot be attributed and should not be quoted as a
baseline.

## Gate an A/B pair before interpreting it

The intended difference is a code change, so declare that key and let
everything else be checked:

```bash
python3 .agents/skills/vllm-ascend-experiment-ledger/scripts/experiment_ledger.py compare \
  --baseline perf-20260901-baseline \
  --candidate perf-20260901-candidate \
  --vary workspace_snapshot.vllm_ascend_commit
```

Exit code `0` means any delta is attributable to that commit. Exit code `1`
means it is not, and `confounders` names what else moved. The usual entries are
a different container, a different memory-utilization value, or a topology that
was changed at the same time.

Varying more than one key is legitimate when that is genuinely the experiment:

```bash
  --vary topology.tensor_parallel_size --vary topology.data_parallel_size
```

Watch for `declared_but_identical`. A declared key that did not actually differ
means the change under test was never applied to the candidate run.

## Check before repeating work

```bash
python3 .agents/skills/vllm-ascend-experiment-ledger/scripts/experiment_ledger.py index \
  --run-type debug --status passed
```

Scan the identity of the results for the code state and topology you are about
to test. A terminal run with the same identity is an answer already on disk.

State the limit when you conclude nothing matches: the ledger sees
manifest-recorded runs only, so "no prior run" means no prior *recorded* run.

## Create a run that will be traceable

Identity has to be recorded at creation, which means the values are gathered
before the workload starts:

```bash
python3 .agents/scripts/run_manifest.py init \
  --run-type performance \
  --output .vaws-local/<skill>/<run-dir>/manifest.json \
  --workspace-snapshot '{"vllm_ascend_commit":"<sha>","vllm_commit":"<sha>","dirty_files":0}' \
  --environment '{"container":"<name>","cann":"<version>","torch_npu":"<version>"}' \
  --model '{"path":"/home/weights/<model>","quantization":"<scheme>"}' \
  --topology '{"tensor_parallel_size":16,"data_parallel_size":4,"nodes":4}'
```

The commit and dirty-file values come from a probe against the runtime that will
execute the workload, not from the local checkout. Those two can differ, and
when they do, the local values describe a run that did not happen.

`vllm-ascend-multinode-serving` emits exactly this probe as its
`identity_probe`, so a multi-node deployment can populate these fields from the
same command it uses to check for cross-node drift.
