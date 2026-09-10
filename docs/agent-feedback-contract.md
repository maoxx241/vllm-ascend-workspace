# Agent feedback contract — Result Envelope v1

Status: current

VAWS business tools retain enough structured information to locate a failure
**without re-running it**. Ordinary native file, shell and Git operations do
not require the Agent to construct an Envelope.

This document is the contract. The library is
`.agents/lib/vaws_result_envelope.py`, the machine-readable schema is
`.agents/schemas/result-envelope-v1.schema.json`, and conformance is
checkable with `.agents/scripts/envelope_lint.py`.

This remains the complete machine-record contract. The next Agent-facing
presentation is defined in
[agent-first-openviking-spec.md](agent-first-openviking-spec.md): tools retain
the complete record and default to a compact view with a readback reference.
That view is a presentation projection, not a complete Envelope or a new
execution state model. Doctor now defaults to that compact projection with
`record_ref`; `--full` returns the complete record. Other commands retain the
full JSON until their presentation paths are migrated. Agents do not author
these records manually.

## 1. Why this exists

The workspace spans a local laptop, a remote Ascend host, a container, NPU
devices and a model service. From the outside those layers look alike: a
non-zero exit code, a truncated stderr tail, a timeout. The repository's own
knowledge base already records the cost of confusing them — an *instant*
timeout from the remote tool service is a tool-service fault, not a remote
command fault, and the recorded avoidance for the gloo `/etc/hosts` signature
is mostly a warning not to start tuning HCCL environment variables.

Both recorded signatures share a shape: an environment or measurement problem
that presents as a model bug, where starting from the traceback is the most
expensive possible route. A result that does not say *which layer* it is
talking about invites exactly that route.

The closest existing prior art lives in the extracted
`vllm-ascend-workspace/remote-dev` repository (`core/result.py` and
`schemas/result.schema.json`): a real result contract with
`tool`, `invocation_id`, `target`, `outcome`, `status`, `summary`, `preview`,
`refs`, `artifacts`, `next`. This design builds on it rather than around it:
the outcome vocabulary, the preview/ref split and the invocation id all carry
over. What it adds is the part remote-dev deliberately left open —
`outcome: "failed"` still does not say *which layer* failed, `target` is an
untyped object, there is no environment identity, and `next` is free-form.

## 2. The envelope

One JSON object on `stdout`, in success and in failure. Progress lines stay on
`stderr`. Top-level fields are fixed; additive data goes in `extensions`.

```
schema_version   "vaws.result-envelope.v1"
envelope_id      stable id for this emission
emitted_at       RFC3339 UTC
operation        what was attempted: skill, entry_point, action, target
outcome          success | partial | failure | blocked | cancelled
exit_code        conventional process exit code (0/1/1/2/3)
summary          one line, <= 400 chars
attempt          the exact command that ran, reproducible by hand
failure          null on success; layer attribution otherwise
environment      soc/cann/driver/torch/torch_npu/vllm/vllm_ascend
evidence         run_id, manifest_ref, refs[], previews{}
next_step        actions[], do_not[], knowledge[]
parts            per-unit results for fan-out operations
attempts         retry history + idempotency class
children         bounded digests of nested envelopes
warnings         non-fatal notes
extensions        additive, producer-defined
```

### 2.1 What was attempted

`attempt.command` carries `argv`, a shell-quoted `display`, `cwd`,
`env_keys` (**names only** — values are never carried, so the envelope cannot
become the thing that leaks a credential) and `timeout_seconds`.
`attempt.reproduce` is one copy-pasteable string.

For remote work the local argv is not reproducible on its own, so
`attempt.remote_command` carries the endpoint, and either the remote `argv` or
a bounded `script_preview` with a `script_ref` to the full text. An envelope
that claims a remote command but carries neither is rejected: an agent that
cannot re-run the failing step by copy-paste will guess instead.

### 2.2 Environment identity

All seven fields are always present. An explicit `null` means "not captured";
a *missing* key would be indistinguishable from a producer that never knew the
field existed. `environment.unknown_fields` lists exactly the null ones, so a
consumer can filter comparable results without walking the object, and
`environment.source` records whether the values were probed, read from a
manifest, declared by the caller, or cached.

### 2.3 Evidence pointers

`evidence.refs[]` are locators (`name`, `kind`, `ref`, optional `bytes` and
`sha256`), never inlined content. `evidence.run_id` /
`evidence.parent_run_id` / `evidence.manifest_ref` correlate the envelope with
Run Manifest v1 (`vaws_coordinator.run_manifest`,
`.agents/schemas/run-manifest-v1.schema.json`) so one envelope can be joined
to the whole experiment it belonged to.

