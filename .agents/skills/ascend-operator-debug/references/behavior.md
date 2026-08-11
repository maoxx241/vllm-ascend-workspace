# Ascend operator debug behavior contract

## Operator config

An operator case names the exact invocation and trusted reference. It records
fixed absolute and relative tolerances before results are observed.

Each explicit case contains:

- a stable ID;
- execution mode: `eager`, `compile`, or `graph`;
- input names, shapes, strides, dtypes, and layouts;
- scalar and boolean operator attributes.

Prefer explicit cases over an uncontrolled Cartesian product. Add one case when
one dimension answers a concrete hypothesis.

## Result contract

```json
{
  "schema_version": 1,
  "case_id": "fp16-2x4-graph",
  "status": "numerical_mismatch",
  "comparisons": [
    {
      "output": "out",
      "max_abs": 0.02,
      "max_rel": 0.1,
      "cosine": 0.998
    }
  ],
  "timing_us": 41.2,
  "source": "/path/to/raw-result.json"
}
```

Statuses are `passed`, `numerical_mismatch`, `crash`, and `unsupported`.
Crashes and unsupported cases require an error signature. Timing is optional
supporting evidence and is not a model-level regression verdict.

## Analysis semantics

- `diagnosed`: at least one isolated case crashes or mismatches;
- `inconclusive`: planned evidence is missing or a combination is unsupported;
- `passed`: all isolated cases pass.

If every isolated case passes while the source model still fails, the report
routes investigation back to the integration boundary without claiming that the
operator is correct for untested cases.

## Case layout

```text
case/
├── manifest.json
├── operator-config.json
├── case-matrix.json
├── results.json
├── raw-results/
├── input-metadata/
├── artifacts/
├── analysis.json
├── report.md
└── reproduction.md
```
