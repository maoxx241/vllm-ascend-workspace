# Change validation behavior contract

## Contents

- [Diff input](#diff-input)
- [Knowledge routing](#knowledge-routing)
- [Plan items](#plan-items)
- [Linked evidence](#linked-evidence)
- [Final status](#final-status)
- [Artifacts](#artifacts)

## Diff input

`plan` accepts either:

- `--repo-root` with a baseline and candidate ref;
- `--repo-root` with candidate `WORKTREE`, including non-ignored untracked text files;
- `--diff-file` containing a unified diff assembled across one or more repositories.

The persisted `diff-summary.json` stores file paths, line counts, and a SHA256 digest, not the full source diff.

For cross-repository changes, create one combined unified diff and list each target with `--target-repository`.

## Knowledge routing

Default rules live in `references/validation-rules.yaml`. The `--knowledge`
option may replace them with a reviewed workspace-specific rule set. A rule
contains:

- `path_patterns`: regular expressions matched only against changed paths;
- optional `content_patterns`: regular expressions matched against added and removed lines when symbol-level routing is necessary;
- `category`: affected component;
- `required_checks`;
- optional `recommended_checks`;
- optional route conditions such as `route_on_hang`.

Every plan item records the rule IDs and categories that caused it. Multiple rules producing the same check merge into one item, and required wins over recommended.

If no rule matches, emit required `correctness:targeted-smoke` and set `manual_review_required=true`.

## Plan items

Each item has:

- stable `id`;
- human-readable `check`;
- `required` or `recommended` priority;
- source rules;
- rationale categories;
- coverage status.

Review the plan before running expensive hardware validation. Correcting a false mapping is allowed; silently deleting required evidence is not.

## Linked evidence

Link the downstream `manifest.json` and one or more plan IDs. The parent records:

- child run ID and type;
- current child status;
- absolute manifest location;
- covered plan items.

A child with another non-null parent ID cannot be linked. Duplicate child run IDs are rejected.

## Final status

- `passed`: every required item has at least one linked passed run, every linked run is passed, and no manual review remains;
- `failed`: any linked child run is failed;
- `inconclusive`: required coverage is missing, manual review remains, or any child is planned, running, inconclusive, or cancelled.

Recommended evidence may remain missing without changing a fully proved required plan from passed, but the report keeps it visible.

## Artifacts

```text
change-validation/
├── manifest.json
├── run.json
├── diff-summary.json
├── impact-analysis.json
├── validation-plan.json
├── linked-runs.json
└── pr-validation-report.md
```
