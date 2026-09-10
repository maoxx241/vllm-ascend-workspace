# Graph debug behavior contract

## Contents

- [Case lifecycle](#case-lifecycle)
- [Snapshot contract](#snapshot-contract)
- [Comparison semantics](#comparison-semantics)
- [Run Manifest integration](#run-manifest-integration)
- [Failure boundaries](#failure-boundaries)

## Case lifecycle

Use `scripts/graph_debug_case.py` to keep one structured case directory:

```text
case-dir/
├── case.json
├── manifest.json
├── comparisons/
│   └── comparison-001.json
└── validation/
    ├── minimal-reproduction.<ext>
    └── original-reproduction.<ext>
```

The lifecycle is:

1. `init`: record environment, workspace snapshot, topology, reproduction, eager result, graph result, and the classified failure stage. Pass `--parent-run-id` when the case is evidence for a change-validation plan; `link` refuses cases without it.
2. `record`: append one single-variable experiment with its hypothesis, expected observation, actual observation, conclusion, and next step.
3. `compare`: consume an observational certificate built from each snapshot's `{stem}.identity.json` (or `--eager-identity` / `--graph-identity`) plus the case identity, then align eager and graph snapshots by `step/layer/rank/tag`. The default variable under test is `execution_mode`. A `not-comparable` pair, including a declaration/observation mismatch retained on each certificate side, is refused; numeric alignment is not recorded as if the snapshots were a pair.
4. `finalize`: record the root cause, fix, minimal-reproduction result, original-reproduction result, and debug-instrumentation cleanup, attaching the rerun output behind every `pass` claim.

A case is resolved only when both reproductions pass and instrumentation is removed or disabled. Other final results are `inconclusive`.

### Resolution evidence

`finalize` fails closed. It refuses to write a resolution when any of these is missing, and its error names every missing item at once:

- `--minimal-result pass` requires `--minimal-evidence PATH`, the non-empty output of rerunning the minimal reproduction after the fix;
- `--original-result pass` requires `--original-evidence PATH`, the non-empty output of rerunning the original reproduction after the fix;
- resolving a case (both results `pass`) requires at least one recorded experiment.

Evidence files are copied into `validation/` and linked from the Run Manifest as `reproduction-rerun-output` artifacts, so a reviewer can open exactly what the `pass` claim rests on. A `fail` result needs no evidence file; it can only lead to `inconclusive`. The script checks that evidence exists and is non-empty; it does not interpret its contents.

## Snapshot contract

Write UTF-8 JSON Lines. Each non-empty line is one object with this key:

```json
{
  "step": 0,
  "layer": 1,
  "rank": 0,
  "tag": "attention-output",
  "shape": [16, 4096],
  "dtype": "bfloat16",
  "stats": {
    "min": -1.25,
    "max": 2.5,
    "mean": 0.03125,
    "var": 0.75
  },
  "sample": [0.5, 0.25, -0.125]
}
```

Required fields:

- `step`, `layer`, and `rank`: integers;
- `tag`: non-empty string;
- `stats`: object when numeric statistics are captured.

Optional `sample` may contain nested numeric arrays. Duplicate alignment keys are invalid.

Graph capture constraints:

- allocate snapshot buffers before capture;
- perform only device-side `copy_` and supported device-side statistics inside captured execution;
- synchronize, read to CPU, and write JSONL outside capture;
- use identical tags and representative-slice logic in eager and graph runs.

## Comparison semantics

The comparator:

- aligns records by `step/layer/rank/tag`;
- sorts aligned keys deterministically;
- reports missing records as divergence;
- compares all shared statistic fields and optional samples;
- accepts independent absolute and relative tolerances;
- reports the earliest divergent key as `first_divergence`;
- does not decide whether a tolerance is acceptable for a model or dtype.

Default tolerances are zero. Choose non-zero tolerances explicitly and record why.

## Run Manifest integration

`init` creates Run Manifest v1 with `run_type=debug` and the optional `parent_run_id`. The first experiment or comparison moves it to `running`. Each comparison becomes a linked artifact. `finalize` links `case.json` and every validation evidence file and moves the manifest to:

- `passed` for a resolved case;
- `inconclusive` otherwise.

`passed` is unreachable without at least one experiment record and two rerun outputs; `init` followed directly by `finalize` is rejected.

Do not store passwords, tokens, credentials, or secret environment variables in case inputs.

## Failure boundaries

The script manages evidence and comparison; it does not:

- launch a vLLM service;
- synchronize local code to a remote machine;
- collect device logs or stack dumps;
- insert instrumentation into vLLM or vllm-ascend automatically;
- decide task-level accuracy acceptance;
- diagnose eager failures.

Bind actual sources and let coordinator prepare the managed run; use the appropriate serving or distributed-debug workflow for business checks.
