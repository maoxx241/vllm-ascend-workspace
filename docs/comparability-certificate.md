# Observational comparability certificate

A pair of experiments that differ in two ways cannot be attributed to either.
This certificate is the mechanical form of that rule: it diffs the identity
**observed** from each of two actual runs, key by key, classifies every
undeclared difference as a confounder, and returns a hard verdict.

It lives in `.agents/lib/vaws_comparability.py` and hangs off Run Manifest v1
as a `comparability-certificate` artifact. It does not change the meaning of
any existing manifest field.

A comparison entry point must call `consume_certificate` before it may emit
`passed`. Consuming recomputes the verdict from the identity body. A
handwritten `verdict: comparable` is rejected.

## Verdict rule

`comparable` only when every blocking list is empty:

1. every required identity group has at least one leaf;
2. every must-observe prefix has at least one **observed** leaf on each side;
3. no declaration/observation mismatch;
4. no confounder (undeclared difference);
5. every declared varying key exists on at least one side.

One confounder is enough. The verdict is hard, not a warning.

## Empty identity

`workspace_snapshot`, `environment`, `model`, and `topology` may still be `{}`
at run creation. Empty is a recorded `unknown` that blocks `comparable` and
therefore blocks `passed`. It is not a hard rejection at `init`/`plan`:
planning before observation is legitimate, and inventing values just to
satisfy a constructor would be worse. Two empty objects are never treated as
agreement; that would launder missing evidence into a match.

`failed` and `inconclusive` remain reachable without a comparable certificate
only on paths that are not two-state comparisons (graph-debug case
resolution; distributed-debug rank completion). Two-state comparison entry
points abort before classification when the certificate is `not-comparable`.

## Identity keys and origins

| Key / prefix | Typical origin | Why |
|---|---|---|
| `workspace_snapshot.*` | observed when a producer recorded the commit/digest the process used; declared when copied from the manifest at `init` | Code state. A plan-time snapshot is a promise. |
| `environment.*` | observed from the recorded runtime; declared from the manifest | Container / CANN / torch-npu. |
| `model.*` | observed offline (constructor argument) or from a recorded observation; declared online (`model` path is not what the service loaded) and from the manifest | Weights. |
| `topology.*` | observed from a recorded observation; declared from the manifest / `shared` | TP, DP, device list. Restarting with a different TP on the same URL is the failure mode this exists to catch. |
| `engine_args.*` | **observed** offline (passed to `LLM`); **declared** online (never sent to the service) | Eager/graph, TP, feature flags. Labelling the online copy `observed` would launder the declarative gate. |
| `base_url`, `served_model` | observed when the harness used them for HTTP | The request target, not the process flags behind it. |
| `cases_sha256` | observed (digest of the case array the producer iterated) | Different case files are an undeclared difference. |
| `serve_args`, `bench_args`, `dataset` | observed from a measurement observation; declared from perf `shared` | Workload identity. |
| `max_concurrency`, `request_rate` | observed from a measurement observation; declared from `shared` | Audit C5 / case 6. |
| `npu_devices` | observed from a measurement observation; declared from `shared` | Card count. |
| `native_digest` | observed only when a producer recorded the native build digest | Audit §3.3 / case 2 rebuild trigger. Absent → unknown. |

`identity_from_manifest_fields` always labels `declared`.
`identity_from_execution_block` labels online `engine_args` / `model` as
`declared`. `identity_from_recorded_observation` labels every leaf `observed`
and is only for values a producer obtained from the run.

## How comparison entry points consume it

| Entry point | What it builds from | Variable under test | On `not-comparable` |
|---|---|---|---|
| `correctness_run.py compare` | manifest (declared) + `execution` (labelled by offline/online) + optional result `observation` (observed) | `--allowed-difference` | abort; no `comparison.json`; manifest stays non-terminal |
| `performance_regression.py analyze` | `shared` (declared) + per-state measurement `observation` (observed) | `allowed_differences` or `workspace_snapshot.vllm_ascend_commit` | abort; no `passed` |
| `graph_debug_case.py compare` | case identity (declared) + `{stem}.identity.json` sidecar (observed) | `--allowed-difference` (default `execution_mode`) | abort; no comparison artifact |

`change_validation.py link` is not a two-state comparison. It now rejects a
child whose `run_type` does not match the check-domain prefix
(`correctness:` → `correctness`, `performance:` → `performance`).
`build:`, `test:`, `compatibility:`, and `operator:` have no run type and are
not checked.

`graph_debug_case.py finalize` and `distributed_debug.py analyze` can still
reach `passed` without a certificate: they are case resolution and rank
completion, not a pair of runs.

## Hardware matrix (acceptance criteria — not run)

Shared Ascend hosts were in use. The matrix from audit §8.2 is encoded as
unit tests in `.agents/tests/test_comparability_certificate.py`
(`HardwareMatrixAcceptanceTests`) from recorded observations. **It has not
been run on hardware.**

| # | Design | Expected |
|---|---|---|
| 1 | Same code, same config, twice | `comparable` |
| 2 | Only candidate code (and native digest) changes | `comparable` |
| 3 | Candidate also has `--enforce-eager` | `not-comparable` |
| 4 | Candidate also changes TP / card count | `not-comparable` |
| 5 | Case 3 under `dp > 1` | `not-comparable` |
| 6 | Two states with different `max_concurrency` | `not-comparable` |

## What the certificate still cannot catch

- A hand-written `observation` / sidecar that was not produced by the run.
- An online service restarted on the same `base_url` with different flags,
  unless those flags were recorded as an observation. Online `engine_args`
  stay declared.
- Human-applied performance `--state` labels (audit C7).
- Raw Benchmark JSON contents or hashes (audit C8).
- Graph snapshots that omit prompt/seed from the identity sidecar.
- Whether `/metrics` was summed per engine label under DP>1 (audit §7.1).
- Sampling parameters that the harness used but never wrote into the
  execution or observation block.
