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

## Comparison precedence

Primary classification precedence is:

1. missing or errored result → `infrastructure_failure`;
2. unsupported state → `unsupported_combination`;
3. repeats disagree → `flaky_or_nondeterministic`;
4. configured metric exceeds its regression threshold → `task_metric_regression`;
5. comparable token IDs, tokens, or text disagree → `token_divergence`;
6. numeric evidence exceeds tolerance → `numerical_regression`;
7. numeric evidence differs within tolerance → `numerical_difference_within_tolerance`;
8. otherwise → `exact_match`.

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
├── comparison.json
├── report.md
└── reproduction.sh
```

The normalized files are the comparison source. Mixed service or vLLM stdout is supporting evidence, not a parser contract.

## Routing

- eager passes, graph fails: route to `vllm-ascend-graph-debug`;
- multi-rank hang or metadata mismatch: route to distributed debug;
- output is correct but slower: route to performance regression;
- executor cannot start or reach a service: repair infrastructure and rerun the identical case;
- missing compatibility fact: report unsupported or unknown, never pass.