### 2.4 Bounded output

`text_preview()` keeps the head/tail shape of remote-dev's
`core/preview.py` — same field names, so a remote-dev result lifts
into an envelope without reshaping — and adds one rule: **a truncated preview
must carry a `ref`**. The validator rejects a truncated preview without one.
A large payload can then never crowd out the diagnosis, because the diagnosis
is always the bounded part and the bulk is always one dereference away.

The shape is duplicated rather than imported: remote-dev is already an
external repository, and `.agents/` must not take a hard import dependency
across that boundary.

## 3. The failure taxonomy

Seven layers. `unknown` is a first-class outcome, not a fallback to the
nearest guess.

Two rules make that real:

1. **Naming a layer is a claim, and the claim must cite evidence.**
   `failure.attribution_basis` must be non-empty for every layer except
   `unknown`. The validator enforces it.
2. **`unknown` cannot be confident.** `confidence: "high"` with
   `layer: "unknown"` is rejected, and a failing envelope must always carry at
   least one `next_step.action` — for an unknown, the action is whatever would
   narrow it.

`failure.ruled_out[]` records the layers the evidence *excludes*. It carries as
much value as the layer itself: the recorded gloo signature is worth having
mostly because it tells you not to touch HCCL environment variables.

### 3.1 `caller` — the invocation was wrong

Bad arguments, missing target, an unsupported flag combination. Nothing was
attempted downstream.

```json
{
  "outcome": "blocked",
  "failure": {
    "layer": "caller", "confidence": "high", "reason_code": "bad_arguments",
    "message": "--warmup-runs 3 must be less than --runs 3",
    "attribution_basis": ["argument validation rejected the invocation before any remote call"],
    "ruled_out": ["transport", "remote_env", "remote_workload", "device"]
  },
  "next_step": {"actions": [{"description": "re-run with --warmup-runs 1 --runs 3", "command": null, "ref": null}], "do_not": [], "knowledge": []}
}
```

### 3.2 `tool` — the wrapper or tool service misbehaved

A crash in our own code, non-JSON output from a child, or the recorded
tool-service signature: `remote_bash` returning `timeout` *instantly* for
every command while `remote_probe` and `remote_ls` keep working.

```json
{
  "failure": {
    "layer": "tool", "confidence": "high",
    "reason_code": "tool_service_instant_timeout",
    "message": "remote_bash returned timeout in 41ms for three different commands",
    "attribution_basis": [
      "timeout returned in 41ms; the configured timeout was 600s",
      "remote_probe against the same endpoint succeeded in the same window"
    ],
    "ruled_out": ["remote_workload", "device"]
  },
  "next_step": {
    "actions": [{"description": "fall back to a direct ssh invocation off the shared mux", "command": "ssh -o BatchMode=yes -p <port> root@<host> true", "ref": null}],
    "do_not": ["do not debug the remote command; it never ran"],
    "knowledge": [{"entry_id": "ssh-stream-over-shared-controlmaster-mux-dies-early", "summary": "instant timeout from the tool service is a tool-service fault", "avoidance": "do not retry the same stream command on the mux", "score": 6}]
  }
}
```

This is the entry the whole taxonomy exists for. Under the current
convention this arrives as `{"status": "timeout"}` and costs an hour of
debugging the wrong layer.

### 3.3 `transport` — SSH, network, or multiplexing

```json
{
  "failure": {
    "layer": "transport", "confidence": "high", "reason_code": "ssh_mux_stream_died",
    "message": "log stream over the shared ControlMaster closed after 0.4s",
    "attribution_basis": [
      "ssh exited 255 with no remote output",
      "the same command over a dedicated connection (mux=False) streamed normally"
    ],
    "ruled_out": ["remote_workload"]
  }
}
```

### 3.4 `remote_env` — the container is wrong

Missing path, import error, version mismatch, hostname not in `/etc/hosts`.
This is the recorded gloo case, and it is the layer most often mistaken for
`remote_workload`.

