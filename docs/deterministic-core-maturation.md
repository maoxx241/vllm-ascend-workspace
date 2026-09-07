# Deterministic-core maturation

The scaffold's target state collapses deterministic mechanics — SSH transport,
file operations, patch application, search, the job registry, artifact
transfer with hash verification, environment probing — into a small CLI
surface, and matures those commands until they are boring. This document
defines "mature" operationally, records the first real-hardware pass of the
maturation harness (`.agents/maturation/`), and ranks the mechanics that are
least mature going into the CLI consolidation.

A command is not mature because it has unit tests. It is mature when it has
survived real remote hosts repeatedly, including the ugly shapes already
recorded in `.agents/knowledge/known-failure-signatures.yaml`: a long stream
dying over a shared SSH multiplexer, a container missing its own hostname
mapping, an instant "timeout" that is really a tool-service fault. Every one of
those was found on hardware, not in a unit test, so the harness runs on
hardware and reports rates, not verdicts.

## 1. What "mature" means

### 1.1 Rate, not verdict

Each declared operation is run `n` times per endpoint. The harness reports,
per operation and per endpoint:

- `passes / n` and the **Wilson 95 % lower bound** of the pass proportion,
- a duration profile (min / median / p95 / max),
- for every failure, the **layer** it is attributed to (see 1.4),
- one of five verdicts:

| verdict | meaning |
|---|---|
| `mature` | zero failures, `n >= min_repetitions` for the class, p95 within the class budget |
| `flaky` | intermittent: some repetitions passed, some failed — **19/20 is flaky, not mature** |
| `broken` | deterministic: every repetition failed |
| `slow` | zero failures but p95 above the class latency budget |
| `insufficient` | zero failures but too few repetitions to claim anything |

`flaky` versus `broken` is the harness's main product. A single green run
proves very little; a 5 % failure rate is a bug with a stochastic trigger, and
a 100 % failure rate is a contract or environment mismatch. They need
different owners.

### 1.2 Operation classes and thresholds

The declared thresholds (from `.agents/maturation/operations.yaml`) are the
acceptable *long-run* pass rate once enough repetitions have accumulated across
runs; `min_repetitions` is the least a single run must contribute per endpoint
before a `mature` verdict is even possible.

| class | mechanics | min repetitions / endpoint | pass-rate threshold | p95 budget |
|---|---|---|---|---|
| `transport` | `remote.bash` foreground: success, non-zero exit, real timeout, large output | 20 | 99.5 % | 3 s |
| `stream` | long-lived output streams (15 s+), alone and under concurrent load | 3 | 98 % | – |
| `concurrency` | 6 in-process parallel calls; 4 separate CLI processes sharing the mux | 5 | 99 % | 10 s |
| `file_ops` | `remote.read` / `ls` / `write` / `edit`, including declared second-run outcomes | 10 | 99.5 % | 4 s |
| `patch` | `remote.apply_patch` add, update+move, and re-apply contract | 10 | 99.5 % | 5 s |
| `search` | `remote.glob`, `remote.grep` | 20 | 99.5 % | 5 s |
| `transfer` | push → manifest → pull → sha256 verification, 256 KiB and 8 MiB | 5 | 99 % | – |
| `recovery` | transfer killed mid-way (agent process or SSH connection), then re-run and verified; no leftover temp files | 4 | 95 % | – |
| `jobs` | background start → poll → tail → repeat status; stop of a long job | 5 | 98 % | 20 s |
| `environment` | `remote.probe`, context snapshot, hostname self-resolution | 5 | 100 % | 8 s |

A mechanic is **mature for consolidation** when, on every endpoint kind it
targets, every operation in its class holds `mature` across at least two
independent runs on at least three distinct hosts. Anything `flaky` or
`broken` anywhere blocks the class.

### 1.3 Adversarial shapes

Every operation declares a shape; the shape is how the repetitions are
produced. The declared set must cover all of them:

| shape | what it does | which recorded failure it targets |
|---|---|---|
| `baseline` | one call per repetition | latency and contract drift |
| `long_stream` | a 15 s streaming command, in-process and as a fresh CLI process | *ssh-stream-over-shared-controlmaster-mux-dies-early* |
| `stream_under_load` | a 12 s stream while 8 short commands hit the same mux | same, with contention |
| `concurrent` | 6 calls at once from one process | mux/state races |
| `multi_session` | 4 separate CLI processes at once, sharing the ControlMaster socket | operations issued from separate sessions |
| `large_transfer` | push, manifest, pull, local/remote sha256 comparison | hash verification, throughput |
| `interrupted_transfer` | kill the wrapper process **or** only its `ssh` child mid-transfer; check for temp leftovers; re-run; verify every hash | dropped connection, partially applied sync |
| `idempotent_rerun` | same call twice with a declared second outcome, then verify | "already applied" contracts |
| `job_registry` | start, poll to terminal state, tail, status again, stop a long job | registry/state consistency |

