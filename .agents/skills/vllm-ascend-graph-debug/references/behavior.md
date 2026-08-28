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
└── comparisons/
    └── comparison-001.json
```

The lifecycle is:

1. `init`: record environment, workspace snapshot, topology, reproduction, eager result, graph result, and the classified failure stage.
2. `record`: append one single-variable experiment with its hypothesis, expected observation, actual observation, conclusion, and next step.
3. `compare`: align eager and graph snapshots by `step/layer/rank/tag`, compare statistics and optional samples, then record the first divergence.
4. `finalize`: record the root cause, fix, minimal-reproduction result, original-reproduction result, and debug-instrumentation cleanup.

A case is resolved only when both reproductions pass and instrumentation is removed or disabled. Other final results are `inconclusive`.

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

`init` creates Run Manifest v1 with `run_type=debug`. The first experiment or comparison moves it to `running`. Each comparison becomes a linked artifact. `finalize` links `case.json` and moves the manifest to:

- `passed` for a resolved case;
- `inconclusive` otherwise.

Do not store passwords, tokens, credentials, or secret environment variables in case inputs.

## Failure boundaries

The script manages evidence and comparison; it does not:

- launch a vLLM service;
- synchronize local code to a remote machine;
- collect device logs or stack dumps;
- insert instrumentation into vLLM or vllm-ascend automatically;
- decide task-level accuracy acceptance;
- diagnose eager failures.

Use `remote-code-parity` before remote execution and the appropriate Serving or distributed-debug workflow for runtime control.
