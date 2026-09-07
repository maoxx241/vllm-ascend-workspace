# Experiment ledger behavior contract

## Contents

- [Discovery](#discovery)
- [Identity flattening](#identity-flattening)
- [Index output](#index-output)
- [Comparability verdict](#comparability-verdict)
- [Exit codes](#exit-codes)
- [Limits](#limits)

## Discovery

`index` walks `--state-root` (default `.vaws-local`) recursively for files named
`manifest.json` and validates each one against Run Manifest v1 through the
shared `.agents/lib/vaws_run_manifest.py` validator.

Files that fail validation are not skipped. They are listed under `unindexed`
with the reason, because a `manifest.json` that is not a Run Manifest is the
most misleading artifact in the tree: it looks like evidence of a run whose code
state can be recovered, and it is not. The same applies to a duplicate
`run_id`, where the second occurrence is reported rather than overwriting the
first.

Discovery is read-only. Nothing in this Skill writes to a manifest.

## Identity flattening

Four manifest fields make up a run's identity:

| Field | What it pins |
|---|---|
| `workspace_snapshot` | the code state: repository commits, dirty state, native-extension identity |
| `environment` | the runtime: container, image, CANN and torch-npu versions, endpoint |
| `model` | the weights: path, revision, quantization |
| `topology` | the device layout: tensor/data/expert parallel sizes, node and device counts |

Each is flattened into dotted scalar keys, so a nested
`{"topology": {"tensor_parallel_size": 16}}` becomes
`topology.tensor_parallel_size = "16"`. Lists are compared as their sorted JSON
form. `None` compares equal to the empty string, so an explicitly null field and
an absent field are treated alike.

Flattening is what makes `--vary` usable: the caller names the exact leaf
expected to differ rather than a whole subtree.

## Index output

- `run_count`, `runs`: the indexed runs, sorted by `created_at` then `run_id`.
- `by_run_type`, `by_status`: counts, for a quick read of what the tree holds.
- `unindexed`: path plus reason for everything that could not be indexed.
- `runs_without_identity`: runs whose four identity fields are all empty. These
  are indexed and countable, but their results cannot be attributed to a code
  state, which is usually worth fixing before the next run rather than after.

`--run-type` and `--status` filter the `runs` list after indexing, so
`unindexed` and `runs_without_identity` still reflect the whole tree.

## Comparability verdict

`compare` classifies every differing identity key:

- keys named by `--vary` are **intended differences**;
- every other differing key is a **confounder**.

The verdict is `comparable` only when there are no blocking reasons. Blocking
reasons are:

1. either run carries no identity metadata at all;
2. one or more confounders exist;
3. either run has not reached a terminal status;
4. a declared varying key is absent from both runs, which usually means the key
   was misspelled and the real difference is being counted as a confounder or
   missed entirely.

A declared key that exists but happens to be identical is reported under
`declared_but_identical` and does not block. It means the intended change was
not actually applied, which is worth seeing before the numbers are interpreted.

The reason a single confounder is disqualifying rather than advisory: two runs
give one delta. If two things differ, the delta has two candidate causes and no
way to separate them. Holding the confounder constant and re-running costs less
than defending an unattributable result.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | the command answered the question; for `compare`, the verdict is `comparable` |
| `1` | `compare` returned `not-comparable`; the payload lists the blocking reasons |
| `2` | the question could not be answered: missing state root, unknown `run_id`, unreadable input |

The distinction between `1` and `2` matters in a script: `1` is a real answer
that happens to be negative, `2` means nothing was decided.

## Limits

The ledger sees only runs that wrote a conforming Run Manifest v1. Serving and
benchmark state, per-Skill ad-hoc result files, and anything produced outside
the manifest contract are invisible to it.

So an empty result from `index` is not proof that no such experiment ran. When
concluding that work is new, say that the conclusion covers manifest-recorded
runs only.
