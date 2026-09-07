# Property-based tests for the deterministic cores

The scaffold's target state collapses deterministic mechanics into a small CLI
surface that is matured through heavy testing. The `test_property_*.py` files
under `.remote-dev/tests/` and `.agents/tests/` are the offline half of that
testing: they state a property, generate inputs against it, and record every
defect they found as an expected failure (nothing is fixed by these suites —
fixes are routed separately, see below).

## Running

```bash
python3 -m unittest discover -s .remote-dev/tests
python3 -m unittest discover -s .agents/tests
```

Both trees are discovered independently; the generator support module
(`test_property_support.py`) is therefore duplicated in both trees on purpose
(`.remote-dev` is being extracted into its own repository). Keep the two copies
in sync.

`hypothesis` is not a dependency. The support module provides a small seeded
generator (`Gen`) and a case runner (`run_cases`). Every case derives its own
seed from `(suite seed, case index)`; a failure message always contains both:

```
codex executor all-or-nothing: case #124/400 failed
(VAWS_PROPTEST_SEED=2026, case_seed=16592026355301341376): ...
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `VAWS_PROPTEST_SEED` | `20260907` | Suite seed; change it to explore new inputs, set it to a reported value to replay a failure. |
| `VAWS_PROPTEST_SCALE` | `1` | Multiplier on every case count (`3` = three times as many cases). |

Case counts are bounded so the default run adds roughly 4–5 s per tree. The
suites were swept over 20+ seeds at `VAWS_PROPTEST_SCALE=3` before landing.

Remote-side executor scripts (`REMOTE_FILE_PY`, `REMOTE_CODEX_PATCH_PY`,
`REMOTE_MANIFEST_PY`) are executed in-process on temporary trees via
`run_remote_script`; the unified-diff path runs the real `bash` + `git apply`
script through a fake transport. Nothing needs a reachable host.

## Properties proved per core

| Core | Property | File |
|------|----------|------|
| path policy | accept/reject decision equals an independent lexical reference; accepted paths are absolute, normalized, `..`-free and under root; idempotent; sibling-prefix roots never confused; degenerate roots rejected; NFC/NFD forms do not alias | `.remote-dev/tests/test_property_path_policy.py` |
| path policy (remote layer) | on a tree with file/dir/dangling symlinks pointing outside root, no write/edit/multi_edit/patch op ever changes anything outside root, and reads never reveal outside content | same |
| permissions | secret shapes detected anywhere, case-insensitively; transports detected at word boundaries | `.remote-dev/tests/test_property_permissions.py` |
| endpoint | well-typed payloads always yield well-formed endpoints; garbage payloads raise only `EndpointError`; identity depends only on user/host/port/root; alias merge and managed mapping are deterministic | `.remote-dev/tests/test_property_endpoint.py` |
| patch application | `parse(render(ops)) == ops`; malformed patches rejected; an applied Codex patch equals a reference model of the tree; one failing op at any position leaves the tree byte-identical; commit failure rolls files back; unified diffs apply fully or not at all | `.remote-dev/tests/test_property_patch_ops.py` |
| read ledger | random read/external-change/write interleavings are judged exactly by a "last observed sha per (context, file)" model; reading another file or another context never refreshes the guard; ledgers round-trip and never leak across endpoints; scopes are single safe path segments | `.remote-dev/tests/test_property_read_ledger.py` |
| previews | truncated previews are exact prefix + suffix, never overlap, never split code points, and report true byte counts at limit, limit±1, empty and multi-byte input; `compact_text` is bounded; read pagination equals a reference slice with correct `partial`/truncation flags | `.remote-dev/tests/test_property_preview.py` |
| artifact manifests | remote and local manifests agree with a reference; every single-byte flip, truncation, extension, add, remove and rename changes the manifest; symlinks block; pulls never land bytes that disagree with the manifest and leave no `.tmp`; pushes report exactly the local manifest and stop on mismatch | `.remote-dev/tests/test_property_artifacts.py`, `.agents/tests/test_property_artifact_toolbox.py` |
| Run Manifest v1 | generated manifests validate, are never mutated, round-trip through write/load; every single-field corruption and every missing/unknown field is rejected with a message naming the field; status table is exhaustive and terminal states absorb; run ids are safe ids; patterns/enums match the JSON schema | `.agents/tests/test_property_run_manifest.py` |
| shared validation | `require_safe_id`, `require_env_name`, `parse_device_csv` match reference predicates over hostile inputs; safe ids are single path segments; `ensure_child_path` accepts iff the resolved location is under root (symlinks included) | `.agents/tests/test_property_validate.py` |
| session identity and state | `normalize_session_id` is total, idempotent, bounded, keeps long inputs distinct; lease allocation agrees with a reference model over random op sequences; concurrent threads never receive the same device; the file lock excludes a live holder, keeps a fresh crashed lock, recovers a stale one | `.agents/tests/test_property_session_state.py` |

## Known defects found (recorded as `expectedFailure`, not fixed here)

Each entry is a test named `test_known_defect_*` whose docstring carries the
evidence. Severity is the author's estimate.

### Route to `.remote-dev` (being extracted into its own repository)

| Sev | Module | Defect |
|-----|--------|--------|
| high | `core/endpoint.py`, `core/ssh_transport.py` | `user` starting with `-` is appended to `ssh` argv as `-oProxyCommand=…@host` and parsed as an option → local command execution from a tool argument or alias file. |
| medium-high | `core/patch_ops.py` (`REMOTE_CODEX_PATCH_PY`) | `@@ <anchor>` text is dropped by the parser; the executor replaces the first occurrence, so a hunk anchored at `def second():` edits `def first():` and reports `applied`. |
| medium-high | `core/patch_ops.py` (`REMOTE_CODEX_PATCH_PY`) | overlay keyed by unresolved `Path`: `a.py` and `sub/../a.py` (or a path through an in-root dir symlink) are two keys for one file; the later write discards the earlier hunk while reporting `applied` for both. |
| medium | `core/state_store.py` `resolve_ledger_scope` | falls back to `path_fingerprint(raw)`, which requires an absolute path; a `client_context_id` (or `CLAUDE_SESSION_ID`/`CODEX_SESSION_ID`) longer than 80 chars or without ASCII alphanumerics makes every `remote.read/write/edit` raise `PathPolicyError` before any remote call. |
| medium-low | `core/artifact_ops.py` `_safe_local_artifact_path` | `mkdir(parents=True)` runs before the containment check → directories are created outside the local dir through a pre-existing symlinked dir; a dangling symlink or regular file in a parent position raises `FileExistsError` (not `ValueError`) and crashes the pull. |
| medium-low | `core/endpoint.py` | relative `root`/`cwd` accepted at resolution; every later call fails as `path_outside_root` far from the cause. |
| low-medium | `core/artifact_ops.py` (`REMOTE_MANIFEST_PY`) | missing remote path yields `status: ok, file_count: 0` (the `FileNotFoundError` guard around non-strict `resolve()` is dead). |
| low-medium | `core/patch_ops.py` parser | `str.splitlines` also breaks on `\x0c`, `\r`, `\x1c`–`\x1e`, `\x85`, `\u2028/9`; `+a\x0c+b` is stored as `a\x0cb`. |
| low-medium | `core/patch_ops.py` (`REMOTE_CODEX_PATCH_PY`) | context-free hunk (`old == ""`) is prepended at offset 0 instead of rejected. |
| low-medium | `core/state_store.py` `resolve_ledger_scope` | `agent/1` and `agent_1` share a scope → the stale-write guard can be refreshed by another context. |
| low | `core/endpoint.py` | port range not validated; non-numeric `connect_timeout_ms` leaks `ValueError`; explicit `null` payload fields override alias values. |
| low | `core/preview.py` | `[-0:]` family: `compact_text` with `limit <= len(marker)` returns marker + whole value; `tail_text(v, 0)` and `text_preview(tail_chars=0)` return the whole value. |
| low | `core/patch_ops.py` (`REMOTE_CODEX_PATCH_PY`) | rollback rewrites untouched files and reports them as failed; directories created during a failed commit are left behind. |
| low | `core/patch_ops.py` `parse_unified_patch_paths` | `\t<timestamp>` header suffix is kept in the extracted path. |
| low | `core/path_policy.py` | `join_under_root(None)` raises `AttributeError`, not `PathPolicyError`. |
| low (advisory) | `core/permissions.py` | `RAW_REMOTE_RE` misses `;ssh`, `|ssh`, `&&ssh`, `$(ssh`. |

### Route to this repository (`.agents/lib`)

| Sev | Module | Defect |
|-----|--------|--------|
| medium | `vaws_session_state.py` `file_lock` | stale-lock recovery is check-then-unlink: two waiters that both saw a stale lock can both end up inside the critical section, so two `allocate_session_leases` calls can hand out the same NPU device. Reproduced deterministically with event ordering. |
| medium | `vaws_remote_toolbox.py` `artifact_pull` | manifest `relpath` is joined onto `local_dir` with no `..`/absolute check (the `.remote-dev` port has `_safe_local_artifact_path`); `../escaped.txt` lands outside `local_dir` with `status: ok`. Defense-in-depth gap: requires a hostile remote. |
| low-medium | `vaws_run_manifest.py` | validator diverges from `run-manifest-v1.schema.json`: accepts `schema_version: true`/`1.0`, unknown artifact keys, `sha256: null`. |
| low | `vaws_run_manifest.py` | free-form objects with non-JSON-native content (int keys, NaN) validate but do not round-trip. |
| low | `vaws_run_manifest.py` | secret filter misses `PASSWD`, `PRIVATE_KEY`, `*_KEY`. |
| low | `vaws_validate.py` `parse_device_csv` | `int(token, 10)` accepts `1_0` (→10), `+1`, Arabic-Indic and full-width digits. |

## Not testable offline (handoff to the hardware harness)

| Core | What remains | What it needs |
|------|--------------|---------------|
| endpoint / transport | `ssh_transport.run_script/run_bytes/run_remote_python` against a real host: ControlMaster mux sharing, `ConnectTimeout`, timeout → `timed_out` mapping, exit-code 72/73 propagation, stdin streaming of multi-MiB artifacts, `bash -c` quoting on the remote shell. | one reachable container endpoint |
| endpoint (managed) | `_endpoint_from_managed` against real `.vaws-local` session state and `resolve_remote_target` (a fake resolver was used). | a created session |
| patch / file executors | the `REMOTE_*_PY` scripts under the container's Python and filesystem (tests ran on local Python 3.11 / APFS, which is case-insensitive; Linux case-sensitive aliasing and overlayfs/NFS symlink semantics differ); `git apply` behaviour on the remote git version. | container with the production image |
| read ledger | cross-process ordering with two real MCP clients writing the same file. | two agents, one endpoint |
| artifacts | end-to-end pull/push over ssh; toolbox tar batch path (`_artifact_pull_tar_batch`, forced off here); `mode`/`mtime_ns` preservation. | container with `tar` |
| session state | multi-process / multi-host contention on the lease file (`O_EXCL` on NFS is not reliable; only in-process threads were tested); `port_available` probes; NPU probe integration. | two clients sharing a state dir; NPU host |
| session identity | `resolve_session_id` env/branch/binding precedence inside a real session worktree. | `session_create.py` flow |
