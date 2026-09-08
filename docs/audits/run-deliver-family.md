# Audit — the "run and deliver" family

Status: dated 2026-09-07 at 161fed1 — historical evidence

Scope: `vllm-ascend-serving`, `vllm-ascend-benchmark`,
`vllm-ascend-pd-serving`, `remote-code-parity`, `modelscope`.

Read at scaffold commit `161fed1`. Every line reference is to that commit.
Nothing in this audit changes behaviour; it is a document only.

Measured size of the family:

| Skill | argparse scripts | Python lines (scripts + tests) |
|-------|------------------|-------------------------------|
| `vllm-ascend-serving` | 4 | 2 872 |
| `vllm-ascend-benchmark` | 2 | 3 051 |
| `vllm-ascend-pd-serving` | 1 | 813 |
| `remote-code-parity` | 6 | 4 337 |
| `modelscope` | 4 | 1 117 |
| **total** | **17** | **12 190** |

The brief counts 19 entry points for this family. The difference is two parity
test modules that construct `argparse.Namespace` objects and therefore match an
`argparse` grep
(`remote-code-parity/tests/test_sync_decisions.py`,
`remote-code-parity/tests/test_git_transport.py`). They are not agent-facing.
This audit works with the **17 real entry points** and says so explicitly
wherever the count matters.

---

## 1. Capability inventory

Paths below are relative to `.agents/skills/`. "Called by" was established by
reading code, not only by grep — several of these are invoked as subprocesses
from other scripts.

### 1.1 `vllm-ascend-serving` (4 entry points)

**`vllm-ascend-serving/scripts/serve_start.py`** (1 451 lines)

* Verbs: one — start; `--relaunch` is a second mode of the same verb
  (`serve_start.py:869`).
* Real parameters: `--session-id` / `--session-file`, `--preset`, `--model`,
  `--served-model-name`/`--served-name`, `--tp`, `--dp`, `--devices`,
  `--extra-env` (repeatable), `--unset-env`, `--unset-args`, `--relaunch`,
  `--skip-parity`, `--port`, `--health-timeout`, `--wrap-script`, plus
  everything after a bare `--` forwarded to `vllm serve`
  (`serve_start.py:845-882`, split at `serve_start.py:892-895`).
* What it does, in order: resolve session → take a per-session serving lock →
  apply preset defaults → preset preflight against the container → require a
  live NPU lease → validate the remote model path → stop any previous service →
  run parity → guard `/etc/hosts` → probe host NPUs → allocate a leased port →
  build and launch an escaped bash script under `nohup`/`disown` → staged
  readiness wait ending in one real request → persist state → emit JSON.
* Called by: `vllm-ascend-benchmark/scripts/_common.py:589` (`call_serve_start`),
  `vllm-ascend-pd-serving/scripts/pd_serving.py:218-240`
  (`service_command(action="start")`),
  `.agents/lib/vaws_remote_toolbox.py:1759` (`call_service`, which backs
  `.agents/scripts/remote_service_start.py`),
  `ascend-memory-profiling` and `ascend-profiling-collection` via their own
  `_common.py` wrappers.

**`vllm-ascend-serving/scripts/serve_stop.py`** (223 lines)

* Verbs: one — stop. Parameters: session target, `--force`
  (`serve_stop.py:67-72`).
* Does: session lock → liveness probe → SIGINT, wait 5 s → SIGTERM, wait 5 s →
  SIGKILL only with `--force` (`serve_stop.py:144-161`) → reap orphaned
  `VLLM::` processes (`serve_stop.py:47-64`) → release the port lease.
* Called by: benchmark cleanup (`_common.py:617`), `bench_compare` state
  teardown (`bench_compare.py:456`), `pd_serving.py:241-249`,
  `session-management/scripts/session_remove.py`, the toolbox service adapter.

**`vllm-ascend-serving/scripts/serve_status.py`** (184 lines)

* Verbs: one — status. Parameters: session target only
  (`serve_status.py:65-69`).
* Does: read serving state → `kill -0` → `/health` → `/v1/models` → collapse
  into `ready` / `alive_healthy` / `alive` / `stopped` / `not_found`
  (`serve_status.py:116-123`); releases the port lease when it observes
  `stopped` (`serve_status.py:134-140`).
* Called by: `pd_serving.py` status fan-out, the toolbox service adapter.

**`vllm-ascend-serving/scripts/serve_probe_npus.py`** (88 lines)

* Verbs: one — probe. Parameters: `--machine` **xor** session target
  (`serve_probe_npus.py:38-42`, enforced at `:49`).
* Does: SSH to the bare-metal host, `npu-smi info`, parse through the shared
  `parse_npu_smi_info`, return free/busy/HBM (`_common.py:252-283`).
* Called by: nothing programmatically. It is the only entry point in this family
  referenced by name in `AGENTS.md` (as a legacy `--machine` surface). Its
  actual logic is re-executed inside `serve_start.py:1202` through the same
  `probe_npus` helper.

### 1.2 `vllm-ascend-benchmark` (2 entry points)

**`vllm-ascend-benchmark/scripts/bench_run.py`** (325 lines)

* Verbs: one — run. Parameters: session target, `--model` (required),
  `--preset`, `--tp`, `--dp`, `--port`, `--served-model-name`, `--devices`,
  `--health-timeout`, `--extra-env`, `--bench-env`, `--refer-nightly`,
  `--skip-parity`, `--runs`, `--warmup-runs`, plus two positional-style
  sections `--serve-args …` and `--bench-args …` split by a hand-rolled
  tokenizer (`bench_run.py:98-121`).
* Does: assemble config (CLI > preset > nightly > default,
  `_common.py:410-573`) → `serve_start.py` as a subprocess bounded by
  `health_timeout + 300` (`_common.py:580-613`) → N `vllm bench serve` runs over
  SSH against the same warm service → `serve_stop.py` → aggregate → JSON.
* Called by: no other script. Documented as the single-state entry point.

**`vllm-ascend-benchmark/scripts/bench_compare.py`** (742 lines)

* Verbs: one — compare. Parameters: everything `bench_run.py` takes except
  `--skip-parity` (parity is always skipped, `bench_compare.py:588`), plus
  `--state LABEL=REF` (repeatable, required), `--vllm-ref`,
  `--bench-request-counts`, `--fixed-request-dataset` and its four `--fixed-*`
  knobs, `--accuracy-probe` and its two knobs, `--remote-patch-file`,
  `--allow-stale-native`, `--stale-cleanup` (`bench_compare.py:142-202`).
* Does: per state — align the in-container git ref → optional `git apply` →
  fingerprint native inputs and gate → optional safe stale cleanup → start
  service → optional accuracy probe → run every case × every run → stop →
  persist that state immediately → finally compare TPOT deltas against the
  first state.
* Called by: no other script.

### 1.3 `vllm-ascend-pd-serving` (1 entry point, 5 verbs)

**`vllm-ascend-pd-serving/scripts/pd_serving.py`** (615 lines)

* Verbs: `plan`, `start`, `status`, `smoke`, `stop`
  (`pd_serving.py:575-587`). Parameters: `--output-dir` for all;
  `--config` + `--group-file` for `plan`; `--force` for `stop`.
* Does: `plan` validates a PD config against a ready Session Group and writes
  immutable `pd-config.json` / `session-group.json` / `lifecycle.json` /
  `state.json` / `manifest.json` (`pd_serving.py:277-359`); `start` walks
  `startup_order` calling `serve_start.py` per role and rolls back already
  started roles in reverse on failure (`pd_serving.py:362-427`); `status` fans
  out `serve_status.py` plus one proxy health GET; `smoke` POSTs the configured
  request through the proxy and stores the raw response; `stop` fans out
  `serve_stop.py` in reverse order.
* Called by: nothing. It is a top-level controller.

### 1.4 `remote-code-parity` (6 entry points)

**`remote-code-parity/scripts/parity_sync.py`** (251 lines)

* Verbs: one — sync. Parameters: `--machine` (legacy) or session target,
  `--repo-root`, `--source` (repeatable), `--workspace-id`, `--runtime-root`,
  `--container-user`, `--container-cache-root`, `--preserve-path`,
  `--snapshot-id`, `--print-manifest`, `--force-reinstall`, `--dry-run`,
  `--transport`, `--apply-mode`, `--print-derived-args`
  (`parity_sync.py:175-204`).
* Does: resolve the target from a session record or machine inventory, derive
  the low-level argument set, check the persisted `sync_mode` gate
  (`parity_sync.py:220-244`), then exec `remote_code_parity.py sync`.
* Called by: `serve_start.py:81-87` (`run_parity`),
  `ascend-memory-profiling/scripts/mem_collect.py`,
  `.agents/lib/vaws_remote_toolbox.py:1569` and `:1719`
  (`sync_plan` / `sync_apply`, which back `.agents/scripts/remote_sync_*.py`).

**`remote-code-parity/scripts/remote_code_parity.py`** (2 351 lines)

* Verbs: `plan`, `sync` (`remote_code_parity.py:2299-2323`). Parameters: the
  full low-level set — `--workspace-root`, `--workspace-id`, `--server-name`,
  `--runtime-root`, `--container-identity`, `--container-cache-root`,
  `--marker-dirname`, `--preserve-path`, `--source`, `--snapshot-id`, plus for
  `sync` the container host/port/user, `--force-reinstall`, `--dry-run`,
  `--print-manifest`, `--transport`, `--apply-mode`.
* Does: everything real — postorder synthetic snapshots, container-local bare
  mirrors over receive-pack with a bundle fallback, container lock, in-place
  materialization, the reinstall trigger matrix, the seven-step runtime install
  with per-step progress, import/dependency verification, commit-id proof.
* Called by: `parity_sync.py:246`, `parity_watch.py:29`,
  `vaws_remote_toolbox.py:1587` (plan only), `vaws_task_client.py`.

**`remote-code-parity/scripts/install_consent.py`** (237 lines)

* Verbs: `resolve`, `set`, `batch-set`, `resolve-sync-mode`, `set-sync-mode`
  (`install_consent.py:184-211`) — five agent-visible subcommands over one JSON
  file.
* Called by: `parity_sync.py:22`, `remote_code_parity.py` consent read,
  `vaws_remote_toolbox.py:1628` (imported as a library).

**`remote-code-parity/scripts/parity_watch.py`** (61 lines)

* Verbs: one — watch. Parameters: `--interval`, `--once`, then **all remaining
  argv forwarded verbatim into the low-level parser**
  (`parity_watch.py:40-46`).
* Does: hash the workspace, and on change run `run_sync` forced to
  `source-only`.
* Called by: nothing; documented in the coordinator README.

**`remote-code-parity/scripts/gc_runtime_cache.py`** (62 lines)

* Verbs: one — gc. Parameters: `--container-host`, `--container-port`,
  `--container-user`, `--workspace-id` (all required),
  `--container-cache-root`, `--keep-manifests`, `--dry-run`
  (`gc_runtime_cache.py:14-22`). No session or machine surface at all.
* Called by: nothing.

**`remote-code-parity/scripts/transport_benchmark.py`** (360 lines)

* Verbs: one. Parameters: `--files`, `--bytes-per-file`, `--changed-bytes`,
  `--repeats` (`transport_benchmark.py:259-264`).
* Does: a purely local micro-benchmark in temporary repos comparing bundle vs
  receive-pack payloads. Touches no remote host.