```json
{
  "failure": {
    "layer": "remote_env", "confidence": "high", "reason_code": "hostname_unresolvable",
    "message": "gloo::makeDeviceForHostname failed for the container hostname",
    "attribution_basis": ["stderr matches a recorded known-failure signature", "the same image serves TP1 successfully"],
    "ruled_out": ["remote_workload", "device"]
  },
  "next_step": {
    "actions": [{"description": "map the container hostname in /etc/hosts before serving", "command": "echo \"127.0.0.1 $(hostname)\" >> /etc/hosts", "ref": null}],
    "do_not": ["do not tune GLOO_SOCKET_IFNAME or other HCCL env vars before /etc/hosts is fixed"],
    "knowledge": [{"entry_id": "gloo-init-container-hostname-missing-from-etc-hosts", "summary": "fresh containers lack their own hostname mapping", "avoidance": "do not debug gloo/HCCL env vars first", "score": 8}]
  }
}
```

### 3.5 `remote_workload` — the code under test failed

The only layer that says "the thing you are testing is guilty". Claiming it
means claiming the environment was fine.

```json
{
  "failure": {
    "layer": "remote_workload", "confidence": "medium", "reason_code": "workload_nonzero_exit",
    "message": "vllm serve exited 1 during weight load",
    "attribution_basis": [
      "the process reached the weight-load stage, so imports and CANN init succeeded",
      "the same command on the baseline commit reached ready in the same container"
    ],
    "ruled_out": ["caller", "transport", "tool"]
  }
}
```

### 3.6 `device` — NPU or lease unavailable

```json
{
  "failure": {
    "layer": "device", "confidence": "high", "reason_code": "npu_busy",
    "message": "devices 4-7 are held by another session lease",
    "attribution_basis": ["npu-smi reports non-zero HBM on 4-7", "the local lease file records another session holding them"],
    "ruled_out": ["remote_workload"]
  }
}
```

### 3.7 `unknown` — honest

```json
{
  "failure": {
    "layer": "unknown", "confidence": "low", "reason_code": "unattributed",
    "message": "the service never bound its port and no stderr was produced",
    "attribution_basis": [],
    "ruled_out": ["caller"],
    "signals": [{"kind": "stderr_bytes", "value": 0}]
  },
  "next_step": {
    "actions": [{"description": "re-run with the launch wrapper's stderr retained, then re-attribute", "command": null, "ref": null}],
    "do_not": ["do not attribute this to the code under test on this evidence"],
    "knowledge": []
  }
}
```

## 4. Partial success

A multi-host operation where three of four nodes succeeded is not a boolean.
`parts[]` carries one record per unit (`unit`, `unit_kind`, `outcome`,
`layer`, `reason_code`, `summary`, `refs`), and the aggregate `outcome` is
*derived* from them — the validator rejects an envelope whose declared outcome
disagrees with its parts, because a producer that hand-writes both will
eventually disagree with itself.

- every unit succeeded → `success`
- some succeeded, some failed → `partial`
- all failed → `failure` (or `blocked` when every failing unit was blocked)

`failure_from_parts()` summarizes: one shared layer across every failing unit
is reported as that layer; a mix is reported as `unknown`, because "some nodes
hit the transport and some hit the workload" genuinely is not one diagnosis.
Shared `unknown` is still unknown: the helper keeps `confidence: "low"` even
when every unit failed or was blocked. Counting more unattributed failures
does not create an attribution, and the validator still rejects `unknown` +
`high`. A failing part must carry a layer — `unknown` if that is the truth —
so a per-node result is never less diagnosable than a whole-operation result.

## 5. Retries and idempotency

`attempts.count` plus `attempts.records[]` (per-attempt outcome, layer,
reason code, duration). A transport failure on attempt 1 that succeeded on
attempt 2 is a *flake report*, and it is worth keeping even in a successful
envelope.

`attempts.idempotency.class` is one of `idempotent`, `at_most_once`,
`unsafe_to_retry`, `unknown`. `retry_safe` is **derived** from the class, not
set by the caller, so the two can never disagree, and it stays `null` unless
the producer actually classified the operation. Defaulting an unclassified
operation to "safe to retry" is how a benchmark gets run twice against a
service that was already holding NPU memory.

## 6. Composition of nested envelopes

Most entry points are wrappers: `bench_run` calls `serve_start`, which calls
`parity_sync`, which calls `remote_code_parity`. Composition rules:

1. **A child appears as a bounded digest, not a nested envelope.**
   `children[]` holds `envelope_id`, `entry_point`, `action`, `outcome`,
   `layer`, `reason_code`, `summary`, `ref`, `depth`. The full child is
   reachable through `ref`. A digest containing its own `children` is
   rejected: a three-level call chain must not crowd out the diagnosis it
   exists to deliver.
2. **A parent with no failure of its own adopts the child's attribution.**
   Re-guessing at each frame is how a `transport` fault becomes a
   `remote_workload` fault two levels up.
