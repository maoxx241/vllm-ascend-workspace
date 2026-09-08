# Property-based tests for the deterministic cores

Status: current

The scaffold's target state collapses deterministic mechanics into a small CLI
surface that is matured through heavy testing. Property suites state a
property, generate inputs against it, and keep regressions as ordinary passing
tests. A seeded finite suite is replay evidence, not exhaustive proof.

Ownership is split by repository. Do not copy or sync test implementations
between them.

| Repository | Owns | Paths |
|---|---|---|
| this scaffold | Run Manifest, shared validators, toolbox artifact path, session identity/leases/locks, and the scaffold generator support module | `.agents/tests/test_property_run_manifest.py`, `test_property_validate.py`, `test_property_artifact_toolbox.py`, `test_property_session_state.py`, `test_property_support.py` |
| standalone `remote-dev` | path policy, permissions, endpoint payloads, patch ops, read ledger, previews, artifact manifests, and its own generator support module | `tests/test_property_path_policy.py`, `test_property_permissions.py`, `test_property_endpoint.py`, `test_property_patch_ops.py`, `test_property_read_ledger.py`, `test_property_preview.py`, `test_property_artifacts.py`, `test_property_support.py` |

Managed machine/session endpoint mapping belongs to the scaffold resolver
(`.agents/lib/vaws_remote_dev_plugin.py`), not to remote-dev property tests.

## Running

From this scaffold:

```bash
python3 -m unittest discover -s .agents/tests
```

From a standalone `remote-dev` checkout (not the former in-tree remote-dev tests directory):

```bash
python3 -m unittest discover -s tests
```

The three `#90` `SubstrateIntegrationTests` need an explicit external
remote-dev source root. They live in `.agents/tests/test_remote_dev_consumer.py`
once that extraction lands. From this scaffold root:

```bash
VAWS_REMOTE_DEV_ROOT=/absolute/path/to/remote-dev python3 -m unittest discover -s .agents/tests -p test_remote_dev_consumer.py
```

That file's substrate-backed cases are:

- `SubstrateIntegrationTests.test_resolvers_env_registers_the_scaffold_selector_fields`
- `SubstrateIntegrationTests.test_machine_selector_resolves_from_a_fake_inventory_with_runtime_env_file`
- `SubstrateIntegrationTests.test_empty_payload_without_binding_yields_the_substrate_error`

Without `VAWS_REMOTE_DEV_ROOT` (or the shared checkout location) those three
skip. They are not a substitute for the remote-dev property suites.

`hypothesis` is not a dependency. Each repository's `test_property_support.py`
provides a seeded generator (`Gen`) and a case runner (`run_cases`). Every
case derives its own seed from `(suite seed, case index)`; a failure message
always contains both:

```
codex executor all-or-nothing: case #124/400 failed
(VAWS_PROPTEST_SEED=2026, case_seed=16592026355301341376): ...
```

| Variable | Default | Meaning |
|----------|---------|---------|
| `VAWS_PROPTEST_SEED` | `20260907` | Suite seed; change it to explore new inputs, set it to a reported value to replay a failure. |
| `VAWS_PROPTEST_SCALE` | `1` | Multiplier on every case count (`3` = three times as many cases). |

Case counts are bounded so the default scaffold run stays on the order of a
few seconds. Remote-side executor scripts used by remote-dev suites
(`REMOTE_FILE_PY`, `REMOTE_CODEX_PATCH_PY`, `REMOTE_MANIFEST_PY`) execute
in-process on temporary trees via `run_remote_script`; the unified-diff path
runs the real `bash` + `git apply` script through a fake transport. Nothing
needs a reachable host.

## Properties covered per core