### 1.4 Attribution layers

A boolean hides the difference between an instant failure and a timeout. Every
failed trial carries the exact command, the endpoint identity (label, kind,
remote Python, hostname digest), the raw output tail, the timing, and a layer:

| layer | evidence |
|---|---|
| `transport` | ssh exit 255, `kex_exchange_identification`, `mux_client`, connection reset/closed |
| `tool-service` | no parsable result, or a "timeout" that returned in < 25 % of its budget (the recorded *instant timeout* signature) |
| `tool-helper` | the tool's own remote helper script or wrapper code failed (`remote python failed`, `exception`) |
| `timeout` | the deadline genuinely expired |
| `remote-command` | the command ran and exited non-zero |
| `path-policy` | blocked by root / cwd / symlink policy |
| `integrity` | hash mismatch, leftover temp files, partial state |
| `remote-state` | job registry or log state inconsistent |
| `environment` | an endpoint fact violated (hostname mapping, interpreter) |
| `contract` | tool reported success but the declared expectation failed |
| `harness` | the harness itself raised |

### 1.5 Evidence and feedback

Evidence stays under untracked `.vaws-local/maturation/runs/<run-id>/`
(`trials.jsonl`, `failures/`, `hosts.json`, `run.json`, `summary.md`). Host
identities exist only there; reports and candidates use `host-a` labels.

A failure that reproduces at least twice with the same (operation, layer,
fingerprint) is turned into a redacted `known-failure-signatures` candidate and,
with `--capture-knowledge`, handed to `.agents/scripts/knowledge_capture.py`
as a CLI contract. The harness reports the capture script's own verdict and
flags `contract-drift` if the script's interface changes underneath it.

## 2. First pass — results

First pass `pass-1` ran on 2026-09-07T09:53:08Z–10:31:50Z (2322 s wall)
from `.agents/maturation/operations.yaml`, endpoint parallelism 4, default
repetitions, no `--max-load` filter. **No endpoint was skipped.** Eight
endpoints ran the full 31-operation plan (277 trials each).

**2216 trials, 2094 passes, 122 failures (94.5 % overall).** Recorded
failure layers: `tool-helper` 60, `timeout` 37, `environment` 14,
`integrity` 9, `harness` 2. The two `harness` labels are a harness defect
in the retained evidence (see 2.4): `remote.artifact_pull` leaked
`TimeoutExpired` on a wedged ControlMaster instead of returning a payload.
The attribution code now maps that shape to `transport`. The `run.json`
was not rewritten and no second hardware pass was run.

### 2.1 Endpoints

| label | kind | remote python | free MiB | load1 | hostname mapped | trials | wall s |
|---|---|---|---|---|---|---|---|
| host-a | host | 3.13.13 | 1020962 | 25.76 | yes | 277 | 2322 |
| host-a-ctr | container | 3.12.3 | 1020962 | 26.35 | yes | 277 | 352 |
| host-b | host | 3.9.9 | 1014402 | 20.65 | yes | 277 | 1428 |
| host-b-ctr | container | 3.12.3 | 1014402 | 20.65 | no | 277 | 369 |
| host-c | host | 3.9.9 | 1005312 | 17.95 | yes | 277 | 1421 |
| host-c-ctr | container | 3.12.3 | 1005312 | 17.93 | no | 277 | 344 |
| host-d | host | 3.9.9 | 1011143 | 22.84 | yes | 277 | 1400 |
| host-d-ctr | container | 3.12.3 | 1011143 | 22.2 | no | 277 | 355 |

host-a is the slow interpreter called out in §4 (Python 3.13); it is
reported, not excluded. Three of four containers failed the recorded
hostname self-mapping check at prepare time (`host-a-ctr` is the
exception). Teardown succeeded on every endpoint.

### 2.2 Per class