* Called by: nothing.

### 1.5 `modelscope` (4 entry points)

**`modelscope/scripts/modelscope_auto.py`** (428 lines)

* Verbs: `ensure`, `status`, `verify`, `worker` (internal)
  (`modelscope_auto.py:370-377`). Parameters: `--model MODEL_ID=LOCAL_DIR`
  (repeatable), `--root`, `--revision`, `--proxy`, `--no-proxy`,
  `--max-retries`, `--max-workers`, `--download-parallels`,
  `--parallel-threshold-mb`, `--auto-install`.
* Does: query the official file list, compare local sizes, decide
  active / verified / needs-verify / needs-download
  (`modelscope_auto.py:155-183`), and launch a detached worker that in turn
  execs the two low-level scripts.
* Called by: nothing; it is the documented front door.

**`modelscope/scripts/download_from_modelscope.py`** (205 lines) — one verb,
`--model-id` + `--local-dir` + retry/parallelism/proxy knobs; invoked as a
subprocess by `modelscope_auto.py:284-299`.

**`modelscope/scripts/verify_modelscope_sha256.py`** (353 lines) — one verb,
`--model`, `--revision`, `--chunk-size`, `--output-dir`, `--output-prefix`,
`--write-model-sha256sums`, `--ignore-extra`, `--ignore-official`; invoked as a
subprocess by `modelscope_auto.py:260-272`.

**`modelscope/scripts/modelscope_download_status.py`** (131 lines) — one verb,
`--model`, `--revision`, `--ignore-official`. **Nothing calls it.** Its whole
body (`:64-126`) recomputes what `modelscope_auto.py:107-193` already computes
from the same API call.

---

## 2. Classification per capability

40 capabilities across the 17 entry points.

| Class | Count |
|-------|-------|
| closed-world mechanics | 26 |
| mixed (a real seam inside one capability) | 6 |
| open-world judgment | 3 |
| redundant (another entry point already owns it) | 5 |

### 2.1 Serving — 12 capabilities (10 closed, 2 mixed)

| # | Capability | Class | Evidence | Owner after collapse |
|---|-----------|-------|----------|----------------------|
| S1 | session/target resolution | closed | `vllm-ascend-serving/scripts/_common.py:140-167` | one shared resolver (§5.1) |
| S2 | SSH exec + quoting + timeout semantics | closed | `_common.py:67-100` | one shared SSH layer (§5.2) |
| S3 | host NPU probe + occupancy parse | closed | `_common.py:252-283` | `serve` command |
| S4 | device selection from the session lease | closed | `serve_start.py:1022-1068` | `serve` command |
| S5 | service port lease + release | closed | `serve_start.py:1242-1266`, `serve_status.py:134-140` | `serve` command |
| S6 | launch-script construction (Ascend preamble, dynamic custom-op `set_env.bash`, heredoc safety) | closed | `serve_start.py:240-343`, guard at `:226-238` | `serve` command |
| S7 | staged readiness (log markers → `/health` → `/v1/models` → one real request) | closed | `serve_start.py:514-708` | `serve` command |
| S8 | stop escalation + orphan reap | closed | `serve_stop.py:37-64`, `:144-179` | `serve` command |
| S9 | serving state, lock, relaunch merge | closed | `_common.py:173-212`, `serve_start.py:782-832`, lock at `serve_start.py:931-934` | `serve` command |
| S10 | preset load + container preflight | **mixed** | mechanics `_common.py:222-245` + `serve_start.py:722-775`; the *choice* of preset is judgment | seam in §3 |
| S11 | launch-failure attribution (environment vs code) | **mixed** | `serve_start.py:458-511` | seam in §3 |
| S12 | `--wrap-script` extension hook | closed | `serve_start.py:319-328` | `serve` command |

Real-hardware validation that would make S1–S9 mature: they are already the
best-tested mechanics in the family (`tests/test_serving_identity.py`, 570
lines, covering A3 `npu-smi` parse fail-closed, heredoc escaping, probe-error
semantics, failed-launch cleanup, unset-arg merge). What is *not* covered by
either tests or a recorded run: (a) a `--wrap-script` launch end-to-end, (b) a
TP>1 start on a container that has never had the `/etc/hosts` guard applied —
the guard at `serve_start.py:168-182` is the fix for a recorded failure
signature but no acceptance criterion asserts it, (c) simultaneous `start` in
two sessions on one host contending for the same free cards.

### 2.2 Benchmark — 10 capabilities (5 closed, 2 mixed, 1 open, 2 redundant)

| # | Capability | Class | Evidence | Note |
|---|-----------|-------|----------|------|
| B1 | config assembly priority CLI > preset > nightly > default | **mixed** | `_common.py:410-573` | merge mechanics closed; *what to benchmark* is judgment |
| B2 | start/stop the service around a run | **redundant** | `_common.py:580-633` wrapping `serve_start.py` / `serve_stop.py` | serving already owns it |
| B3 | remote `vllm bench serve` exec + result-JSON recovery | closed | `_common.py:764-835` | needs the shared SSH layer |
| B4 | metrics extraction + mean/stddev aggregation | closed | `_common.py:1175-1200`; duplicated at `bench_run.py:124-156` **and** `bench_compare.py:206-224` | one implementation |
| B5 | in-container git-ref alignment (`pr:NNNN` / sha / branch) | closed | `_common.py:676-750` | `bench` command |
| B6 | native-input digest gate | closed | `_common.py:841-885`, gate at `bench_compare.py:638-685` | strongest fail-closed gate in the family |
| B7 | fixed-token-count dataset generation | closed | `_common.py:926-1066` | `bench` command |
| B8 | deterministic accuracy probe | **mixed** | `_common.py:1069-1154`; prompt baked at `bench_compare.py:85-88` | seam in §3 |
| B9 | is this result trustworthy / is it a regression | **open-world** | scripted at `references/behavior.md:78` | see §3, error A |
| B10 | stale vLLM process cleanup | **redundant** | `_common.py:1204-1295` vs `serve_stop.py:47-64` | two different kill policies (§5.4) |