3. **A nested `caller` fault becomes the parent's `tool` fault.** The parent
   *is* the caller — it built those arguments — so a child rejecting them is
   the parent's bug, not the user's. This is the one layer that changes as it
   propagates.
4. **`do_not` guidance is inherited.** A known signature detected three frames
   down must not be lost on the way up.
5. **Exclusions are frame-aware.** Child `ruled_out` values describe the
   child's wrapper. After a nested `caller` fault becomes the parent's
   `tool` fault, a child exclusion of `tool` is dropped: it is not an
   exclusion of the parent's wrapper. Other child exclusions propagate.
   The child's original fields stay on the child record/`ref`; composition
   must not put the parent's attributed layer in the parent's `ruled_out`.

`compose_child(parent, child, ref=…)` implements these rules, with
`escalate_child_layer` and `propagate_child_ruled_out` as the layer and
exclusion mappings.

## 7. Redaction and publishability

At runtime the envelope carries host addresses, ports and absolute paths.
That is correct — it is what makes the result actionable — and it also means
**the envelope must never be the thing that writes them into a tracked file**.

- `dumps(envelope)` — full fidelity. Only ever safe under untracked
  `.vaws-local/`.
- `redact(envelope)` — replaces IPv4/IPv6 literals, `user@host` pairs, home
  paths and secret-like keys and values with stable placeholders. Only
  loopback and `*.invalid` / `*.example` hostnames survive, because they
  identify nothing and a diagnosis is much harder to read without them. RFC
  5737 documentation ranges are deliberately *not* exempt: a redactor that has
  to reason about whether an address is "safe" is a redactor that will
  eventually be wrong. Shared data roots (`/home/weights`, `/home/models`,
  `/home/data`, `/home/cache`, `/home/shared`) are exempt because they name no
  user and a model path is load-bearing for comparing two results.
- `leak_findings(payload)` — reports residual leaks as JSON paths.
- `assert_publishable(envelope)` / `dumps_publishable(envelope)` — redact,
  re-scan, and **refuse** rather than emit if anything survived.

Anything heading for a PR body, a knowledge candidate or a tracked report goes
through `dumps_publishable`.

## 8. Versioning

`schema_version` is the const string `"vaws.result-envelope.v1"`.

- **Top level is strict.** Unknown top-level fields are rejected, because a
  typo'd field name is the most common producer bug and silently accepting it
  produces a result that looks fine and carries nothing.
- **`extensions` is the additive escape hatch.** Producers put new or
  experimental data there, and readers ignore what they do not recognize, so
  adding a field never needs a version bump.
- **A new required field or a new enum value requires v2.** Enum widening is
  not backward compatible for a reader that switches on the value.
- **Consumers lag producers, so reading is lenient.** `read_envelope()`
  validates strictly on a version it knows and, on an unrecognized version,
  downgrades unknown outcomes and unknown layers to safe values and records
  the reason in `compat_warnings` instead of raising. A reader that crashes on
  a newer producer is worse than a reader that says "I do not understand this
  layer".

`jsonschema` is not installed on the client machines this repo targets, so
`validate_envelope()` in the library is authoritative and also enforces the
cross-field rules the schema document can only partially express. The schema
document is kept in sync by a test, and is used by `jsonschema` when the
package happens to be importable.

## 9. Checking conformance

```
python3 .agents/scripts/envelope_lint.py scan                     # static survey of .agents
python3 .agents/scripts/envelope_lint.py scan --details           # + per-entry-point table
python3 .agents/scripts/envelope_lint.py check --payload-file p.json
python3 .agents/scripts/envelope_lint.py run -- python3 .agents/scripts/remote_probe.py --host 192.0.2.10
```

`scan` is a source heuristic and says so: four required checks
(`envelope_library`, `stdout_json`, `stderr_progress`, `layer_attribution`)
define conformance, five more are reported to sequence a migration, and
`stdout_purity_risk` names the exact line numbers where a plain `print()`
can corrupt the JSON channel. `run` is the real check: exactly one valid
envelope on `stdout`, no progress sentinel on `stdout`, and a process exit
code that agrees with `exit_code`.

The lint emits an envelope itself, so it is both the first conformant entry
point and a worked example. Adoption counts belong to a `scan` run, not to
this contract.

## 10. Migration plan

**Nothing in this change adapts an existing script.** The library, schema,
lint and this document are additive; adapting the entry points is sequenced
below and will land as separate changes, because several agents are working in
this repo concurrently and rewriting shared output shapes underneath them
would be hostile.

