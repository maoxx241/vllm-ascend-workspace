---
name: vllm-ascend-change-validation
description: Consolidate executed vLLM or vllm-ascend change validation into a report tied to an accessible diff and Run Manifest evidence. Use when asked to validate a change experimentally or produce a formal validation report. Ordinary PR reading, code review, diff explanation, and test suggestions use native code and Git tools.
---

# vllm-ascend-change-validation

Assess the changed behavior and affected callers, then summarize the evidence
needed for a reviewable validation conclusion.

Use this workflow when the requested result needs experiment evidence or a
formal validation report. A request to read or review a PR, explain a change,
or suggest tests can be completed directly without this workflow. A report
alone requires no runtime, task allocation or new experiment.

Read the changed behavior and affected callers before choosing tests. Build, numerical, graph, distributed and performance evidence cover different failure modes. Existing evidence is reusable when its observed code states and scope match the diff.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py --baseline BASE --candidate HEAD --repo-root source --evidence correctness/manifest.json performance/manifest.json
```

Use `--diff-file` for an already captured diff. The report summarizes changed
files and supplied manifests, checks artifact availability and reuses existing
comparability evidence to identify observed source revisions. It creates no test
plan from path keywords and no fallback NPU smoke requirement for unclassified
changes.

Individual run outcomes and source matches remain visible. The aggregate stays
inconclusive about complete change validation: the Agent decides whether the
evidence covers the actual changed behavior. Missing or unrelated evidence is
reported as a limitation, without discarding usable artifacts or requiring a
new parent task association.

Execute missing checks with the owning validation, benchmark, profiling or debug skill; this report does not run an NPU experiment.

Read the relevant detail only when needed:
