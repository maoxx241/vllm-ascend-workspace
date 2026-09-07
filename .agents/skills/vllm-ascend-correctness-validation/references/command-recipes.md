# Correctness validation command recipes

Run control-plane commands from the workspace root. Run the offline harness only inside a remote Ascend container.

## Initialize

```bash
python -B .agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py init \
  --run-dir .vaws-local/correctness/change-001 \
  --run-id correctness-change-001 \
  --cases /path/to/cases.json \
  --baseline-label baseline \
  --candidate-label candidate \
  --workspace-snapshot '{"workspace":"<snapshot>","dirty":false}' \
  --environment '{"machine":"<alias>","cann":"<version>","torch_npu":"<version>"}' \
  --model '{"path":"<remote-model-path>"}' \
  --topology '{"tp":2,"devices":[0,1]}' \
  --parent-run-id change-validation-001
```

For a deliberate eager-versus-graph comparison, declare the variable under test
so `compare` accepts that one difference and records it with the verdict:

```bash
  --allowed-difference engine_args.enforce_eager
```

Any other difference between the two results' `execution` blocks aborts the
comparison. `--parent-run-id` is required for the run to be linkable by
`change_validation.py link`.

## Execute normalized cases

Remote offline:

```bash
python -B .agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py \
  --config /remote/path/baseline-harness.json \
  --output /remote/path/baseline-result.json
```

Online, after `vllm-ascend-serving` reports healthy:

```bash
python -B .agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py \
  --config /remote/path/candidate-online-harness.json \
  --output /remote/path/candidate-result.json
```

Harness config includes the case array plus:

```json
{
  "schema_version": 1,
  "label": "candidate",
  "model": "/models/example",
  "engine_args": {
    "tensor_parallel_size": 2,
    "enforce_eager": true
  },
  "base_url": "http://127.0.0.1:8000",
  "served_model": "example",
  "cases": []
}
```

The harness copies `model`, `engine_args`, `base_url`, and `served_model` into
the result's `execution` block and adds `cases_sha256`. Use the same case array
verbatim in both harness configs; a different array is an undeclared difference.

## Compare

```bash
python -B .agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py compare \
  --run-dir .vaws-local/correctness/change-001 \
  --baseline /path/to/baseline-result.json \
  --candidate /path/to/candidate-result.json
```

`compare` first checks that the two results are a comparable pair: labels match
the run, both carry `execution`, and every `execution` difference was declared
at `init`. An undeclared difference exits 1 naming the keys and writes no
comparison. Read the compact stdout first (it lists `observed_differences`),
then inspect `report.md` and only the relevant raw evidence.
