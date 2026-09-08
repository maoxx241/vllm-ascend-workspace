# Correctness validation behavior contract

## Contents

- [Case document](#case-document)
- [Normalized result](#normalized-result)
- [Comparison precedence](#comparison-precedence)
- [Run artifacts](#run-artifacts)
- [Routing](#routing)

## Case document

Use one JSON document for both baseline and candidate:

```json
{
  "schema_version": 1,
  "cases": [
    {
      "id": "chat-smoke",
      "mode": "online-chat",
      "repeats": 2,
      "sampling": {
        "temperature": 0,
        "seed": 7,
        "max_tokens": 32
      },
      "request": {
        "messages": [
          {
            "role": "user",
            "content": "Return the word ready."
          }
        ]
      },
      "comparison": {
        "atol": 0.00001,
        "rtol": 0.0001,
        "metric_rules": {}
      },
      "matrix": {
        "execution_mode": "eager",
        "tp": 2,
        "features": []
      }
    }
  ]
}
```

Valid modes are `offline-generate`, `offline-chat`, `online-chat`, and `aisbench`. Non-AISBench exact-comparison cases require `temperature=0` and an explicit seed.

## Normalized result

Each state writes:

```json
{
  "schema_version": 1,
  "label": "baseline",
  "execution": {
    "model": "/models/example",
    "engine_args": {
      "tensor_parallel_size": 2,
      "enforce_eager": true
    },
    "base_url": null,
    "served_model": null
  },
  "cases": [
    {
      "id": "chat-smoke",
      "status": "ok",
      "outputs": [
        {
          "text": "ready",
          "token_ids": [1234],
          "tokens": ["ready"],
          "numerics": {
            "logprobs": [-0.01]
          }
        }
      ],
      "metrics": {
        "accuracy": 0.75
      }
    }
  ]
}
```

`status` is `ok`, `error`, or `unsupported`. An executor may provide text, token IDs, token strings, numeric evidence, task metrics, or a relevant subset.

`execution` records what decided the run's behaviour besides the code under test. The harness fills it from its config (`engine_args`, `model`, `base_url`, `served_model`). Case files live in git; `code.snapshot_commit` already covers them. The AISBench adapter requires an execution block via `normalize --execution`. It is a declaration of the launch configuration, not an observation of the running service.

## Execution identity check

`compare` refuses to classify anything until the two results are shown to be a comparable pair:

- each result's `label` must equal the `baseline_label` / `candidate_label` recorded at `init` (passing one file twice is rejected);
- both results must carry an `execution` block with an `engine_args` object;
- every key where the two `execution` blocks differ (`engine_args.*` flattened, plus `model`, `base_url`, `served_model`) must have been declared at `init` with `--allowed-difference KEY`.

An undeclared difference aborts the comparison with the list of differing keys and leaves the manifest non-terminal. This is what stops a deliberate eager-versus-graph comparison from being reported as a code regression: with `--allowed-difference engine_args.enforce_eager` the comparison runs, and the report and `execution.json` state that the divergence is attributable to that declared variable. Without the declaration the comparison is not a code comparison and is not performed.

The check compares declared launch configuration. It cannot see differences that were never written into the harness config (for example an online service restarted with other flags but the same `base_url`).

## Observational comparability certificate

`compare` then issues an observational certificate from `.agents/lib/vaws_comparability.py` before it classifies anything. Each identity leaf is labelled `observed`, `declared`, or `unknown`:

- the Run Manifest identity written at `init` is **declared**;
- offline `execution.engine_args` and `execution.model` are **observed** (they were passed to `LLM`); online those two are **declared** (never sent to the service);
- `base_url` and `served_model` are **observed**;
- an optional result `observation` object is **observed** and is required for `workspace_snapshot`, `environment`, `model`, `topology`, and `native_digest`.

`consume_certificate` recomputes the verdict from the identity body, including each side's declaration/observation mismatches. Empty identity groups, and null or whitespace-only identity scalars, are recorded as `unknown` and block `comparable`, so they block `passed`. They are not rejected at `init`. Two `{"text": ""}` outputs are `infrastructure_failure` (`empty-output-is-not-agreement`), not `exact_match`.

See `docs/comparability-certificate.md`. The audit §8.2 hardware matrix is encoded as unit tests and has not been run on NPU.

## Comparison precedence

Primary classification precedence is:

1. missing or errored result → `infrastructure_failure`;
2. unsupported state → `unsupported_combination`;
3. repeats disagree → `flaky_or_nondeterministic`;
4. configured metric exceeds its regression threshold → `task_metric_regression`;
5. comparable token IDs, tokens, or text disagree → `token_divergence`;
6. numeric evidence exceeds tolerance → `numerical_regression`;
7. numeric evidence differs within tolerance → `numerical_difference_within_tolerance`;
8. matching empty text or empty token lists → `infrastructure_failure` (`empty-output-is-not-agreement`);
9. otherwise → `exact_match`.

Run status:

- `passed`: every case is exact or within tolerance;
- `failed`: at least one correctness or task-metric regression and no inconclusive case;
- `inconclusive`: any infrastructure, unsupported, or flaky case.

## Run artifacts

```text
correctness-run/
├── manifest.json
├── run.json
├── cases.json
├── environment.json
├── raw_outputs/
│   ├── baseline.json
│   └── candidate.json
├── execution.json
├── comparability-certificate.json
├── comparison.json
├── report.md
└── reproduction.sh
```

The normalized files are the comparison source. Mixed service or vLLM stdout is supporting evidence, not a parser contract. `execution.json` holds both sides' execution identity, the declared allowed differences, and the observed differences; the manifest links it and the raw outputs with SHA256, and `comparison.json` embeds the same block under `execution`.

## Routing

- eager passes, graph fails: route to `vllm-ascend-graph-debug`;
- multi-rank hang or metadata mismatch: route to distributed debug;
- output is correct but slower: route to performance regression;
- executor cannot start or reach a service: repair infrastructure and rerun the identical case;
- missing compatibility fact: report unsupported or unknown, never pass.
