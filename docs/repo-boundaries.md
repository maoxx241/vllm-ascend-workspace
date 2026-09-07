# Repository boundaries

`vllm-ascend-workspace` is being split into a multi-repo organization. Three
subsystems leave this tree:

| Subsystem | Leaves as | What it is |
|-----------|-----------|------------|
| `.remote-dev/` | `vllm-ascend-workspace/remote-dev` | Stateless SSH-backed remote developer substrate |
| `.agents/coordinator/` | `vllm-ascend-workspace/vaws-coordinator` | Task identity, shared runtime pool, managed job supervision |
| the fleet dashboard (`vaws-top` branch) | `vllm-ascend-workspace/vaws-top` | Loopback-only NPU fleet monitor |

What stays is the domain layer: 24 skills under `.agents/skills/`, shared
libraries under `.agents/lib/`, helper scripts under `.agents/scripts/`.

This document is the **consumer-side** view: what the scaffold consumes from
those three, and what they consume from the scaffold. The authoritative
counterpart is each extracted repository's own `docs/HANDOFF.md`
(`remote-dev`, `vaws-coordinator`, `vaws-top`) — those declare what each
subsystem will publish. Where this document says "must publish", read it as a
request against that handoff, not as a decision already taken.

This branch changes no behaviour. It adds the inventory below, a machine
checkable guard, and a dated baseline of the violations that exist today.

---

## 1. Dependency direction

Five subsystems, declared as data in
[`.agents/policy/repo-boundaries.json`](../.agents/policy/repo-boundaries.json):

```
layer 40   workspace-root      CI workflows, client config, top-level docs
              |
layer 30   scaffold-domain     .agents/skills, .agents/lib, .agents/scripts
              |
layer 20   coordinator         (external checkout)  ->  vaws-coordinator
              |
layer 10   remote-dev          .remote-dev          ->  remote-dev
layer 10   vaws-top            (not in this tree)   ->  vaws-top
```

A subsystem may reference a subsystem at its own layer or below. Never above.

### R1 — dependencies point downward only

*Severity: error. Classification produced: **reverse dependency**.*

A lower layer that imports a higher one cannot be extracted at all:
`.remote-dev` would not import without `.agents/lib` on `sys.path`, and
`.agents/coordinator` would not import without the domain skills present.

Worse, the current tree contains a **cycle across all three future
repositories**:

```
remote-dev/core/vaws_ops.py  --imports-->  vaws_task_client  (coordinator)
vaws_task_client             --RPC----->   coordinator/server.py
coordinator/backend.py       --imports-->  core.endpoint, core.shell_ops  (remote-dev)
```

While that cycle exists there is no valid extraction order for any of the
three, which is why cutting it is the first item in the plan below.

### R2 — cross-subsystem references target published entry points

*Severity: error. Classification produced: **legitimate consumption** when the
target is published, **misplaced ownership** or an unstable pin when it is not.*

A downward dependency is fine. A downward dependency on someone else's
internals becomes a version-pinning problem the moment the two live in
different repositories. The sharpest example: `.agents/coordinator/backend.py`
reads `.remote-dev/core/managed_jobs.py` as *source text*, compiles it, and
execs it on the remote host. That pins the coordinator to the byte content of a
file remote-dev is free to rewrite, with no version negotiation anywhere.

Published surfaces, as the policy currently declares them:

- `remote-dev`: `core.result` and `core.errors` (schema-pinned by
  `.remote-dev/schemas/result.schema.json`), `.remote-dev/mcp/server.py`,
  `.remote-dev/tools/`, `.remote-dev/schemas/`, `.remote-dev/endpoints.json`.
  Everything under `core/`, `hooks/`, `tests/` and `state/` is internal.
- `coordinator`: `vaws_task_client` as the client facade, `server.py` and
  `prepare_runtime.py` as entry points. `backend.py` and the pool internals
  are not.
- `scaffold-domain`: **nothing**. Deliberately empty — a lower layer that needs
  scaffold data must receive it through an interface the lower layer owns.

### R3 — a domain skill uses another skill's published entry points only

*Severity: error.*

Skills are independently readable units; an agent routes to one by reading its
`SKILL.md`. Reaching into a sibling's `scripts/` internals makes that routing a
lie. A referenced script counts as published when the owning skill's `SKILL.md`
names it — exactly the contract an agent can discover.