Real-hardware validation for B3–B7: the 689-line test module is entirely
mock-based (`tests/test_bench_scripts.py`). What a mature `bench` command needs
recorded on hardware: a two-state comparison where the native digest genuinely
differs (proving the gate fires rather than being asserted in a mock), a
`--bench-request-counts` sweep on one warm service, and one run where the
service fails to stop, proving the `cleanup_failed` non-zero exit at
`bench_run.py:264-266` is reachable.

### 2.3 PD serving — 5 capabilities (2 closed, 1 mixed, 1 open, 1 redundant)

| # | Capability | Class | Evidence |
|---|-----------|-------|----------|
| P1 | PD config + Session Group validation (roles, unique members, single shared snapshot, explicit connector) | closed | `pd_serving.py:74-207` |
| P2 | ordered start with reverse rollback, reverse stop | closed | `pd_serving.py:362-427`, `:526-571` |
| P3 | per-role start / status / stop | **redundant** | `pd_serving.py:210-249` builds `serve_*.py` argv |
| P4 | proxy health + KV-path smoke evidence | **mixed** | `pd_serving.py:429-523`; the honest `claim` string at `:511` is the right shape, the fixed `timeout=5` at `:458` is not |
| P5 | topology / connector / option choice | **open-world, correctly** | `SKILL.md:24-26` and `references/behavior.md:20-23` refuse to synthesize connector arguments |

P5 is the single best example in the family of leaving judgment where it
belongs: the controller records connector options for traceability and requires
the operator to write the exact CLI JSON.

Real-hardware validation: all seven tests are mock runners
(`tests/test_pd_serving.py`). Nothing has ever exercised `plan → start → smoke
→ stop` against two live sessions. Until it has, `status`'s acceptance of
`alive_healthy` as ready (`pd_serving.py:464`) is an unvalidated relaxation of
the single-node contract, which requires a real first token
(`serve_start.py:674-684`).

### 2.4 Parity — 9 capabilities (7 closed, 1 mixed, 1 redundant)

| # | Capability | Class | Evidence |
|---|-----------|-------|----------|
| R1 | synthetic snapshot, mirror publish, transport selection + fallback | closed | `remote_code_parity.py:432-514`, `:728-873` |
| R2 | in-place materialization with preserved runtime-private paths | closed | `remote_code_parity.py:955-1031`, preserve list at `:177` |
| R3 | reinstall trigger matrix + build-input fingerprints | closed | `remote_code_parity.py:1033-1054`, `:1477-1517` |
| R4 | runtime install steps incl. `check-build-compat` preflight and requirements reconciliation | closed | `remote_code_parity.py:1056-1386`; preflight `:1239-1272`; image-provided reconciliation `:1178-1237` |
| R5 | `--apply-mode auto` tiering | closed | `remote_code_parity.py:1776-1795` |
| R6 | sync-mode / first-install consent | **mixed** | recording is closed (`install_consent.py:52-80`); *asking* is judgment (`SKILL.md:170-175`) |
| R7 | continuous source-only staging watcher | closed | `parity_watch.py:16-33` |
| R8 | container cache manifest GC | closed | `gc_runtime_cache.py:25-54` |
| R9 | bundle-vs-receive-pack micro-benchmark | **redundant as an entry point** | `transport_benchmark.py` — a development measurement tool with no remote surface; it belongs beside the tests, not in the agent's command list |

R1–R5 are the most thoroughly reasoned mechanics in the repository and the
acceptance file reads like a regression ledger. Their remaining maturity gap is
not logic, it is surface: five separate commands for one mechanic (see §4.2).
Hardware validation still missing: a `--transport git` failure that genuinely
falls back to bundle against a live container (tests cover it with fakes at
`tests/test_git_transport.py:327`), and a first install on an image whose torch
pin mismatches, proving `check-build-compat` fires before the multi-minute
build instead of after.

### 2.5 ModelScope — 4 capabilities (2 closed, 1 open, 1 redundant)

| # | Capability | Class | Evidence |
|---|-----------|-------|----------|
| M1 | download / resume / detached background worker | closed | `modelscope_auto.py:209-254`, `download_from_modelscope.py:152-191` |
| M2 | expected-vs-local size status | **redundant** | `modelscope_download_status.py:64-126` duplicates `modelscope_auto.py:107-193` |
| M3 | SHA256 verification + report artifacts | closed | `verify_modelscope_sha256.py:157-297` |
| M4 | repair after a real mismatch | **open-world, correctly** | `modelscope_auto.py:394-399` marks `verify-failed` and stops; `SKILL.md:42` says report and ask before repair |

Validation status: **zero tests** for the whole skill — the only skill in this
family with none. Before it is called mature, three things need a real run
recorded: a resume that survives a killed worker (the PID file is written by
the parent at `modelscope_auto.py:253`, not by the worker, so a worker that
dies before its first write leaves a stale-but-plausible PID), a SHA256
mismatch path, and a `--root` discovery over a directory tree that contains a
non-model subdirectory.

---

## 3. The two opposite errors

### Error A — a skill scripts a decision the agent should make

**A1 (sharpest in the family). The 3 % regression verdict.**
`vllm-ascend-benchmark/references/behavior.md:78`

> compute the ratio `r = T_p / T_b`. If `r < 0.97`, the patched version is
> considered a throughput regression. The same threshold applies to
> `spec_decode_acceptance_rate` … TTFT and TPOT regressions use inverted
> comparison

One constant decides "regression" for throughput, acceptance rate, TTFT and
TPOT alike, on any model, at any concurrency, with any number of runs. The same
skill computes the sample stddev that would tell an agent whether a 3 % gap is
even outside noise (`bench_run.py:145-155`,
`bench_compare.py:216-224`), and `bench_compare.py:508-511` reports
`delta_tpot_pct_vs_first` without a verdict — the numbers are right there and
the guidance overrides them with a fixed number. Deciding whether a delta is
real, given the observed spread, the warmup count, and whether the native gate
warned, is exactly the judgment the target state wants to leave to the agent.
The document should describe what makes a comparison trustworthy (identical
serve/bench args, digest match, spread smaller than the delta, more than one
request-count case) and stop naming a threshold.

**A2. A failed launch is diagnosed as an environment fault and prescribed a
force-reinstall.** `vllm-ascend-serving/scripts/serve_start.py:458-511`

A substring match on the stderr tail (`ImportError`,
`ModuleNotFoundError`, `cannot open shared object file`, …) produces
`cause: "remote Python package version mismatch"` and a
`recovery_command` of `parity_sync.py --session-id … --force-reinstall`. Two
problems. First, `ImportError` at `:467` matches any import error in any model
code, including one the agent just wrote — the classifier cannot distinguish
"the runtime is wrong" from "my change is wrong", which is precisely the
open-world question the brief names. Second, the prescription contradicts
parity's own rule that `--force-reinstall` costs 5–15 minutes of remote
compilation and must not be used as a precaution
(`remote-code-parity/SKILL.md:295-297`). The mechanics worth keeping are the
tag extraction and the "never bare `pip install` in the container" warning at
`:504-510`; the *diagnosis* and the *recovery choice* should be returned as
evidence, not as a command.

**A3. Bench load shape is chosen for the agent.**
`vllm-ascend-benchmark/scripts/_common.py:361-378` injects
`--backend openai-chat`, `--endpoint /v1/chat/completions`,
`--num-prompts 64`, `--max-concurrency 16` whenever the caller did not.
`references/behavior.md:85-90` admits these are "conservative defaults suitable
for a quick smoke test". A benchmark that silently substitutes a smoke-test
load when the agent forgot to specify one produces a number that looks like a
benchmark result and is not; `needs_input` would be the honest answer.

**A4. The accuracy evidence is a baked-in arithmetic prompt.**
`bench_compare.py:85-88` ships
`"Solve this exactly and return only the final integer: 12345 + 67890 - 11111."`
and `DEFAULT_ACCURACY_MAX_TOKENS = 64` as the default probe. Hashing the output
text is closed-world and correct (`_common.py:1098-1104`); choosing a prompt
whose output is sensitive to the change under test is judgment, and a fixed
arithmetic prompt will happily return an identical hash across a change that
broke long-context or tool-call behaviour.

**A5. Fixed retry and escalation policies stand in for a decision.**
Three examples, all in mechanics where the right retry depends on what failed:
`serve_start.py:1110-1121` escalates a previous service to SIGKILL
unconditionally after a 20-second deadline, while `serve_stop.py:154-168`
refuses to SIGKILL without `--force` — the same mechanic with two opposite
policies, and the more destructive one is the implicit path;
`download_from_modelscope.py:171-190` retries exactly 3 times with a fixed 10 s
sleep for any exception, including auth failures that will never succeed;
`modelscope_auto.py:72-87` (and its two copies) retries the metadata API 5
times with `min(30, attempt*5)` backoff.

**A6. `bench_compare.py` decides how many runs are enough.**
`bench_compare.py:533` and `:537` default to 3 runs with 1 warmup. Three
measurements minus a warmup is two samples; the script then reports a stddev
over two samples as if it characterized the spread. Either the default should
be absent (forcing the agent to choose) or the output should say how much
confidence two samples buy.

### Error B — a skill leaves mechanics to the agent that should be pinned

**B1 (sharpest in the family). Rebuilding the custom CANN operators after
parity.** `vllm-ascend-serving/references/command-recipes.md:250-264`

```
python3 .agents/scripts/remote_job_start.py \
  --session-id <session-id> --kind build \
  --cwd /vllm-workspace/vllm-ascend \
  --command 'bash csrc/build_aclnn.sh /vllm-workspace/vllm-ascend ascend910b'
...
Note: if numpy>=2.0 is installed, first downgrade through parity or use the
same HuaweiCloud pip index: pip3 install "numpy<2.0.0" -i https://...
```

The agent is asked to hand-assemble a remote build invocation containing a
literal runtime root **and a hardcoded SoC name** (`ascend910b`), then to
hand-run a `pip install` inside the container. Every part of this is already
pinned elsewhere: the launch script discovers the custom-op environment
dynamically and refuses to hardcode the vendor name
(`serve_start.py:263-276`); parity owns the editable install of `vllm-ascend`
including its custom ops (`remote_code_parity.py:1273-1279`); parity exports
`SOC_VERSION` from `VAWS_SOC_VERSION` rather than a literal
(`remote_code_parity.py:124`); and parity is required to fail closed with its
captured log rather than switch package sources or install modes
(`remote-code-parity/SKILL.md:74`, `:303`), which is exactly what a manual
in-container `pip3 install` bypasses — `serve_start.py:504-510` warns against
that same manual install in its own failure output. A stable
`serve --rebuild-custom-ops` (or, better, a parity apply-mode that notices
missing build artifacts) should own this. As written, the guidance instructs
the agent to do the two things the rest of the family fails closed on.

**B2. The staging watcher and the cache GC have no target surface.**
`remote-code-parity/scripts/parity_watch.py:40-46` forwards all unrecognized
argv straight into the low-level parser, so using the watcher means
hand-writing `--workspace-root --workspace-id --server-name --runtime-root
--container-identity --container-host --container-port --container-user`.
`gc_runtime_cache.py:15-19` requires container host, port, user and workspace id
outright. Both are pure mechanics over a target that
`parity_sync.py:203`/`:214-218` can already derive from a session with
`--print-derived-args`. Eight hand-copied endpoint parameters per invocation is
the parameter-shape failure mode the target state exists to eliminate.

**B3. Multi-state benchmarking by hand.**
`vllm-ascend-benchmark/references/command-recipes.md:115-175` documents a
fallback in which the agent creates git worktrees, "handles symlinking or
parity sync with the worktree path" (`:136`), runs `bench_run.py` per state,
then "collects all JSON outputs and compares" (`:177-179`). Aligning source,
holding configuration identical across states, and computing deltas are exactly
what `bench_compare.py` already does, including the native-input gate that this
manual path silently loses. Keeping the manual recipe as documentation
guarantees that when `bench_compare.py` does not quite fit, the agent will
reconstruct a comparison without the gate that makes it valid.

**B4. Consent is a five-subcommand surface with one correct usage.**
`install_consent.py:184-211` exposes `resolve`, `set`, `batch-set`,
`resolve-sync-mode`, `set-sync-mode`, and every consumer path in the repo needs
exactly one of them —
`set-sync-mode --sync-mode local --allow-first-install --approved-by-user`,
which `SKILL.md:80` and `references/behavior.md:53` both have to explain at
length because the alternative combinations exist and produce a second prompt or
a clobbered field. The state file is read as a library by
`parity_sync.py:220-223` and `vaws_remote_toolbox.py:1628`; the agent-facing
surface should be one `parity consent` verb with the safe write as its default,
not five spellings of one JSON update.

**B5. `--serve-args` / `--bench-args` are parsed by a hand-rolled splitter, in
two copies.** `bench_run.py:98-121` and `bench_compare.py:127-139` implement
the same argv sectioning independently. The residue is visible:
`bench_run.py:174` falls back to `getattr(args, "serve_args", None)` for an
attribute the parser never defines, i.e. dead code left behind by the shape
being maintained twice. This is a mechanic (argv sectioning) that the agent
pays for in every invocation.

**B6. PD recovery after a rollback is left to the agent with no mechanism.**
A failed `start` writes `status: "failed"` (`pd_serving.py:407`), `start`
refuses any state other than `planned` (`pd_serving.py:371-372`), and `plan`
refuses a non-empty output directory (`pd_serving.py:283-284`). So the
documented way to retry a PD deployment after a partial failure is to invent a
new output directory by hand, losing the run id continuity that the manifest
exists to provide. Re-planning in place, or an explicit `reset` verb, is
mechanics.

---

## 4. Diagnosability gaps

### 4.1 Worst gap — an SSH transport fault is indistinguishable from a remote command failure

`vllm-ascend-serving/scripts/_common.py:76-100` converts a client-side
`subprocess.TimeoutExpired` into `CompletedProcess(rc=255, stderr="ssh_exec
timed out after …")` and, with `check=True`, raises
`RuntimeError("remote command failed (rc=255): …")`. The result an agent sees
carries no field naming the layer. The same shape exists in
`remote-code-parity/scripts/common.py:76-108` (`command failed (…)` /
`command timed out after …s`) and `vllm-ascend-benchmark/scripts/_common.py:806`
(a bare `subprocess.run(..., timeout=1200)` whose `TimeoutExpired` propagates
as a traceback through `bench_run.py:229-243`, where it is reported as
`phase: "bench_run"` — i.e. attributed to the benchmark).

This is verbatim the confusion recorded in
`.agents/knowledge/known-failure-signatures.yaml:47-54`:

> Treat instant 'timeout' from MCP remote_bash as a tool-service fault
> signature, not a remote command fault.

The cost of getting this attribution wrong has already been paid once, and the
family that surfaces it most often is the one that has not adopted the fix.
Which compounds into:

**The recorded remedy is not applied in this family.** The same signature's
resolution says to use `base_ssh_options(mux=False)` for streams and long-lived
commands, and claims it was fixed in "remote-code-parity + vllm-ascend-serving
ssh helpers". At `161fed1` the profiling skills do it
(`ascend-profiling-collection/scripts/_common.py:149`,
`ascend-profiling-analysis/scripts/_common.py:279`) and this family does not:

* `vllm-ascend-serving/scripts/_common.py:70` — mux on, for a probe chain whose
  first-token curl allows `--max-time 120` under a 180 s SSH cap
  (`_common.py:44-47`, `serve_start.py:601-611`).
* `remote-code-parity/scripts/common.py:241` — mux on, and this is the base
  command used by `ssh_exec_stream` (`common.py:271-279`), the long-lived
  streamer that carries multi-minute editable installs.
* `vllm-ascend-benchmark/scripts/_common.py:668`, `:799`, `:1274` — mux on, for
  a 300 s script runner, a 1 200 s `vllm bench serve` and the stale cleanup.

An agent hitting the recorded symptom in this family will query the knowledge
base, read that it was fixed here, and be wrong.

### 4.2 A parity failure does not say which of five commands to look at

`parity_sync.py:246` execs the low-level script with inherited stdio and simply
returns its exit code, so a failure inside `remote_code_parity.py` surfaces to
`serve_start.py:1183-1189` as
`"remote-code-parity did not return a ready state (got 'failed')"` plus the
nested payload. The nested payload is good — `run_sync` wraps every exception
with the phase name (`remote_code_parity.py:2279-2280`) and the summary records
container cache root, snapshot ids, observed runtime commits and reinstall
status (`:1610-1639`). What is missing is one level up: nothing in the returned
JSON names *which entry point* produced it, so an agent reading a failed parity
result cannot tell whether it came from `parity_sync.py`, a toolbox
`remote_sync_apply.py`, `parity_watch.py`, or a direct low-level call — and each
has different defaults for `--apply-mode` and consent. Five entry points into
one mechanic with a shared error shape and no producer field.

### 4.3 A benchmark run does not record which cleanup policy ran

Two stale-process reapers exist with different match sets:
`serve_stop.py:47-64` kills anything matching `^VLLM::` via
`pkill -9 -f`, while `_common.py:1210-1268` matches
`^(VLLM::EngineCor|VLLM::Worker|VLLMWorker|VLLM::Core)`, skips PID 1, excludes
cmdlines mentioning `sshd`/`vaws`, and signals explicit PIDs only. The
benchmark result JSON reports `matched_pids` only when `--stale-cleanup` was
passed (`bench_compare.py:402`, `:459`), and `serve_stop`'s reap count lands in
a different field (`reaped_workers`, `serve_stop.py:139`). After a run that
left NPUs occupied, an agent cannot tell from the output which policy ran, in
which order, or whether the narrow one was skipped and the broad `pkill -9 -f`
ran instead. The narrow one exists precisely because the ad-hoc predecessor
once SIGTERM'd a session's dedicated sshd
(`vllm-ascend-benchmark/SKILL.md:42`).

### 4.4 `bench_run.py` attributes a serve_start subprocess timeout to itself

`_common.py:126-150` kills the child on its watchdog and returns rc 124 with a
note appended to stderr, then `call_serve_start` raises
`"serve_start.py produced no output (rc=124)"` (`:604-607`). The service may
well be running — `serve_start.py:1374-1378` writes `status=starting` with the
PID before readiness probing exactly so it can be cleaned up. `bench_run.py`
does then call `serve_stop --force` (`:200`), but the reported failure is
`phase: "serve_start"` with `error: "no output"`, which reads as "the service
never started" when the truth is "the launch outlived my watchdog". The budget
that produced it (`health_timeout + 300`, `_common.py:37-39`, `:600`) is not in
the payload either.

### 4.5 A no-op probe entry point that cannot explain a launch block

When `serve_start.py:1201-1215` fails closed with `phase: "probe-npus"`, the
error text carries the exception string but not the parsed device table. The
standalone `serve_probe_npus.py` would return the full table — but it accepts a
`--machine` surface that `serve_start` does not, and on failure returns
`{"status": "failed", "error": …}` with `machine` and `session_id` echoed from
`args` (`serve_probe_npus.py:77-83`), so a session auto-resolved from the cwd
binding reports `session_id: null` in exactly the failure case where an agent
needs to know which session it was talking about.

### 4.6 ModelScope reports a PID that may never have existed

`modelscope_auto.py:253` writes `download.pid` from the parent immediately after
`Popen`. `pid_is_active` (`:118-126`) then reports `active` for any live PID.
If the detached worker exits before writing to `download.log`, the status output
shows `active` with a PID and a percentage that never moves, and there is no
field distinguishing "worker running" from "PID was recorded, worker gone,
someone else now holds that PID". The launch log is the only evidence and
`modelscope/SKILL.md:15` tells the agent not to read logs unless a task fails.

---

## 5. Duplication across skills

### 5.1 Four target resolvers

| Implementation | Resolves |
|----------------|----------|
| `vllm-ascend-serving/scripts/_common.py:140-167` | session → container + host endpoints, runtime base (delegates to `vaws_remote_toolbox.resolve_remote_target`) |
| `vllm-ascend-benchmark/scripts/_common.py:640-653` | session → container host + SSH port, straight out of the session dict |
| `remote-code-parity/scripts/parity_sync.py:61-137` | session **or** machine inventory → workspace root, workspace id, container identity, endpoint |
| `vllm-ascend-pd-serving/scripts/pd_serving.py:288-302` | group file member → session id, then hands it to the serving scripts |

Single owner: `vaws_remote_toolbox.resolve_remote_target` (already used by
serving). Benchmark and parity should consume it rather than re-derive endpoint
fields from session JSON; PD should keep member→session mapping (its own
concern) and delegate the endpoint entirely.

### 5.2 Four SSH execution layers

`vllm-ascend-serving/scripts/_common.py:67-100`,
`vllm-ascend-benchmark/scripts/_common.py:656-673`,
`remote-code-parity/scripts/common.py:238-268` (+ `ssh_exec_stream` at `:271`),
and `vaws_remote_toolbox.py:414-484`. Serving's copy documents the duplication
in a comment:

> Serving keeps its own copy so the serving surface stays untouched;
> `vaws_remote_toolbox.ssh_exec` is the equivalent shared implementation for new
> code. (`_common.py:83-86`)

Consequences already visible: four different mux policies (§4.1), four different
timeout semantics (180 s cap in serving, none in parity's `ssh_exec`, 300 s and
1 200 s in benchmark), and four different failure shapes. Single owner:
`vaws_ssh` + one `vaws_remote_toolbox.ssh_exec` / `ssh_stream` pair, with a
mandatory layer field in the result.

### 5.3 Two service-lifecycle wrappers over one implementation

Benchmark wraps `serve_start`/`serve_stop` as subprocesses
(`_common.py:580-633`); PD builds the same argv
(`pd_serving.py:210-249`); `vaws_remote_toolbox.call_service`
(`:1754-1797`) wraps them a third time and is itself exposed as four more
scripts (`.agents/scripts/remote_service_{start,status,logs,stop}.py`). Single
owner: the serving command, consumed as a library function by benchmark and PD
rather than re-spawned with hand-built argv — which is also what would let a
benchmark distinguish "service failed" from "my watchdog fired" (§4.4).

### 5.4 Two stale-process cleanup policies