Migration is per-script and mechanical once the shared helpers land.

**Wave 0 — shared helpers (one change, no behaviour change).**
Add `envelope_*` helpers next to the existing `print_json` / `emit_progress`
in `vaws_remote_target`, `vllm-ascend-serving/scripts/_common.py`,
`vllm-ascend-benchmark/scripts/_common.py`,
`ascend-profiling-collection/scripts/_common.py` and
`remote-code-parity/scripts/common.py`. Keep both emitters; nothing switches
yet.

**Wave 1 — the shared exec and probe path.**
`remote_exec.py` / `remote_probe.py` (thin over vaws-remote-dev) and adapter `cli_error`, plus the
thin `.agents/scripts/remote_*.py` wrappers that delegate to them. This is the
highest-value wave: it is where `transport` vs `remote_env` vs
`remote_workload` is actually distinguishable (SSH exit 255 and no remote
output ⇒ `transport`; remote shell ran and the command exited non-zero ⇒
`remote_workload`; timeout with a remote pid alive ⇒ `remote_workload`,
timeout with nothing started ⇒ `transport`), and the thin `remote_*`
wrappers inherit the result. `probe_remote` already collects everything
`environment` needs, so this wave is also where environment identity starts
flowing.

**Wave 2 — service lifecycle.**
`serve_start.py`, `serve_status.py`, `serve_stop.py`, `serve_probe_npus.py`.
Fold `diagnose_env_failure` into a `remote_env` attribution with a real
`attribution_basis` and a `next_step.do_not` ("do not run bare `pip install`
inside the container"), and give it the ability to return `unknown`. Attribute
device-lease refusals to `device` rather than `blocked`-with-a-string.

**Wave 3 — nested callers.**
`bench_run.py`, `parity_sync.py`, `collect_torch_profile_case.py`,
`run_remote_analyse.py`, `pd_serving.py`, `change_validation.py`. Replace
`call_json_command`'s `RuntimeError(...)` string flattening with
`compose_child`, and re-express `bench_run`'s invented `cleanup_failed` as
`outcome: "partial"` with a `device` part for the leaked service.

**Wave 4 — fan-out and local state.**
`pd_serving.py` and any multi-host operation move to `parts[]`.
`session_*`, `machine_*`, `modelscope_*`, `npu-fleet-monitor` follow; these
are mostly `caller` / `tool` / local-state faults and are cheap.

**Wave 5 — enforce.**
Once each wave lands, raise `envelope_lint.py scan --fail-under` in CI to the
new floor. Fix scripts that print on the JSON channel on the way through,
since a plain `print()` breaks the contract regardless of the envelope.

Per-script recipe: build `operation` and `attempt` before doing anything, so
the failure path already has them; return an envelope from every exit path;
never invent a layer without an `attribution_basis`; keep `print_json` as a
thin alias during transition if callers parse the old shape.

## 11. Adoption in the extracted `remote-dev` repo

remote-dev's `core/result.py` and `schemas/result.schema.json` (now in the
external `vllm-ascend-workspace/remote-dev` repository) are the ancestor of
this design and are unchanged by it. Now that remote-dev is extracted, it
should adopt the envelope on its own terms:

- Keep `remote-dev.result.v1` as the wire format for its MCP tools, and add
  the envelope fields it is missing: a `failure` block with the same seven-layer
  taxonomy, `environment`, and `attempt.reproduce`. Its `preview`/`refs`
  split, `invocation_id` and `outcome` vocabulary already match.
- Own the layers it can actually attribute — `caller`, `tool`, `transport`,
  and `device` for lease refusals — and return `unknown` rather than
  `remote_workload` for anything it only observed through an exit code. The
  substrate rarely has the context to convict the code under test; the domain
  skill above it does.
- Keep the taxonomy as shared vocabulary rather than shared code, so neither
  repo needs to import the other. `.agents/lib/vaws_result_envelope.py` and
  the extracted `result.py` should stay independently vendorable, with the
  enum lists and the layer semantics as the contract between them.
- The composition rule that matters at the boundary: an `.agents` skill
  wrapping a remote-dev tool call treats the tool's result as a **child**
  envelope, so a nested `caller` fault surfaces as the skill's own `tool`
  fault.

## 12. The convention lines this replaces

`AGENTS.md` and `.agents/README.md` currently carry the placement-only
convention. Those files are owned by other agents in this cycle and are
deliberately not edited here. The replacement text is in the pull request
description for whoever lands it.
