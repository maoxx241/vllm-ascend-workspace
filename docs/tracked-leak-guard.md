# Tracked-file leak checks

Status: current

The [scanner](../.agents/scripts/tracked_leak_scan.py) checks tracked text using
rules from the installed knowledge package and the workspace's reviewed
`.agents/leak-guard/allowlist.yaml`. It detects publication mistakes; it is not
a public-redaction exporter. Knowledge contribution uses a package-prepared
redacted copy.

```text
uv run --no-project python .agents/scripts/tracked_leak_scan.py --format json
uv run --no-project python .agents/scripts/tracked_leak_scan.py --staged
uv run --no-project python .agents/scripts/tracked_leak_scan.py --commit-range origin/main..HEAD
```

Use `--repo-root` to select a checkout. Its own policy is used unless an explicit
allowlist option replaces it. A missing or invalid policy/package is an error.
Progress goes to stderr, one JSON result to stdout; exit codes are 0 for a clean
scan, 1 for findings and 2 for an input/policy error. Default previews mask values.
The local `--show-matches` option is for investigating a finding, not public logs.

## Input boundaries

Gitlinks are skipped, so the scanner does not traverse upstream submodules.
Tracked private runtime roots are rejected before reading file contents or
computing text diffs. This includes force-added empty/binary files and renamed
paths; an exclusion cannot turn them into a clean result. Removing accidentally
tracked runtime state is permitted. Errors name only the fixed private root,
not private filenames or contents.

Ordinary binary and oversized files are counted separately rather than reported
as examined text. Each scanned text line is considered in full. Explicit fixture
exclusions keep the scanner's test corpus out of production scans; the same
content placed elsewhere is still checked.

## Findings and exceptions

Rules cover non-documentation addresses, identity-bearing host/container names,
user paths, email, secret-shaped keys and recognized credential values. Shared
container roots and documented model mounts are not automatically personal
paths. The policy records the applicable roots and narrowly scoped allowances.

Prefer reserved documentation addresses, example domains and placeholders when
an example does not need a real value. If a detector is imprecise, fix its owner
with an appropriate regression test. An allowance is for an intentional,
understood exception, not an alternative to correcting a faulty detector.

The policy contains settings, per-finding allowances and scoped fixture
exclusions. Allowances identify their path/category/value scope and explain why
it is appropriate. Unknown settings, categories or malformed YAML fail clearly.
PyYAML and the redaction rules come from the installed dependency environment;
there is no second local YAML parser or reduced fallback scanner.

Suppressed findings and unused allowance entries remain visible in the result.
`--strict-allowlist` also rejects unused entries. No source comment can silently
suppress a result; the exception stays in the reviewed policy.

## Verification

The local suite uses temporary Git repositories and reserved fixture values to
check category coverage, redacted previews, policy errors, scoped exclusions,
tracked private roots, staged changes and commit ranges:

```text
uv run --no-project python .agents/scripts/local_tests.py .agents/tests/test_tracked_leak_scan.py
```

It does not replace review of the actual public diff or authorize publication
of private evidence.