| Core | Property | Owner / file |
|------|----------|--------------|
| path policy | accept/reject decision equals an independent lexical reference; accepted paths are absolute, normalized, `..`-free and under root; idempotent; sibling-prefix roots never confused; degenerate roots rejected; NFC/NFD forms do not alias | remote-dev `tests/test_property_path_policy.py` |
| path policy (remote layer) | on a tree with file/dir/dangling symlinks pointing outside root, no write/edit/multi_edit/patch op ever changes anything outside root, and reads never reveal outside content | same |
| permissions | secret shapes detected anywhere, case-insensitively; transports detected at word boundaries | remote-dev `tests/test_property_permissions.py` |
| endpoint | well-typed host/port/alias payloads yield well-formed endpoints; garbage payloads raise only `EndpointError`; identity depends only on user/host/port/root; alias merge is deterministic | remote-dev `tests/test_property_endpoint.py` |
| managed mapping | `machine` / `session_id` / `session_file` resolve through the scaffold `vaws` resolver; empty payload without a binding declines to the substrate "no endpoint target" error | scaffold `#90` `test_remote_dev_consumer.py` |
| patch application | `parse(render(ops)) == ops`; malformed patches rejected; an applied Codex patch equals a reference model of the tree; one failing op at any position leaves the tree byte-identical; commit failure rolls files back; unified diffs apply fully or not at all | remote-dev `tests/test_property_patch_ops.py` |
| read ledger | random read/external-change/write interleavings are judged exactly by a "last observed sha per (context, file)" model; reading another file or another context never refreshes the guard; ledgers round-trip and never leak across endpoints; scopes are single safe path segments | remote-dev `tests/test_property_read_ledger.py` |
| previews | truncated previews are exact prefix + suffix, never overlap, never split code points, and report true byte counts at limit, limit±1, empty and multi-byte input; `compact_text` is bounded; read pagination equals a reference slice with correct `partial`/truncation flags | remote-dev `tests/test_property_preview.py` |
| artifact manifests | remote and local manifests agree with a reference; every single-byte flip, truncation, extension, add, remove and rename changes the manifest; symlinks block; pulls never land bytes that disagree with the manifest and leave no `.tmp` | remote-dev `tests/test_property_artifacts.py` |
| toolbox artifacts | local manifests are deterministic and mutation-sensitive; `artifact_pull` never lands disagreeing bytes and refuses manifest relpaths that would escape `local_dir` | scaffold `.agents/tests/test_property_artifact_toolbox.py` |
| Run Manifest v1 | generated manifests validate, are never mutated, round-trip through write/load; every single-field corruption and every missing/unknown field is rejected with a message naming the field; status table is exhaustive and terminal states absorb; run ids are safe ids; patterns/enums match the JSON schema; secret-like environment keys are rejected | scaffold `.agents/tests/test_property_run_manifest.py` |
| shared validation | `require_safe_id`, `require_env_name`, `parse_device_csv` match reference predicates over hostile inputs; safe ids are single path segments; `ensure_child_path` accepts iff the resolved location is under root (symlinks included) | scaffold `.agents/tests/test_property_validate.py` |
| session identity and state | `normalize_session_id` is total, idempotent, bounded, keeps long inputs distinct; lease allocation agrees with a reference model over random op sequences; concurrent threads never receive the same device; the file lock excludes a live holder, keeps a fresh crashed lock, recovers a stale one, and cooperating local processes exclude each other | scaffold `.agents/tests/test_property_session_state.py` |

## Historical defects now ordinary passing regressions

These were first recorded by the `#89` suites (`b0dfbe5cff1f099ca46d34c49d3494c900bf1443`)
as expected failures. The accepted `#94` follow-up
(`15bb2e896429b9b3bf2c67929c6f03bbcecb770a`) and `#87` occupancy repair
(`765cb9891f49b6651547ba39a39d11a6d352c4d1`) keep them as ordinary passing
tests. Names may still say `test_known_defect_*` for provenance; they are not
current expected failures.

| Provenance | Repair | Ordinary regression |
|---|---|---|
| `file_lock` check-then-unlink let two stale waiters overlap | `#94` cooperating `.guard` reclaim | `test_known_defect_two_waiters_can_both_acquire_after_removing_a_stale_lock`; also release-timeout must not unlink a replacement owner, sidecar `EMFILE` must release the Python gate, and a body failure must not hide a lease-close I/O error |
| toolbox `artifact_pull` joined manifest `relpath` with no `..`/absolute check, so `../escaped.txt` landed outside `local_dir` with `status: ok` | `#94` | `test_known_defect_manifest_relpath_traversal_escapes_the_local_dir` |
| secret-key filter missed `PASSWD` and `*_KEY` components (`DB_PASSWD`, `SSH_PRIVATE_KEY`) | `#94` | `test_known_defect_common_secret_spellings_pass_the_filter` |
| Python validator accepted `schema_version: true`/`1.0`, unknown artifact keys, and `sha256: null` | `#94` | `test_known_defect_validator_accepts_schema_version_true_and_float`, `test_known_defect_validator_accepts_artifact_shapes_the_schema_forbids` |
| free-form objects with non-JSON-native content validated but did not round-trip | `#94` | `test_known_defect_free_form_objects_may_validate_but_not_round_trip` |
| `parse_device_csv` used `int(token, 10)`, so `1_0`, `+1`, and Unicode digits were accepted | `#94` | `test_known_defect_int_parsing_accepts_more_than_decimal_ascii_digits` |
| successful occupancy probe with `free=[]` was collapsed to unknown (`None`), so explicit/count NPU allocation skipped the guard | `#87` | `test_known_empty_free_set_refuses_explicit_and_count_requests`, `test_unknown_occupancy_refuses_npu_requests_but_allows_port_only` |

Remote-dev historical defects (SSH `user` option injection, patch overlay
aliasing, ledger scope collisions, and the other `#89` remote-dev
`test_known_defect_*` cases) belong to standalone `remote-dev` `tests/` and
were repaired there. This scaffold does not keep copies of those tests.

## Offline evidence boundary

Covered here, offline, on local POSIX files:

- scaffold property suites under `.agents/tests`
- lock exclusion for local threads **and** cooperating independent processes
- occupancy fail-closed behavior with mocked transport and temporary lease state

Not qualified by these suites:

- NFS or cross-host lock coherence
- actual SSH interruption, mux recovery, or ControlMaster behavior
- hardware NPU allocation, `npu-smi`, or container runtime behavior

Existing Fable hardware-harness evidence remains distinct and is not repeated
or replaced by this document. No additional hardware pass is requested here.