| class | n | pass | rate | 95 % lower | failure layers |
|---|---|---|---|---|---|
| transport | 480 | 480 | 100.0 % | 99.2 % | — |
| stream | 72 | 72 | 100.0 % | 94.9 % | — |
| transfer | 120 | 120 | 100.0 % | 96.9 % | — |
| patch | 160 | 160 | 100.0 % | 97.7 % | — |
| jobs | 96 | 96 | 100.0 % | 96.2 % | — |
| file_ops | 560 | 534 | 95.4 % | 93.3 % | timeout 26 |
| concurrency | 120 | 113 | 94.2 % | 88.5 % | timeout 7 |
| environment | 160 | 146 | 91.3 % | 85.9 % | environment 14 |
| recovery | 128 | 113 | 88.3 % | 81.6 % | integrity 9, timeout 4, harness 2 |
| search | 320 | 260 | 81.3 % | 76.6 % | tool-helper 60 |

Transport, stream, transfer, patch, and jobs had zero failures. Search
and recovery are the classes that are not ready to consolidate.

### 2.3 Per operation

Repetitions are the YAML defaults (for example `bash.echo` 30, `glob.seed`
20, each interrupted-transfer op 4). `n` is repetitions × 8 endpoints.

| operation | class | shape | n | pass | rate | 95 % lower | p50 ms | p95 ms | verdict |
|---|---|---|---|---|---|---|---|---|---|
| bash.echo | transport | baseline | 240 | 240 | 100.0 % | 98.4 % | 122 | 1160 | mature |
| bash.echo_cli | transport | baseline | 80 | 80 | 100.0 % | 95.4 % | 187 | 1194 | mature |
| bash.nonzero_exit_contract | transport | baseline | 80 | 80 | 100.0 % | 95.4 % | 129 | 1129 | mature |
| bash.timeout_contract | transport | baseline | 40 | 40 | 100.0 % | 91.2 % | 1006 | 1009 | mature |
| bash.large_output | transport | baseline | 40 | 40 | 100.0 % | 91.2 % | 271 | 1352 | mature |
| bash.long_stream_cli | stream | long_stream | 24 | 24 | 100.0 % | 86.2 % | 15233 | 16232 | mature |
| bash.long_stream_inprocess | stream | long_stream | 24 | 24 | 100.0 % | 86.2 % | 15178 | 16246 | mature |
| stream.under_load | stream | stream_under_load | 24 | 24 | 100.0 % | 86.2 % | 12199 | 13326 | mature |
| bash.concurrent | concurrency | concurrent | 40 | 40 | 100.0 % | 91.2 % | 192 | 1297 | mature |
| multi_session.bash | concurrency | multi_session | 40 | 38 | 95.0 % | 83.5 % | 219 | 1533 | flaky |
| multi_session.read | concurrency | multi_session | 40 | 35 | 87.5 % | 73.9 % | 238 | 30079 | flaky |
| read.seed | file_ops | baseline | 160 | 140 | 87.5 % | 81.5 % | 137 | 30004 | flaky |
| ls.seed | file_ops | baseline | 160 | 154 | 96.2 % | 92.1 % | 129 | 1228 | flaky |
| write.overwrite_idempotent | file_ops | idempotent_rerun | 80 | 80 | 100.0 % | 95.4 % | 404 | 3492 | mature |
| write.no_overwrite_contract | file_ops | idempotent_rerun | 80 | 80 | 100.0 % | 95.4 % | 277 | 2295 | mature |
| edit.rerun_contract | file_ops | idempotent_rerun | 80 | 80 | 100.0 % | 95.4 % | 523 | 4635 | slow |
| patch.add_then_rerun | patch | idempotent_rerun | 80 | 80 | 100.0 % | 95.4 % | 402 | 3463 | mature |
| patch.update_move | patch | baseline | 80 | 80 | 100.0 % | 95.4 % | 126 | 1163 | mature |
| glob.seed | search | baseline | 160 | 100 | 62.5 % | 54.8 % | 129 | 1165 | flaky |
| grep.seed | search | baseline | 160 | 160 | 100.0 % | 97.7 % | 136 | 1155 | mature |
| artifact.roundtrip_small | transfer | large_transfer | 80 | 80 | 100.0 % | 95.4 % | 2023 | 21428 | mature |
| artifact.roundtrip_8mib | transfer | large_transfer | 40 | 40 | 100.0 % | 91.2 % | 7917 | 42015 | mature |
| artifact.interrupted_pull_transport | recovery | interrupted_transfer | 32 | 26 | 81.2 % | 64.7 % | 5201 | 90813 | flaky |
| artifact.interrupted_pull_wrapper | recovery | interrupted_transfer | 32 | 29 | 90.6 % | 75.8 % | 6850 | 391519 | flaky |
| artifact.interrupted_push_transport | recovery | interrupted_transfer | 32 | 30 | 93.8 % | 79.8 % | 7513 | 76636 | flaky |
| artifact.interrupted_push_wrapper | recovery | interrupted_transfer | 32 | 28 | 87.5 % | 71.9 % | 7120 | 392363 | flaky |
| job.lifecycle | jobs | job_registry | 64 | 64 | 100.0 % | 94.3 % | 2935 | 7542 | mature |
| job.stop | jobs | job_registry | 32 | 32 | 100.0 % | 89.3 % | 2946 | 12386 | mature |
| env.probe | environment | baseline | 80 | 80 | 100.0 % | 95.4 % | 133 | 1219 | mature |
| env.context_snapshot | environment | baseline | 40 | 40 | 100.0 % | 91.2 % | 114 | 1173 | mature |
| env.hostname_mapping | environment | baseline | 40 | 26 | 65.0 % | 49.5 % | 886 | 7369 | flaky |