`serve_stop.py:47-64` vs `vllm-ascend-benchmark/scripts/_common.py:1204-1295`.
Single owner: the narrow, PID-explicit, sshd-excluding implementation, moved
into the serving command and called by `stop`. There is no reason for the
serving stop path to use the broader `pkill -9 -f '^VLLM::'` when a safer
version exists two skills away.

### 5.5 Two aggregation implementations, one metrics contract

`bench_run.py:124-156` and `bench_compare.py:206-224` compute the same
mean/stddev over the same metric keys extracted by the same
`extract_metrics` (`_common.py:1175`). Single owner: `_common.py`.

### 5.6 Device-selection policy in two places

`_common.py:286-331` (`select_devices`: validate a request, else take
`free[:tp*dp]`) and `serve_start.py:1031-1068` (require a live lease, require a
requested set to be a lease subset, else take the first N of the sorted lease).
`serve_start` always populates `devices` before calling `select_devices`, so the
free-card auto-selection branch at `_common.py:321-331` is unreachable from the
only real caller — and it encodes the opposite policy from the one the skill
documents ("free cards outside the lease are never auto-selected",
`SKILL.md:210`). Single owner: the lease-derived path; the standalone probe
should return facts, not selections.

### 5.7 ModelScope metadata client, three copies

`modelscope_auto.py:72-104`, `modelscope_download_status.py:75-107`,
`verify_modelscope_sha256.py:112-143` — identical `request_json` +
`fetch_official_files` + `DEFAULT_IGNORE_OFFICIAL` triples, including the
retry policy. Single owner: one module inside the skill.

### 5.8 Parity invoked through five surfaces

`parity_sync.py`, `remote_code_parity.py`, `parity_watch.py`,
`.agents/scripts/remote_sync_plan.py`, `.agents/scripts/remote_sync_apply.py`
(the last two via `vaws_remote_toolbox.sync_plan`/`sync_apply`, which
re-implement the install-reason computation at `:1613-1620` using the same
`VLLM_*_REINSTALL_PATTERNS` the low-level script uses at
`remote_code_parity.py:1033`). Single owner: one parity command with
`sync`/`plan`/`watch`/`gc`/`consent` verbs; the toolbox scripts become thin
aliases or disappear.

---

## 6. Proposed collapse

**17 agent-facing entry points → 5 commands**, with two demotions.

| Command | Verbs | Absorbs |
|---------|-------|---------|
| `serve` | `start`, `relaunch`, `status`, `stop`, `probe` | `serve_start.py`, `serve_stop.py`, `serve_status.py`, `serve_probe_npus.py` (4→1); becomes the single owner of stale-process reaping and device selection |
| `bench` | `run` (one or many `--state`) | `bench_run.py`, `bench_compare.py` (2→1) — `run` with a single implicit state and parity enabled *is* `bench_run`; with N states and parity skipped it *is* `bench_compare` |
| `parity` | `sync`, `plan`, `watch`, `gc`, `consent` | `parity_sync.py`, `remote_code_parity.py`, `parity_watch.py`, `gc_runtime_cache.py`, `install_consent.py` (5→1), all sharing the one session/machine target surface |
| `weights` | `ensure`, `status`, `verify` | `modelscope_auto.py`, `modelscope_download_status.py` (2→1); the two low-level scripts stay as internal modules, not entry points |
| `pd` | `plan`, `start`, `status`, `smoke`, `stop`, `reset` | `pd_serving.py` (1→1), with `start`/`status`/`stop` calling the `serve` command as a library and `reset` closing the gap in §3/B6 |

Demoted out of the agent-facing surface (not deleted):

* `transport_benchmark.py` → a development measurement script beside the parity
  tests; it never touches a remote host.
* `download_from_modelscope.py`, `verify_modelscope_sha256.py` → internal
  modules of `weights`, which is already how `modelscope_auto.py:260-299` uses
  them.

What must move *into* those commands rather than staying in prose:

* the post-parity custom-op rebuild (§3/B1), with SoC and runtime root derived,
  not typed;
* one SSH layer with a layer-tagged failure shape (§4.1), so every result in
  this family says whether the transport, the tool service, or the remote
  command failed;
* one target resolver (§5.1) so `watch` and `gc` stop demanding eight endpoint
  parameters.

What must move *out* of the commands into guidance:

* the regression verdict, replaced by trustworthiness criteria plus the spread
  the scripts already compute (§3/A1);
* launch-failure attribution, replaced by structured evidence — matched tags,
  both log tails, the phase timeline that `wait_for_ready` already records —
  without a prescribed recovery command (§3/A2);
* the benchmark load shape and the accuracy prompt, which should be required
  inputs rather than defaults (§3/A3, §3/A4).

---

## 7. Answers in one place

* **Most scripted decision that should be agent freedom**: the `r < 0.97`
  regression verdict at `vllm-ascend-benchmark/references/behavior.md:78`.
* **Most under-pinned mechanic that should be a stable command**: the
  post-parity custom CANN operator rebuild, currently a hand-assembled remote
  command with a hardcoded SoC name plus a manual container `pip install`, at
  `vllm-ascend-serving/references/command-recipes.md:250-264`.
* **Worst diagnosability gap**: SSH transport faults and remote command failures
  share one shape with no layer field
  (`vllm-ascend-serving/scripts/_common.py:76-100`,
  `remote-code-parity/scripts/common.py:76-108`,
  `vllm-ascend-benchmark/scripts/_common.py:806`), while the recorded remedy for
  exactly this confusion
  (`.agents/knowledge/known-failure-signatures.yaml:47-60`) is applied in the
  profiling skills and not in this family's four SSH helpers.
* **Consolidate first**: the SSH execution layer plus target resolution
  (§5.1, §5.2). Every other collapse in §6 and every fix in §4 rides on those
  two; consolidating anything else first means porting it twice.
