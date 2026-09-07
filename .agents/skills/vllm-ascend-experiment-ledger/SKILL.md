---
name: vllm-ascend-experiment-ledger
description: Index every Run Manifest v1 run across the workspace, show which code state and topology produced a given result, and decide whether two runs differ only in the variable under test. Use when several experiments have accumulated, when a number cannot be traced to the code that produced it, when deciding whether an A/B pair is a fair comparison, or before repeating a diagnosis that may already have been done. Do not use to execute runs, to judge whether a metric regressed, or as a substitute for the owning execution Skill's own state.
---

# vLLM Ascend Experiment Ledger

Answer three questions about work that has already run: what runs exist, what
produced each result, and whether two results may be compared.

Each execution Skill writes its own Run Manifest v1 under its own
`.vaws-local/<skill>/` tree. That is the right place for it, and this Skill does
not move it. What is missing is the view across those trees, and the two
failures that view prevents: attributing a delta to a change that was not the
only thing that differed, and re-running a diagnosis whose result is already on
disk under a directory name nobody remembers.

## Use this Skill when

- more than a couple of runs have accumulated and you need to know what is there
- a number needs to be traced back to the exact code state that produced it
- you are about to compare a baseline against a candidate and need to know the
  pair is fair
- you are about to repeat an experiment and want to check it has not already run

## Do not use this Skill when

- the task is to execute a run; use the owning execution Skill
- the question is whether a metric regressed; that is
  `vllm-ascend-performance-regression`, which owns thresholds and variance
- the question is what evidence a change requires; that is
  `vllm-ascend-change-validation`
- the goal is durable project knowledge rather than run bookkeeping; capture a
  candidate with `.agents/scripts/knowledge_capture.py`

## Workflow

1. Run `scripts/experiment_ledger.py index` and read `run_count`,
   `runs_without_identity`, and `unindexed`.
2. Treat every entry in `unindexed` and `runs_without_identity` as a run whose
   results cannot be attributed later. Say so when reporting; do not quietly
   drop them.
3. Before starting a new experiment, look for an existing run with the same
   identity. A matching terminal run is an answer you already have.
4. Before comparing two runs, run `compare` with the key you intended to vary.
   A `not-comparable` verdict means the delta is not attributable, whatever the
   numbers say.
5. When creating a new run, populate the manifest identity fields at creation
   time through `.agents/scripts/run_manifest.py init`. Identity recorded after
   the workload has run proves nothing about what the workload used.

## Entry point

`scripts/experiment_ledger.py` provides:

- `index`: discover every Run Manifest v1 under the state root, summarize each
  run, and list what could not be indexed;
- `show`: print one run with its full flattened identity;
- `compare`: check whether two runs differ only in the declared varying keys,
  exiting non-zero on `not-comparable`.

Comparability is decided over the manifest identity fields:
`workspace_snapshot`, `environment`, `model`, and `topology`. These are
flattened to dotted scalar keys, so `--vary` takes keys such as
`workspace_snapshot.vllm_ascend_commit` or `topology.tensor_parallel_size`.

Read:

- [Behavior contract](references/behavior.md) for discovery, identity flattening, and verdict semantics.
- [Command recipes](references/command-recipes.md) for indexing, tracing a result, and gating an A/B pair.
- [Acceptance](references/acceptance.md) before presenting a cross-run comparison.

## Rules

- Record identity when the run is created, not when it is reported. A manifest
  whose identity was filled in afterwards describes what you believe ran.
- One undeclared difference is enough to make a comparison unattributable. Two
  runs cannot separate the effect of the intended change from the effect of a
  confounder, so `not-comparable` is a hard verdict rather than a caution.
- A non-terminal run is not a result. Do not compare against `planned` or
  `running`.
- A run that cannot be indexed is worse than a missing run, because it looks
  like evidence. Report the reason rather than the count alone.
- This Skill reads manifests and never edits them. Fixing a manifest's identity
  belongs to the Skill that owns the run.
- Absence of a matching prior run is not proof that the work is new; the ledger
  only sees runs that wrote a conforming manifest. State that limit when
  concluding an experiment has not been done.
- Keep ledger output under `.vaws-local/experiment-ledger/`.