This rule currently has **zero findings**, and that is a real result rather than
a gap: `ascend-memory-profiling` and `vllm-ascend-serving` both invoke
`remote-code-parity`'s `parity_sync.py`, and that script is named in
`remote-code-parity/SKILL.md`. The domain layer already respects this axis. The
rule exists so it stays that way once 24 skills no longer share a repository
with the substrate that used to mediate between them.

### R4 — an extracted subsystem is not referenced as in-repo content

*Severity: error. Classification produced: **misplaced ownership**.*

`.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py` locates the
dashboard with `git worktree add <target> vaws-top` against *this* repository,
then validates `.agents/skills/vaws-top/SKILL.md` inside that worktree. Once
the dashboard is its own repository there is no such branch, and the bootstrap
does not fail loudly — it simply never finds anything. These are the call sites
that must learn a repository instead of a branch name.

---

## 2. Inventory

50 call-site families, covering the 71 findings the guard reports plus the ones
only reading finds. Counts by classification:

| Classification | Families | Guard findings in this class |
|---|---:|---|
| reverse dependency | 19 | 57 × R1 |
| misplaced ownership | 9 | 4 × R1, 4 × R4 |
| legitimate consumption | 12 | 6 × R2 |
| incidental | 10 | none, by design |

The guard's rules and the audit's classifications are deliberately not the same
axis. A rule says *which invariant the tree breaks*; a classification says
*what to do about it*. They diverge in two places, both visible above:
`sync_claude_skills.py` and `validate_remote_dev_scaffold.py` break R1 but are
misplaced ownership — the code moves, the dependency is not inverted — and the
six R2 hits are legitimate consumption that happens to be bound to an
unpublished surface.

The **method** column distinguishes how each row was established:

- `AST import` — read from `ast.Import` / `ast.ImportFrom` at any nesting depth.
  This is how the reverse dependencies inside function bodies were found; no
  textual search of the call site shows them.
- `AST literal` — read from a string constant with module/class/function
  docstrings excluded.
- `read` — established by reading the code, config or docs. Everything the
  guard cannot see mechanically lives here.

### 2.1 Reverse dependency — must be inverted, not preserved

| # | Site | Symbol / path | Method |
|---|---|---|---|
| 1 | `.remote-dev/core/endpoint.py:112-127` | `sys.path` insert of `.agents/lib`, then `from vaws_remote_toolbox import resolve_remote_target` | AST import (import is inside `_endpoint_from_managed`) |
| 2 | `.remote-dev/core/endpoint.py:160-172` | selector-less `resolve_endpoint` falls through to `_endpoint_from_managed`, auto-binding to the nearest session worktree | read — control flow into #1, no symbol on these lines |
| 3 | `.remote-dev/core/vaws_ops.py:20-24` | `from vaws_task_client import TaskClient` (substrate → coordinator) | AST import |
| 4 | `.remote-dev/tests/test_endpoint.py:14,19` | `.agents/lib`, `vaws_session_id` | AST import + literal |
| 5 | `.remote-dev/tests/test_managed_jobs.py:20,22` | `vaws_npu_coordination.process_guard_busy` | AST import |
| 6 | `.remote-dev/tests/test_mcp_schema.py:122,125` | `vaws_remote_toolbox` | AST import |
| 7 | `.remote-dev/tests/test_vaws_ops.py:10,15` | `vaws_task_client` | AST import |
| 8 | `.remote-dev/tests/test_cli_help.py:33,77,82,112,139,156` | `.agents/scripts/vaws.py`, `.agents/skills/*/SKILL.md` | AST literal |
| 9 | `.github/workflows/remote-dev.yml:6-8,17-19` | the substrate's contract job triggers on `.agents/lib/**` and `.agents/scripts/vaws.py` | read — YAML, outside the guard's scan |
| 10 | `.agents/coordinator/backend.py:16` | `from vaws_remote_toolbox import _load_inventory` — an underscore-private symbol across the boundary | AST import |
| 11 | `.agents/coordinator/backend.py:17` | `vaws_runtime_profile.launch_preamble` | AST import |
| 12 | `.agents/coordinator/backend.py:19-21` | `spec_from_file_location` on `.agents/skills/session-management/scripts/npu_coordination.py` | AST literal |
| 13 | `.agents/coordinator/backend.py:130,132` | reads `.agents/lib/vaws_runtime_profile.py` and `vaws_build_inputs.py` *source text* and execs it remotely | AST literal |
| 14 | `.agents/coordinator/prepare_runtime.py:17-21` | `remote_code_parity.{discover_repo_tree,iter_postorder}`, `vaws_runtime_profile.*`, `vaws_build_inputs.*` | AST import + literal |
| 15 | `.agents/coordinator/server.py:23,25` | `vaws_local_state.shared_workspace_root` | AST import |
| 16 | `.agents/coordinator/tests/test_coordinator.py:14,15,17,224,712,745,755` | `vaws_npu_coordination.*` (incl. `_confirmed_free_probe`), `vaws_run_manifest.*`, `vaws_runtime_profile.*` | AST import |
| 17 | `.agents/lib/vaws_task_client.py:18-20,155` | `vaws_agent_session.*`, `vaws_local_state.ROOT`, `vaws_build_inputs.*`, and a shell-out to `remote-code-parity`'s script | AST import + literal |
| 18 | `.agents/lib/vaws_ready_runtime.py:20,21`, `.agents/lib/vaws_managed_execution.py:11` | `vaws_run_manifest.*`, `vaws_runtime_profile.digest` | AST import |
| 19 | fleet dashboard service (on the `vaws-top` branch) | resolves the main worktree by git common-dir and reads its `.vaws-local/machine-inventory.json`; `NFM_SOURCE_WORKSPACE` overrides it | read — `docs/npu-fleet-monitor.md`; the code is not in this tree |

