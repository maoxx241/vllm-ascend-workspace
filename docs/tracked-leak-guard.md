# Tracked-file leak guard

Status: current

`AGENTS.md` says: *never write secrets, passwords, or tokens into tracked
files.* Nothing enforced that rule, and an audit of `main` found an internal
address range, a personal laptop path, and a personal remote path already
committed to a **public** repository. This package is the enforcement layer.

Three surfaces run the same detector against the same installed
`vaws-knowledge` redaction rules, so they cannot disagree. A checkout
that has not run `python .agents/scripts/vaws_deps.py sync` refuses to scan; it does not run with fewer
rules.

| Surface | Entry point | When |
|---------|-------------|------|
| Scanner | `.agents/scripts/tracked_leak_scan.py` | on demand |
| Pre-commit hook | `.agents/hooks/tracked_leak_precommit.py` | every `git commit` |
| CI | `.github/workflows/skill-catalog.yml` | every pull request |

Detection lives in `.agents/lib/vaws_leak_guard.py`, which extends the
secret-shaped patterns already used for knowledge documents
(`vaws_knowledge.SECRET_KEY_RE`, `SECRET_VALUE_RES`) with the categories they
never covered.

## Install the hook

```bash
python3 .agents/hooks/tracked_leak_precommit.py --install
python3 .agents/hooks/tracked_leak_precommit.py --status
```

The installer writes a `pre-commit` shim into the repository's shared hooks
directory (`git rev-parse --git-common-dir`, or `core.hooksPath` when set), so
every linked worktree is covered by one install. A pre-existing foreign hook is
never overwritten silently: the installer refuses and tells you to rerun with
`--force`, which backs the old hook up next to it.

The hook fails closed. A finding, a broken policy file, a missing policy file,
an unreadable staged diff, or a missing `vaws-knowledge` package all block
the commit. The remedy for the package gap is `python .agents/scripts/vaws_deps.py sync` (or
`uv run python3 .agents/scripts/tracked_leak_scan.py`, which syncs first).
`git commit --no-verify` still bypasses the hook, which is why CI runs the
same scanner after `python .agents/scripts/vaws_deps.py sync --locked`.

## Run the scanner

```bash
# whole tracked tree (what CI runs)
python3 .agents/scripts/tracked_leak_scan.py --format json

# only what you are about to commit
python3 .agents/scripts/tracked_leak_scan.py --staged --show-matches

# only what a branch adds
python3 .agents/scripts/tracked_leak_scan.py --commit-range origin/main..HEAD
```

`--repo-root` selects the tree to scan. Unless `--allowlist` or `--no-allowlist`
is given, the policy is `<repo-root>/.agents/leak-guard/allowlist.yaml`. A
missing policy is an error; the scanner does not fall back to another
worktree's file.

Progress goes to `stderr`; a single JSON payload goes to `stdout`. Exit code is
`0` for a clean scan, `1` for findings, `2` for a policy or git error.

Previews are redacted by default (`192****** [13 chars]`) so CI logs of a public
repository do not republish the value they are complaining about. Use
`--show-matches` locally when you need the literal.

## What is scanned

- Tracked blobs only, from `git ls-files -s`. Gitlinks (mode `160000`) are
  dropped, so `vllm/` and `vllm-ascend/` submodule content is never read: it is
  upstream, volatile, and not ours to police.
- `.vaws-local/`, `.vaws-runtime/`, and `.vaws-local/remote-dev-state/` are excluded by
  policy as well as by `.gitignore`, so a stray `git add -f` cannot smuggle
  runtime state past the guard silently — it is reported as skipped, with a
  count in the JSON payload.
- Each text line is scanned in full. File size is still bounded by
  `max_file_bytes` (2 MiB default): binary files and files above that size are
  skipped and counted, never silently ignored.

## Categories

| Category | What trips it |
|----------|---------------|
| `ipv4`, `ipv6` | any address outside loopback and the RFC 5737 / RFC 3849 documentation ranges, **including RFC 1918** |
| `mac-address` | colon or hyphen MAC, except the RFC 7042 documentation range |
| `absolute-user-path` | `/Users/<name>/…`, `/home/<name>/…`, named paths under `/root/` |
| `email` | any mailbox outside the reserved example domains |
| `internal-hostname` | `.local`, `.lan`, `.internal`, `.intra`, `.corp`, … names in a quoted, URL, or host position |
| `internal-identifier` | employee-id-shaped tokens (short letter prefix, long numeric tail) |
| `container-name` | container names embedding an identity token or an address-derived suffix |
| `secret-key` | secret-shaped key with a literal value, classified by `SECRET_KEY_RE` |
| `secret-value` | known credential formats (`sk-…`, `gh?_…`, `AKIA…`, JWT, `Bearer …`, PEM private keys) |

