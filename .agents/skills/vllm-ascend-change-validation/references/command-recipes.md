# Agent call

From the repository root:

```text
python .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py --baseline BASE --candidate HEAD --repo-root source --evidence correctness/manifest.json performance/manifest.json
```

Use --diff-file for an already captured diff. The report classifies affected components, derives supported coverage from actual evidence and exact code identities, and lists missing checks. Agents do not enter coverage labels or lifecycle records.

Use `--help` for exact argument details. Report output directories are optional
where supported; the script creates a fresh directory under `.vaws-local/`.
Schema versions and report identifiers are generated internally. Input files
describe business cases or contain observed results, rather than task ownership.