Rows 17 and 18 are reverse dependencies *because* row 24 below classifies
those modules as coordinator-owned. They read as ordinary intra-`.agents/lib`
imports today and become cross-repo edges the moment the files move.

### 2.2 Misplaced ownership — code on the wrong side of the boundary

| # | Site | What is misplaced | Method |
|---|---|---|---|
| 20 | `.remote-dev/core/vaws_ops.py` (whole module) | the `vaws.*` task facade is coordinator surface implemented inside the substrate | read + AST |
| 21 | `.remote-dev/mcp/tools.py:24,61-64,219-220`, `.remote-dev/mcp/schemas.py:105-114` | the substrate's MCP server hard-codes and dispatches the four `vaws.*` task tools | read |
| 22 | `.remote-dev/tools/sync_claude_skills.py:10,43,53,106` | generates `.claude/skills/*` shims from `.agents/skills`; scaffold work living in the substrate | AST literal |
| 23 | `.remote-dev/tools/validate_remote_dev_scaffold.py:52,54,64` | the substrate's validator runs the scaffold's `compileall`, `unittest` and `git diff --check` | AST literal |
| 24 | `.agents/lib/vaws_ready_runtime.py`, `vaws_managed_execution.py`, `vaws_task_client.py` | coordinator modules sitting in the scaffold's shared library. Importer census: `vaws_ready_runtime` and `vaws_managed_execution` are imported only by `.agents/coordinator/`; `vaws_task_client` is the coordinator's RPC client, consumed by `.remote-dev/core/vaws_ops.py` and one session-management test. No domain skill imports any of them. | read (importer census) |
| 25 | `.agents/coordinator/**` | the coordinator lives inside `.agents/`, the domain layer's own directory, so every path-based rule has to special-case it | read |
| 26 | `.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py:16,128,135,221,250` and `tests/test_manage_monitor.py` | locates the dashboard as an in-repo branch worktree | AST literal (R4) |
| 27 | `.github/workflows/remote-dev.yml` | the substrate's CI contract job is defined in the scaffold repo | read |
| 28 | `.github/workflows/skill-catalog.yml:102-111` | the `coordinator` CI job is defined in the scaffold repo | read |

### 2.3 Legitimate consumption — becomes an external dependency

The direction is correct in every row here; the capability will exist as an
external dependency. Rows marked **R2** are the subset bound to a surface the
target does not publish, so each one needs a published equivalent before the
repositories can version independently.