Some transport / jobs ops are `insufficient` on a single endpoint because
that endpoint contributed fewer than the class `min_repetitions`; the
aggregated `n` still clears the bar, so the verdict above is the
cross-endpoint one.

Anonymised `summary.md` excerpt (identities already labels; evidence
dir is untracked):

```
# Maturation run `pass-1`
- trials: 2216  passes: 2094  failures: 122  pass rate: 94.5%
- failure layers: {'tool-helper': 60, 'integrity': 9,
  'environment': 14, 'harness': 2, 'timeout': 37}

## Least mature first
1. glob.seed — flaky (62.5%, n=160) {'tool-helper': 60}
2. env.hostname_mapping — flaky (65.0%, n=40) {'environment': 14}
3. artifact.interrupted_pull_transport — flaky (81.2%, n=32)
4. artifact.interrupted_push_wrapper — flaky (87.5%, n=32)
5. multi_session.read — flaky (87.5%, n=40) {'timeout': 5}
```

### 2.4 Failures, with layer attribution

122 failures collapse into ten groups. Counts are from the retained
`run.json`; the two `harness` rows are called out as mislabels.

| operation | failures | recorded layer | where | what actually happened |
|---|---|---|---|---|
| `glob.seed` | 60 | tool-helper | host-b, host-c, host-d (20/20 each) | `remote.glob` returned `remote python failed` on every Python 3.9.9 host. host-a (3.13) and all four containers (3.12) were 20/20. Deterministic on that interpreter, so the aggregate `flaky` hides a per-kind `broken`. |
| `env.hostname_mapping` | 14 | environment | host-b-ctr 5/5, host-c-ctr 5/5, host-d-ctr 4/5 | `getent hosts $(hostname)` exited 3. Matches `gloo-init-container-hostname-missing-from-etc-hosts`. host-a-ctr was 5/5 (already mapped). |
| `read.seed` | 20 | timeout | host-d 20/20 | Every call sat out the 30 s budget after `multi_session.*` on the same endpoint. |
| `ls.seed` | 6 | timeout | host-d attempts 0–5 | Same hang; attempts 6–19 then passed. |
| `multi_session.read` | 5 | timeout | host-d 5/5 | Four CLI processes sharing the ControlMaster; worker-0 hit the 30 s budget every time. |
| `multi_session.bash` | 2 | timeout | host-d attempts 3–4 | Same mux, later repetitions. |
| `artifact.interrupted_pull_transport` | 6 | integrity 5 + **harness 1** | integrity on host-a, host-c, host-d, host-d-ctr; harness on host-b attempt 3 | After SIGKILL of the wrapper's `ssh` child, five trials had a parsable payload whose outcome was not `failed`/`timeout` (`partial_state`). host-b attempt 3: in-process `remote.artifact_pull` **raised** `TimeoutExpired` on the ssh/ControlMaster argv at `rerun-pull` (214 s). Recorded as `harness`; correct layer is `transport`. |
| `artifact.interrupted_pull_wrapper` | 3 | **harness 1** + timeout 2 | host-c only | Attempt 1: same leaked `TimeoutExpired` at `rerun-pull` (216 s), recorded `harness`, correct layer `transport`. Attempts 2–3 then timed out at `seed-remote` against the 180 s budget (trial wall ~392 s). Killing the connection wedged the mux; later commands on that endpoint hung for the full timeout. |
| `artifact.interrupted_push_wrapper` | 4 | timeout 2 + integrity 2 | host-b 0/4 | Attempts 0–1 timed out inspecting remote leftovers (30 s budget; trial wall 422 s / 392 s). Attempts 2–3 left `*.tmp-*` files (`leftover_temp_files`). |
| `artifact.interrupted_push_transport` | 2 | integrity | host-a-ctr, host-c | Wrapper payload after the transport drop was not a clean `failed`/`timeout`. |

