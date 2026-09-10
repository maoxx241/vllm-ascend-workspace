---
name: vllm-ascend-change-validation
description: Analyze vLLM and vllm-ascend diffs, map affected components to the minimum sufficient correctness, build, performance, graph, operator, distributed, and profiling evidence, link downstream Run Manifest results, and produce a PR-ready validation report. Use for PR validation, workspace-diff risk analysis, deciding what tests a change requires, or documenting untested combinations. Do not use as a replacement for the downstream execution skills or for a change with no accessible diff.
---

# vLLM Ascend Change Validation

Use this Skill as the validation planner and evidence aggregator for a code change.

## Workflow

1. Obtain the exact baseline and candidate diff, including relevant untracked files.
2. Run `scripts/change_validation.py plan` when you want a structured plan. Ordinary experiments can run first; `plan` is optional bookkeeping.
3. Review `impact-analysis.json` and `validation-plan.json` when they exist; correct false-positive or missing mappings before consuming NPU resources.
4. Execute required items with the owning Skill. Existing results may be linked after the fact when their code, environment, inputs, and artifacts support the conclusion. A missing `parent_run_id` is recorded as post-hoc; it is not itself a rejection:
   - correctness evidence: `vllm-ascend-correctness-validation`;
   - eager-pass/graph-fail diagnosis: `vllm-ascend-graph-debug`;
   - service lifecycle: `vllm-ascend-serving`;
   - benchmark and profiling evidence: their dedicated Skills;
   - distributed, operator, or performance workflows when those Skills are available.
5. Link each downstream Run Manifest with the plan item IDs it covers. `link` rejects a `debug` (or otherwise mismatched) `run_type` covering a `correctness:*` or `performance:*` requirement.
6. Run `finalize`.
7. Deliver `pr-validation-report.md` with explicit missing and recommended coverage.

The planner defaults to the versioned rules in `references/validation-rules.yaml`,
so the Skill remains functional when the formal workspace knowledge store is
empty. Use `--knowledge PATH` to supply a reviewed workspace-specific rule set.
Missing matches produce a generic targeted smoke plus manual-review flag; they
never imply that no validation is needed.

## Entry point

`scripts/change_validation.py` provides:

- `plan`: collect or read a unified diff, classify impact, write a validation plan, and create a parent Run Manifest;
- `link`: associate a downstream Run Manifest with one or more plan items;
- `finalize`: assess required coverage and child statuses, then generate the final PR report.

Read:

- [Behavior contract](references/behavior.md) for diff, rule, plan, link, and final-status semantics.
- [Command recipes](references/command-recipes.md) for worktree, commit-range, and imported-diff examples.
- [Acceptance](references/acceptance.md) before claiming the change is validated.

## Rules

- Keep the reason and source rule for every plan item.
- Distinguish `required` and `recommended`; resource constraints do not silently downgrade required evidence.
- Treat child `failed` as failed, child non-terminal/inconclusive or missing required coverage as inconclusive.
- A passed parent requires every required item to be covered by at least one passed child run.
- Link runs whose actual artifacts support the conclusion. A `passed` child still needs artifacts; never treat a passed string as evidence, and never edit a child manifest to make it linkable.
- Record unsupported, unknown, and intentionally omitted combinations under known limitations.
- Do not duplicate Serving, correctness, Benchmark, Profiling, graph-debug, distributed-debug, or operator-debug implementation.
- Keep run state under `.vaws-local/change-validation/`.