| # | Site | Consumed surface | Method |
|---|---|---|---|
| 29 | `.agents/scripts/vaws.py:11,13` | `core.result.make_result` — the schema-pinned result contract. Correctly published; not flagged. | AST import |
| 30 | `.agents/scripts/vaws_client_setup.py:55-56,92-94` | registers `.remote-dev/mcp/server.py`, a published entry point. Not flagged. | AST literal |
| 31 | `.agents/skills/session-management/tests/test_agent_sessions.py:22,98,249,277,286` | `vaws_task_client.TaskClient`, the published coordinator client. Not flagged. | AST import |
| 32 | `.agents/coordinator/backend.py:14,15` | **R2** — `core.endpoint.resolve_endpoint`, `core.shell_ops.remote_bash`. The coordinator legitimately needs endpoint resolution and remote bash; both are substrate internals today. | AST import |
| 33 | `.agents/coordinator/backend.py:26` | **R2** — reads `.remote-dev/core/managed_jobs.py` and execs its source on the remote host. Legitimate need, pinned to a file's bytes. | AST literal |
| 34 | `.agents/scripts/vaws.py:14` | **R2** — `core.vaws_ops.vaws_call`. Unpublished, and the target module is itself misplaced (row 20). | AST import |
| 35 | `.agents/scripts/vaws_client_setup.py:121` | **R2** — writes pre-write backups into `.remote-dev/state/client-setup`, the substrate's private state tree. | AST literal |
| 36 | `.agents/skills/remote-code-parity/scripts/common.py:31` | **R2** — hard-codes `.remote-dev/state/` in the sync denylist so substrate state never reaches a remote container. Correct behaviour, wrong source of truth. | AST literal |
| 37 | `.mcp.json`, `.cursor/mcp.json`, `.codex/config.example.toml`, `.grok/config.example.toml` | the substrate MCP server entry, as tracked client config | read — config, outside the guard's scan |
| 38 | `.agents/hooks/vaws_session.py:79,98` | injects `context_file` into any tool whose name contains `vaws_session` / `vaws_run` / `vaws_execution` / `vaws_finish`. Legitimate, but the name contract is undeclared, and those tools become coordinator-owned. | read — a name string, not a path |
| 39 | `.agents/skills/session-management/tests/test_agent_sessions.py:165,178-202` | asserts on `mcp__remote_dev__vaws_session` and `remote-dev__vaws_session`. Pins the substrate's *MCP server name*, which the extraction is free to change. | read |
| 40 | 5 × `.agents/skills/*/SKILL.md` "Remote substrate rule" blocks | routing guidance telling agents to prefer `.remote-dev` tools | read |

### 2.4 Incidental — no code coupling

Ten families, none of which the guard reports, and that is the point: an
AST-based scan with docstrings excluded distinguishes a mention from a
dependency where a textual search cannot.

| # | Site | Why it is not a dependency |
|---|---|---|
| 41 | `.agents/lib/vaws_remote_toolbox.py:522` | a docstring explaining that `.remote-dev/core/` keeps a byte-stable copy of an env preamble — excluded as a docstring constant |
| 42 | `.agents/lib/vaws_session_id.py:96` | docstring mentioning the `.agents/lib` copy |
| 43 | `.remote-dev/core/ssh_transport.py:17,31` | comments pointing at `.agents/lib/vaws_ssh.py` — comments are absent from the AST entirely |
| 44 | `.remote-dev/core/vaws_ops.py:18` | comment explaining the lazy import (the import itself is row 3) |
| 45 | `.agents/skills/ascend-profiling-{analysis,collection}/scripts/*.py`, `vllm-ascend-benchmark/scripts/_common.py:1229` | docstrings noting that `_common` already put `.agents/lib` on `sys.path` |
| 46 | `.remote-dev/README.md:17,106`, `DESIGN.md:33,86,88`, `VALIDATION.md:25-29` | substrate docs referencing scaffold paths |
| 47 | `.agents/README.md:5,6,21,191,194`, `AGENTS.md:76,89,90,96,97,99,106` | routing docs |
| 48 | 6 × `.agents/skills/*/references/*.md` "Relationship to remote-dev" | consumption guidance |
| 49 | `.agents/skills/session-management/references/acceptance.md:86-88`, `command-recipes.md:6` | recorded acceptance evidence |
| 50 | `.agents/coordinator/README.md` (9 mentions of remote-dev) | design prose |

### 2.5 Observation outside the boundary question

`.remote-dev/DESIGN.md:3-4` cites an absolute path under a developer's home
directory as the source of the design. That is a tracked file carrying an
absolute user path. It is not a boundary violation and this branch does not
touch `DESIGN.md`, but it should be cleaned up as part of the remote-dev
extraction, since the file moves repositories anyway.

---

## 3. The guard

[`.agents/scripts/repo_boundary_check.py`](../.agents/scripts/repo_boundary_check.py)
reads the policy, scans Python, and reports what the policy forbids. Bounded
progress on `stderr`, one JSON payload on `stdout`.

```bash
# honest picture of the current tree; always exits 0
python3 .agents/scripts/repo_boundary_check.py --mode report

# CI gate: fails on anything the baseline does not already record
python3 .agents/scripts/repo_boundary_check.py --mode enforce
```