host-d's timeout cascade (`multi_session` → `read.seed` 20/20 → `ls.seed`
6, then writes recovered) started **before** the interrupted-transfer
ops. The shared mux can stall under four concurrent CLI processes, not
only after an explicit `ssh` child kill. The kill path is worse: it
leaves the mux wedged so the *next* in-process `artifact_pull` never
returns a result.

That kill-the-master shape belongs to the same family as
`ssh-stream-over-shared-controlmaster-mux-dies-early`. The recorded
signature is long streams dying early; this pass shows the complementary
failure: a dead or recycled ControlMaster master, then every later
command on that endpoint burns its full timeout.

`--capture-knowledge` was not passed. The harness still prepared 11
redacted candidates (reproduced ≥ 2 times) under untracked evidence.
They were not handed to `knowledge_capture.py`.

## 3. Least mature mechanics, ranked

Harness ranking (broken / flaky by pass rate, then slow). Interpretation
is ours; the rates are from `pass-1`.

1. **`remote.glob` helper on Python 3.9.9 hosts** — 62.5 % overall,
   0/60 on the three 3.9.9 hosts, 100/100 elsewhere. `tool-helper`.
   Lowest rate in the pass, but a deterministic interpreter/helper
   mismatch, not a race. Blocks the `search` class until the helper
   either works on 3.9 or the tool fails closed with a typed error.
2. **Container hostname mapping** — 65.0 %. Already a promoted
   signature. Three of four containers are `broken` or nearly so. The
   other environment ops (`probe`, `context_snapshot`) were mature.
3. **Interrupted transfer over the shared ControlMaster** — all four
   recovery ops flaky (81–94 %). This is the finding that matters for
   consolidation. Killing the wrapper's `ssh` child when that child is
   (or becomes) the ControlMaster master wedges the mux; the next
   `artifact_pull` either leaks `TimeoutExpired` or the next `remote.bash`
   sits out its whole budget. Uninterrupted push/pull (256 KiB and
   8 MiB) were 120/120, so the bytes-and-hash path is fine — the
   *interrupt* path is not.
4. **Multi-session mux contention, then poisoned file ops** — host-d
   only. `multi_session.read` 0/5, `multi_session.bash` 3/5,
   `read.seed` 0/20, `ls.seed` 14/20. Same layer (`timeout`), same
   endpoint, then writes/patches/jobs recovered. In-process
   `bash.concurrent` (6 workers) was 40/40 everywhere, so the in-process
   dispatcher is not the problem; four separate CLI processes sharing
   the mux are.
5. **`edit.rerun_contract` latency on host-a** — 80/80 correct, p95
   4635 ms against a 4000 ms class budget, driven by host-a (Python
   3.13). A `slow` verdict, not a functional one.

Everything else in this pass — foreground `remote.bash` including the
real-timeout contract and 200 kB output, long streams alone and under
load, `grep`, patch add/rerun and update+move, job start/poll/tail/stop,
artifact hash round-trips — held `mature` on all eight endpoints.

**Least trusted mechanic today:** interrupting a transfer by killing
the `ssh` child on a shared ControlMaster (the `recovery` class). A
single such kill can take the endpoint's mux down for every later
command. `remote.glob` on 3.9.9 is a close second but it fails the same
way every time and does not poison neighbours.

## 4. Deliberately left undone

- **Parity snapshot and materialization** (`remote-code-parity`) was not
  exercised. It requires a session-bound container with a Git cache root and
  pushes synthetic refs into it; on shared machines with other people's
  services running, that is not a zero-footprint operation. It is the largest
  deterministic mechanic still without a rate.
- No operation runs from inside a managed **session worktree** (zero-argument
  endpoint auto-binding); every call passed `host`/`port` explicitly.
- The MCP **server** transport (stdio framing) was not exercised; the harness
  calls the same dispatcher in-process and the CLI wrappers as processes.
- One run per endpoint. The maturity definition in 1.2 requires at least two
  independent runs; the numbers above are a first data point, not a verdict.
- Latency budgets are fixed per class and not yet normalised for the slow
  interpreter start-up on one host; that host is reported, not excluded.
- The two leaked `TimeoutExpired` rows stay labelled `harness` in the
  retained `run.json`. Attribution was fixed for future runs; the evidence
  was not rewritten and no second hardware pass was started.
- Reproducible failures were prepared as redacted candidates but not
  captured (`--capture-knowledge` was off). Knowledge files were not
  edited.
