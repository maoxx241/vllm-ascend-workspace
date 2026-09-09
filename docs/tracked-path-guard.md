# Tracked-path guard

Status: current

A tracked Markdown, YAML, JSON, or TOML file must not name an in-tree path
that does not exist. After the multi-repo split, that is how pre-migration
routing (the former remote-dev `state/` directory, a deleted coordinator README, a moved helper)
survives in the documents agents actually read.

The checker is
[`.agents/scripts/tracked_path_check.py`](../.agents/scripts/tracked_path_check.py).
The policy is
[`.agents/policy/tracked-paths.json`](../.agents/policy/tracked-paths.json).
The dated baseline is
[`.agents/policy/tracked-paths-baseline.json`](../.agents/policy/tracked-paths-baseline.json).

```bash
python3 .agents/scripts/tracked_path_check.py --mode report
python3 .agents/scripts/tracked_path_check.py --mode enforce
```

Exit codes: `0` clean or `--mode report`, `1` policy violated, `2` unusable
policy/baseline/invocation. Progress on `stderr`, one JSON payload on
`stdout`. CI runs enforce immediately after `repo_boundary_check.py`.

## What is scanned

Tracked files from `git ls-files` with suffixes `.md`, `.yaml`, `.yml`,
`.json`, `.toml`. `vllm/` and `vllm-ascend/` are skipped. The guard's own
test file is the only fixture exemption. A short `skip_paths` list names
policy and fixture files whose job is to record departed or external paths
as data.

## Detection

Path-like tokens that start with `.agents/`, `.remote-dev/`, `.claude/`,
`.codex/`, `.cursor/`, `.grok/`, `.trae/`, `.github/`, `docs/`, or
`scripts/` and contain at least one `/`. Tokens are cut at whitespace and
wrappers; trailing punctuation, `:line`, `:line-line`, `#anchor`, and
`.py:attr` suffixes are stripped. A glob is checked by the directory before
the first `*`. A `scripts/<name>.py` token also resolves as
`.agents/scripts/<name>.py` or the referring skill's `scripts/`. Relative
Markdown links such as `](../<file>.md)` are resolved against the referring
file.

`.vaws-local/` and `.vaws-runtime/` are allowed (untracked state).
Placeholders such as `<session-id>` are allowed. The former in-tree
remote-dev `state/` directory is **not** allowed: that path is drift.

Each hit is `{rule, path, token, resolved, lines[]}`. The fingerprint is
`rule|path|token` with no line number. The dated baseline stores that
path as `named_path` so a JSON key named `token` does not trip the
tracked-leak secret-key detector.

## The baseline

Historical rows, if any remain, are attributed `historical-evidence`.
Three anti-rot properties, matching the boundary guard:

1. **Nothing new passes.** A hit absent from the baseline fails enforce.
2. **A fixed hit must delete its row.** A stale baseline row is a hard
   failure, so the baseline can only shrink, and it shrinks in the same
   commit as the fix.
3. **Every row is attributed.** `--write-baseline` preserves `removed_by` /
   `why` and stamps new rows `unassigned`, which enforce also rejects.

`--mode report` always exits 0 and is the honest picture of the current
tree, including accepted rows.
