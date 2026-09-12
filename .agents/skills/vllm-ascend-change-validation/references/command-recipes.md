# Agent call

From the repository root:

```text
uv run --no-project python .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py --baseline BASE --candidate HEAD --repo-root source --evidence correctness/manifest.json performance/manifest.json
```

Use `--diff-file` for an existing diff. The report preserves supplied run outcomes,
artifact availability and observed source matches. The Agent assesses whether
that evidence covers the changed behavior; no automatic required test plan is generated.

Use `--help` for argument details. Reports create their own identifiers and
output directories; reuse existing observed inputs rather than creating task records.