Exit codes: `0` clean or `--mode report`, `1` policy violated, `2` unusable
policy/baseline/invocation.

### Why the policy is data, and why it is JSON

The dependency direction is declared in
[`.agents/policy/repo-boundaries.json`](../.agents/policy/repo-boundaries.json)
so that changing the intended architecture is a reviewable diff to a rule with a
`why` field, not a code change buried in a checker. JSON rather than YAML,
even though PyYAML is available: the guard then has no third-party import and
can be added to any existing CI job without a `pip install` step, and it stays
consistent with `.agents/schemas/`. Each rule and each non-obvious policy entry
carries its own `*_note` / `why` string, which is where a reader looks first.

Module ownership is *derived*, not restated: the policy lists the directories
some entry point actually places on `sys.path`, and each derived module name is
attributed to the subsystem that owns the file. So `vaws_task_client` is
coordinator-owned because `.agents/lib/vaws_task_client.py` is listed as a
coordinator root, and moving the file moves the module with it.

One deliberate exception is encoded: the `mcp` module name is ignored. The
substrate's `.remote-dev/mcp/` package collides with the installed MCP Python
SDK, and `.agents/coordinator/server.py` imports the SDK — `backend.py` appends
the substrate to `sys.path` *after* site-packages precisely to keep that
resolution order. Treating `mcp` as substrate-owned would report a dependency
that does not exist.

### The baseline

There are 71 violations in the tree today. A guard that is red on day one gets
disabled by the third person who hits it; a guard with an invisible allowlist
rots into decoration. The baseline is the honest middle:
[`.agents/policy/repo-boundaries-baseline.json`](../.agents/policy/repo-boundaries-baseline.json)
records each of today's violations, dated `2026-09-07`, with the extraction
expected to remove it and a sentence explaining what has to change.

| Removed by | Rows |
|---|---:|
| `remote-dev` | 26 |
| `vaws-coordinator` | 41 |
| `vaws-top` | 4 |

Three properties keep it from becoming an allowlist:

1. **Nothing new passes.** A violation absent from the baseline fails
   `--mode enforce`.
2. **A fixed violation must delete its row.** A baseline row that no longer
   matches anything is a hard failure, so the baseline can only shrink, and it
   shrinks in the same commit as the fix. This is the anti-rot property; without
   it, an allowlist survives the code it was describing.
3. **Every row is attributed.** `--write-baseline` regenerates the mechanical
   fields and preserves hand-written `removed_by` / `why`, but stamps new rows
   `"unassigned"` — which `--mode enforce` also rejects. Regenerating the
   baseline is therefore not a way to make the guard pass without deciding
   which extraction removes the violation.

Fingerprints are `rule|path|symbol`, with no line number, so an unrelated edit
above a violation does not invalidate its row. Multiple lines with the same
symbol collapse into one row carrying a `lines` list.

### What the guard does not cover

Stated plainly, because a guard whose scope is guessed at is worse than one
whose limits are written down:

- **Python only.** Markdown, YAML, TOML and shell mentions are inventoried by
  hand in section 2. Rows 9, 27, 28, 37–40 and 41–50 are all outside the
  scan.
- **Assembled paths.** `ROOT / ".agents" / "lib"` is caught only through the
  bare `".agents"` literal; a fully dynamic assembly would be missed.
- **Dynamic imports** are caught through their path literal
  (`spec_from_file_location`, row 12), not through the symbol they bind.
- **Non-import runtime coupling** is invisible to it: MCP tool names (rows 38,
  39), HTTP endpoints, and environment variables like `NFM_SOURCE_WORKSPACE`
  (row 19).
- **`vllm/` and `vllm-ascend/` are never scanned.** Volatile upstream code, not
  ours to police.
- **Ownership of the three `.agents/lib` coordinator modules is an assertion**,
  supported by the importer census in row 24, not a discovery. The coordinator
  extraction confirms or corrects it.
- `.agents/tests/test_repo_boundary_check.py` is the single scan exemption: the
  guard's own tests must name cross-boundary paths as fixtures. A test asserts
  that this list stays exactly one file.

---

## 4. Sequenced rewiring plan

Sequencing is forced, not chosen: the coordinator imports `core.endpoint` and
`core.shell_ops` out of remote-dev and needs a published resolver interface
before it can be rewired, and the three-repo cycle in §1 runs through
`.remote-dev/core/vaws_ops.py`. remote-dev has to settle first.

### Phase 0 — this branch

Audit, guard, dated baseline. No behaviour change, no violation fixed: fixing
one now would collide with an extraction already in flight.

