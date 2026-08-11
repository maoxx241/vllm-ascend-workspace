---
name: vllm-ascend-correctness-validation
description: Plan, execute, normalize, and compare vLLM Ascend inference correctness across baseline and candidate code states, eager and graph modes, offline generate or chat, online chat completions, and AISBench task metrics. Use for accuracy validation, token-output comparison, graph-versus-eager checks, deterministic regression testing, or failure classification. Do not use to root-cause an already reproduced graph-only or isolated-operator failure, or for throughput benchmarking, HBM attribution, or profiling-only analysis.
---

# vLLM Ascend Correctness Validation

Produce traceable correctness evidence instead of treating a successful request as proof of correctness.

## Workflow

1. Define cases and the smallest affected validation matrix.
2. Create independent baseline and candidate code states or eager and graph states.
3. Before remote execution, use `remote-code-parity` for each state.
4. For offline cases, run `scripts/remote_correctness_harness.py` inside the remote NPU container. The harness prioritizes the materialized `vllm/` and `vllm-ascend/` source roots and propagates them through `PYTHONPATH`, so launching from `/vllm-workspace` or spawning a `python -m` child cannot resolve the outer repository directory as a false `vllm` namespace package.
5. For online cases, use `vllm-ascend-serving`, then run the same harness in `online-chat` mode.
6. Use `scripts/correctness_run.py init` to create the run directory and Run Manifest v1.
7. Use `scripts/correctness_run.py compare` to normalize the evidence into a classification and report.
8. Route failures:
   - eager pass and graph fail: `vllm-ascend-graph-debug`;
   - multi-rank hang or inconsistent rank metadata: `vllm-ascend-distributed-debug` when available;
   - performance-only change: `vllm-ascend-performance-regression` when available;
   - task metric execution: use the bundled AISBench adapter.

Do not run the full execution-mode, parallelism, and feature Cartesian product. Select cases from the code impact and `.agents/knowledge/validation-rules.yaml`; record omitted combinations as risks.

## Determinism

For exact token or text comparison:

- set `temperature=0`;
- set an explicit seed;
- keep prompts, messages, chat template, max tokens, model weights, tokenizer, parallel topology, and feature flags identical;
- repeat each case at least twice when nondeterminism is suspected;
- never compare baseline and candidate results produced from different case files.

Use task-level metrics when exact output is not an appropriate acceptance condition. Use numeric tolerances only for explicitly captured logits, hidden states, KV samples, or other numeric evidence.

## Result classes

The comparator emits exactly one primary class per case:

- `exact_match`
- `token_divergence`
- `numerical_difference_within_tolerance`
- `numerical_regression`
- `task_metric_regression`
- `flaky_or_nondeterministic`
- `infrastructure_failure`
- `unsupported_combination`

Do not relabel infrastructure failures as correctness regressions. Do not treat unsupported or untested combinations as passing.

## Structured entry points

- `scripts/correctness_run.py`: initialize a run, compare normalized baseline and candidate outputs, write `comparison.json`, `report.md`, `reproduction.sh`, and update Run Manifest v1.
- `scripts/remote_correctness_harness.py`: execute offline generate, offline chat, or online chat cases and write the normalized result contract.
- `scripts/aisbench_adapter.py`: prepare an AISBench accuracy command/config and normalize task metrics into the correctness result contract.

Read as needed:

- [Behavior contract](references/behavior.md) for case, result, comparison, and artifact schemas.
- [Command recipes](references/command-recipes.md) for local control-plane and remote harness examples.
- [AISBench adapter](references/aisbench.md) before preparing or importing AISBench results.
- [Acceptance](references/acceptance.md) before marking a run passed.

## Boundaries

- Keep runtime state under `.vaws-local/correctness/`.
- Keep progress on stderr and the final machine-readable payload on stdout.
- Never put passwords, tokens, API keys, or credentials in configs or manifests.
- Run torch and torch_npu code only in the remote Ascend container.
- Treat the normalized output file, not mixed runtime stdout, as the comparison source.
