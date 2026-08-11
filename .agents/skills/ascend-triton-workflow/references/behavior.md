# Ascend Triton workflow behavior contract

## Contents

- Workflow configuration
- Stage semantics
- Child evidence
- Final status
- Artifact layout

## Workflow configuration

```json
{
  "schema_version": 1,
  "run_id": "triton-softmax-001",
  "op_name": "softmax",
  "source": {
    "kind": "gpu-triton",
    "path": "/workspace/softmax_gpu.py",
    "sha256": "optional-source-hash"
  },
  "target": {
    "soc": "Ascend910B2",
    "cann": "target-version",
    "triton_ascend": "target-version"
  },
  "required_stages": ["development", "validation", "optimization"],
  "workspace_snapshot": {},
  "environment": {},
  "command": []
}
```

`source.kind` is `torch-reference`, `gpu-triton`, or `ascend-triton`. Paths are
recorded as evidence; the controller does not copy source files.

## Stage semantics

| Stage | Owner | Child run type | Meaning of passed |
|---|---|---|---|
| `development` | `ascend-triton-operator-development` | `debug` | A candidate exists and its linked validation passed |
| `validation` | `ascend-triton-kernel-validation` | `correctness` | Static gate and every planned case passed |
| `optimization` | `ascend-triton-kernel-optimization` | `performance` | The configured performance objective was met |

Optimization depends on validation. Development is required for a new or
migrated kernel but may be omitted when the source is already an Ascend Triton
candidate.

## Child evidence

Each child manifest must:

- use Run Manifest v1;
- have `parent_run_id` equal to the workflow run ID;
- have the expected run type for its stage;
- be terminal before linking;
- be linked once to one stage.

The link records the child run ID, status, run type, manifest path, and artifact
list. A later child does not overwrite earlier evidence; start a new workflow to
replace a stage result.

## Final status

- `passed`: every required stage has a passed child;
- `failed`: at least one required child is failed;
- `inconclusive`: required evidence is missing, cancelled, or inconclusive.

An optional failed stage is reported but does not change a passed required set.

## Artifact layout

```text
workflow/
├── manifest.json
├── workflow-config.json
├── stage-plan.json
├── evidence-links.json
├── workflow-summary.json
└── workflow-report.md
```