False positives are expected and acceptable. Silent misses are not: when a rule
is imprecise, the fix is an allowlist entry, not a looser pattern.

## Decision: remote container paths are not leaks

Ascend work legitimately talks about remote paths. The guard treats them as
follows, and all of it is policy in `.agents/leak-guard/allowlist.yaml` rather
than code:

- **`/vllm-workspace` and other shared container roots: not leaks, not
  scanned.** They are identical on every managed container, `AGENTS.md`
  documents `/vllm-workspace` as the default remote `cwd`, and they name no
  person, host, or organization. `settings.scanned_absolute_path_roots` lists
  the roots the path rule walks (`Users`, `home`, `root`); a maintainer who
  wants container paths flagged adds `vllm-workspace` there.
- **`/home/weights/<model>` and `/root/<model>`: not leaks, but still
  scanned.** A shared weight mount is indistinguishable from a person's home
  directory by shape, so the rule keeps looking at `/home/...` and the specific
  mounts are allowed by `allowed_absolute_path_prefixes` plus a model-name
  segment pattern. `/home/<person>` therefore still fails closed, which is the
  case that actually leaked.
- **`/mnt/...` is not scanned as a home root**, but an identity token inside
  such a path is still caught by `internal-identifier` — that is how a
  previously unreported employee id in a shared-storage path was found.

Getting this wrong in either direction makes the tool useless or ignored, so it
is written down here rather than implied by a regex.

## Allowlist design

One reviewable file, `.agents/leak-guard/allowlist.yaml`, with three sections:

- `settings` — allowed ranges, domains, path prefixes, scanned roots, extra
  name rules. Every allowance that takes a value also takes a `justification`.
- `allowlist` — per-finding allowances scoped by `path_glob`, `categories`, and
  either `match` (exact text) or `match_regex`. `justification` is mandatory
  and at least 20 characters. `remediation` is optional and records what should
  eventually happen to a baseline entry.
- `scoped_exclusions` — whole-file exclusions. Only the guard's own test corpus
  uses one; everything else must be a narrow entry.

Deliberate design choices:

- **No inline pragmas.** A `# noqa`-style comment would spread through the tree
  and never be reviewed again. Every suppression is one diff hunk in one file.
- **Fails closed on anything unclear.** Unknown keys, unknown categories,
  duplicate ids, short justifications, and a wrong `schema_version` are errors,
  not warnings.
- **Suppressions stay visible.** The JSON payload reports `suppressed_count`
  and every suppressed finding, plus `unused_allowlist_entries` so stale
  allowances can be pruned (`--strict-allowlist` turns that into a failure).
- **The policy file may quote the values it allows.** A finding inside
  `allowlist.yaml` whose text is declared by one of its own entries is
  attributed to `policy-self-reference`; nothing else in that file is exempt.
- **PyYAML is optional; `vaws-knowledge` is not.** The policy is read with
  PyYAML when importable and with a small built-in parser otherwise. The
  redaction rules come from the installed package. A missing package is a
  refused scan (`python .agents/scripts/vaws_deps.py sync`), never a reduced rule set that reports `passed`.

## Adding an entry

```yaml
  - id: my-doc-example-host
    path_glob: docs/**
    categories: [ipv4]
    match: 192.0.2.10
    justification: Reserved RFC 5737 documentation address used in an example command.
```

Prefer fixing the value first: RFC 5737 (`192.0.2.x`), RFC 3849
(`2001:db8::`), `example.invalid`, or a `<placeholder>` almost always says the
same thing without naming anything real.

## Tests

```bash
python3 -m unittest discover -s .agents/tests -p "test_tracked_leak_scan.py"
```

The fixtures in `.agents/tests/fixtures/tracked_leak_guard/` deliberately
contain one bad value per category, using only reserved values (RFC 1918,
RFC 4193, `.invalid`). They are kept out of the tracked-tree scan by the single
`scoped_exclusions` entry — not by weakening any pattern — and the tests assert
both halves of that: the fixture content is reported when it appears anywhere
else, and not reported at its fixture path.
