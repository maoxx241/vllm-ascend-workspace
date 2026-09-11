---
name: vllm-ascend-change-validation
description: Consolidate executed vLLM or vllm-ascend change validation into a report tied to an accessible diff and Run Manifest evidence. Use when asked to validate a change experimentally or produce a formal validation report. Ordinary PR reading, code review, diff explanation, and test suggestions use native code and Git tools.
---

# vllm-ascend-change-validation

Map an accessible diff to the minimum evidence needed for a reviewable validation conclusion.

Use this workflow when the requested result needs experiment evidence or a
formal validation report. A request to read or review a PR, explain a change,
or suggest tests can be completed directly without this workflow. A report
alone requires no runtime, task allocation or new experiment.

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
