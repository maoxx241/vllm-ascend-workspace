---
name: vllm-ascend-change-validation
description: Analyze vLLM and vllm-ascend diffs, map affected components to the minimum sufficient correctness, build, performance, graph, operator, distributed, and profiling evidence, link downstream Run Manifest results, and produce a PR-ready validation report. Use for PR validation, workspace-diff risk analysis, deciding what tests a change requires, or documenting untested combinations. Do not use as a replacement for the downstream execution skills or for a change with no accessible diff.
---

# vllm-ascend-change-validation

Map an accessible diff to the minimum evidence needed for a reviewable validation conclusion.

Read the changed behavior and affected callers before choosing tests. Build, numerical, graph, distributed and performance evidence cover different failure modes. Existing evidence is reusable when its observed code states and scope match the diff.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py --baseline BASE --candidate HEAD --repo-root source --evidence correctness/manifest.json performance/manifest.json
```

Use --diff-file for an already captured diff. The report classifies affected components, derives supported coverage from actual evidence and exact code identities, and lists missing checks. Agents do not enter coverage labels or lifecycle records.

Execute missing checks with the owning validation, benchmark, profiling or debug skill; this report does not run an NPU experiment.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