### Phase 1 — `remote-dev` (blocks everything else)

1. **Invert endpoint resolution.** `resolve_endpoint` takes an injected managed
   target resolver — a protocol the substrate owns, registered by the host
   application at startup. Delete the `.agents/lib` `sys.path` insertion and the
   selector-less auto-bind from the substrate (rows 1, 2); the scaffold
   registers a resolver backed by `vaws_remote_toolbox.resolve_remote_target`
   and keeps worktree auto-binding on its own side, where session bindings
   belong.
2. **Cut the cycle.** Move `core/vaws_ops.py` and the four `vaws.*` MCP tool
   descriptors and schemas out of the substrate (rows 3, 20, 21). The MCP
   server gains a registration hook so a host can add tools instead of the
   substrate hard-coding a task facade it does not own.
3. **Publish what consumers actually use.** Beyond `core.result`: the resolver
   protocol, a `remote_bash` entry point, the managed-job worker as a shipped
   artifact rather than source text (row 13's remote-dev half), and the list of
   state paths a host must exclude from sync — which is what
   `remote-code-parity`'s hard-coded `.remote-dev/state/` denylist entry should
   read instead (rows 33, 36).
4. **Move scaffold work out of the substrate.** `tools/sync_claude_skills.py`
   returns to the scaffold; `tools/validate_remote_dev_scaffold.py` splits so
   the substrate validates only itself; `.github/workflows/remote-dev.yml` moves
   to the remote-dev repository (rows 22, 23, 27).
5. **Re-home the substrate's scaffold-dependent tests** (rows 4–8) onto fakes,
   or move the assertions to the scaffold.
6. Scaffold side: `.agents/scripts/vaws.py` stops importing `core.vaws_ops`
   (row 34) and `vaws_client_setup.py` stops writing backups into
   `.remote-dev/state/` (row 35).

Guard effect: the 26 rows attributed to `remote-dev` disappear and their
baseline rows are deleted in the same commits.

### Phase 2 — `vaws-coordinator` (depends on Phase 1)

Landed on this branch as an external checkout. Pin, locator, launcher and
arrival evidence: [coordinator-consumption.md](coordinator-consumption.md).
Host NPU coordination stays scaffold-owned and is injected through
`VAWS_HOST_QUEUE_MODULE`. `vaws_build_inputs.py` stays as a byte-pinned
parity mirror. The HTTP manager has no default `--state-dir`.

Guard effect: in-tree coordinator roots and the moved task-state writers are
gone; residual adapters are locator/launcher/setup only.

### Phase 3 — `vaws-top` (independent; last because it is cheapest)

1. `manage_monitor.py` clones or locates the `vaws-top` *repository* instead of
   `git worktree add … vaws-top`, and stops expecting
   `.agents/skills/vaws-top/SKILL.md` inside a branch worktree (row 26).
2. Invert inventory access: the dashboard is handed an inventory path or an
   endpoint rather than discovering the scaffold's git common-dir (row 19).
3. Update `docs/npu-fleet-monitor.md`, which documents the branch mechanism.

Guard effect: the 4 `vaws-top` rows disappear.

### Phase 4 — close the guard

With the baseline at zero, `--mode enforce` becomes a plain invariant and the
baseline file is an empty, dated record. `scaffold-domain.public_paths` stays
empty; that is the invariant worth keeping.

### The open question: who owns host NPU coordination

`.agents/lib/vaws_npu_coordination.py` is the host's advisory NPU lease
authority. It is used by the `session-management` skill (a domain consumer) and
by `.agents/coordinator/backend.py`, which exec-loads the skill's wrapper script
to reach it. Both readings are defensible, and both failure modes are bad:

- leave it in the scaffold, and the coordinator imports a domain skill forever;
- move it to the coordinator, and `session-management` gains a hard dependency
  on a service the repo documents as optional.

Splitting it wrongly is worse than either: two allocators against one host
SQLite lease database means double-booked NPUs, and that failure is silent
hardware contention rather than an `ImportError`. The policy therefore keeps it
scaffold-owned for now — the conservative choice, which reports the coordinator
coupling rather than blessing it — and `vaws-coordinator`'s `docs/HANDOFF.md`
should settle it.

---

## 5. Routing documentation

The repo's maintenance rule requires `AGENTS.md` and `.agents/README.md` to be
updated alongside a change like this. Both files are owned by sibling agents
right now, so this branch does not edit them; the exact lines to add are in the
pull request description instead.
