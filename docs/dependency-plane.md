Status: current

# Dependency plane

This scaffold consumes four extracted repositories as pinned checkouts, not
submodules and not vendored copies. One pin schema, one loader, and one
capability report are the contract.

## Pins

Tracked files live under `.agents/deps/`. The schema is
`.agents/schemas/dependency-v1.schema.json`. Required fields are
`schema_version` (1), `name`, `repository`, `url`, `visibility`, `ref`,
`commit`, `root_env`, `default_checkout`, `bootstrap`, `consumed_surface`,
and `identity`. Optional fields are `note` and `extensions`.

`consumed_surface` is either an array of surface names or an object mapping
each surface to the implementing path (a string, or a string list when one
surface has several files). Prefer the object form when the mapping is known.

`default_checkout` is a template. Placeholders are `{shared_workspace_root}`,
`{home}`, and `{repo_dirname}`. vaws-top keeps its existing default:

`{home}/vaws-worktrees/{repo_dirname}/npu-fleet-monitor`

Do not relocate an existing user checkout.

Coordinator-only facts (`tree`, `pinned_mirrors`, `arrival_blobs`) live under
`extensions` and stay byte-identical to the previous top-level values.

`identity.canonical_origin` is true on all four pins: a non-canonical origin
is reported as `wrong_origin` (usable, always warned). `false` would drop
origin from identity and never emit `wrong_origin`.

## Loader

`.agents/lib/vaws_dependency.py` is the only identity check.

| Call | Meaning |
|---|---|
| `load_pin(name)` | validate one pin; raise `DependencyPinError` with a field path |
| `all_pins()` | every tracked pin |
| `checkout_path(name)` | resolved path and source (`env` or `default`) |
| `inspect(name)` | never raises; `state` is one of the six values below |
| `resolve(name, required=True)` | execution path; identity drift still returns the path |
| `bootstrap(name)` | clone at the pin. Private pins fall back to `gh repo clone` |

`inspect()` assigns exactly one state, in this order:

| state | Meaning |
|---|---|
| `missing` | path does not exist |
| `not_git` | path exists, `git rev-parse HEAD` failed |
| `wrong_origin` | git checkout, origin does not match `url` |
| `incomplete` | git checkout, origin fine, one or more `identity.required_files` absent. `problems` names exactly which |
| `off_pin` | everything present, `HEAD != commit` |
| `ready` | everything present, `HEAD == commit` |

A directory of required files that is not a git checkout is `not_git`. A real
git checkout that is missing a required file is `incomplete`, never `not_git`.

## Resolve vs warn

| Class | States | `resolve()` | `status` |
|---|---|---|---|
| Unusable → blocks | `missing`, `not_git`, `incomplete` | raises `DependencyUnavailable` (incomplete names the missing files and the bootstrap command) | exit 1 |
| Usable but not the pinned identity → warns | `off_pin`, `wrong_origin` | returns the path | exit 1 unless acknowledged |

`VAWS_DEPS_ALLOW_OFF_PIN` (comma-separated names, or `*`) acknowledges both
identity-drift states. It downgrades the status exit to 0 for the named deps.
`vaws_deps.py doctor` records those names under `acknowledged_drift`. The
escape hatch is visible, not hidden.

Existing locators stay as thin wrappers:

* `vaws_remote_dev.remote_dev_root` / `checkout_status` / `looks_like_checkout`
* `vaws_coordinator.coordinator_root` / `checkout_status`
* `manage_monitor` locate / ensure
* `knowledge_kit.resolve_kit_root` (still requires an explicit kit path)

`resolve()` is the runtime gate. `manage_monitor.py ensure` is a clone-time
decision and stays stricter: it still refuses to clone or reset into a
wrong-origin or off_pin directory. Status and locate may report those trees;
ensure will not write into them.

## Pin drift

Drift is never an execution gate. A developer checkout that is `off_pin`, or a
fork that is `wrong_origin`, still runs. That matches
`docs/coordinator-consumption.md` and the user's "能跑但是提醒就行" decision.

Drift is never silent. `remote_dev.py status`, `vaws.py status`,
`manage_monitor.py status`, and `vaws_deps.py status` exit 1 when an inspected
dep is drifted, and the payload names the mismatch.

In CI (`CI` is set), `test_pin_matches_the_configured_checkout` fails on
commit mismatch. Locally it may skip. A fork at the pinned commit is
`wrong_origin`, not a commit mismatch.

## Capability report

`python3 .agents/scripts/vaws_deps.py doctor` is the first Result Envelope v1
producer. The report lives in `extensions.capability_report`.

Capabilities:

| Capability | Needs |
|---|---|
| `remote_endpoints` | remote-dev in a usable state (`ready`, `off_pin`, or `wrong_origin`) |
| `resolver_registration` | remote-dev, the tracked plugin file, and `REMOTE_DEV_RESOLVERS` in tracked MCP config |
| `task_pool` | vaws-coordinator |
| `host_npu_authority` | vaws-coordinator checkout usable and `host/vaws_npu_coordination.py` present |
| `fleet_observation` | vaws-top |
| `shared_knowledge` | the shared knowledge cache (same inspector as the knowledge client) |
| `conformance_kit` | vaws-knowledge checkout |

A usable-but-drifted checkout is `available` and `degraded`. The capability
report carries a `degradation[]` entry. Unusable states are not available.

Each capability uses the knowledge-client degradation fields: `layer`,
`detail`, `effect`, `remedy`, `expected_source_repo`, `expected_source_ref`.
`shared_knowledge` reuses that client's shared-layer entry.

`resolver_registration` names the layer:

* remote-dev missing or unusable → `dependency`
* plugin file missing → `scaffold`
* `REMOTE_DEV_RESOLVERS` absent from tracked `.mcp.json` / `.cursor/mcp.json` → `client_config`

The client_config effect is: only host+port endpoints resolve; `--machine` /
`--session-id` / worktree auto-bind silently unavailable. The check is static.
It does not import the substrate.

Envelope outcomes: `success` when nothing is degraded, `partial` when
degraded, `failure` when a pin file is invalid. Exit 0 / 1 / 2. Progress on
stderr; one JSON object on stdout.

## Hooks

`remote_dev.py hook`, `vaws.py hook`, and `.agents/hooks/vaws_session.py`
still exit 0 when the dep is unusable. A missing substrate must not block the
client's own tools. Identity-drifted checkouts still exec.

They now name the lost capability and the exact bootstrap command on stderr,
and append one line to
`<shared_workspace_root>/.vaws-local/deps-degradation.log`:

`ISO-time hook=<name> dep=<name> state=<state>`

The ledger keeps the last 200 lines. `doctor` surfaces the last 10 as
`recent_hook_degradations`.

## Commands

```
python3 .agents/scripts/vaws_deps.py status [name...]
python3 .agents/scripts/vaws_deps.py bootstrap <name|all> [--dest] [--reset] [--dry-run]
python3 .agents/scripts/vaws_deps.py doctor
```

`--reset` fetches and checks out the pin only when the working tree is clean.
An `off_pin` dest is never reset without that flag. An `incomplete` checkout,
like `off_pin`, is never rewritten without `--reset`. A `wrong_origin` dest is
never overwritten by bootstrap; choose a different dest.

`--dry-run` prints one JSON object of planned destinations and does not
touch the network.

`bootstrap all` continues past failures and prints one object keyed by
dependency name. It exits 0 when every public pin succeeded, even if a
private pin was `access-denied`. It exits 1 only when a public pin failed.

## Bootstrap

These four repositories are not git submodules. `git submodule update
--init --recursive` does not fetch them. A clone that only initializes
submodules looks complete and still has no remote endpoints, task pool,
fleet observation, or shared knowledge.

| Repository | Visibility | Organization access | Capability |
|---|---|---|---|
| `remote-dev` | private | required | `remote_endpoints`, `resolver_registration` |
| `vaws-coordinator` | public | not required | `task_pool` |
| `vaws-top` | private | required | `fleet_observation` |
| `vaws-knowledge` | public | not required | `conformance_kit` |

`repo-init` and the README quick-start offer:

```
python3 .agents/scripts/vaws_deps.py bootstrap all
python3 .agents/scripts/vaws_deps.py doctor
```

Installing the four is optional and skippable. A user without organization
access can finish `repo-init` successfully. The two public pins clone; the
two private pins report `access-denied` with remedy `gh auth login`. That
is a documented, non-fatal outcome.

After a public-only bootstrap, `doctor` reports `task_pool` and
`conformance_kit` as available. `remote_endpoints`, `resolver_registration`,
and `fleet_observation` stay degraded until the private checkouts exist.
`shared_knowledge` is the local cache of the knowledge corpus, not the
kit checkout; import it separately if you need the shared layer.

Name capabilities from `doctor`'s report. Do not re-derive them.

## Service API compatibility

Each provider checkout may declare the APIs it speaks in `service-api.json`
at the checkout root:

```json
{"schema_version": 1, "name": "vaws-coordinator", "service_api_version": 1, "supports": [1]}
```

Each pin may accept a closed integer range via optional `service_api`:
`{"min": 1, "max": 1}`. `inspect()` reads the file offline (no process
spawn) and attaches a `service_api` sub-result. That fact is orthogonal to
the identity state machine (`ready` / `off_pin` / `wrong_origin` /
`incomplete` / …).

| `service_api.state` | Meaning | Policy |
|---|---|---|
| `compatible` | any value in `supports` intersects `[min, max]` | no extra effect; `off_pin` + `compatible` stays the existing warn-only drift |
| `incompatible` | `supports` misses the accepted range | every capability that depends on that pin is degraded/unavailable with remedy `bump the pin or update the checkout to a build whose supports includes {min}..{max}`; `resolve()` still returns the path |
| `undeclared` | file missing, malformed JSON, or no usable `supports` | warning only; capability stays available if the checkout is otherwise usable |

Example `vaws_deps.py status` excerpt (flat name → inspect map):

```json
{
  "vaws-coordinator": {
    "state": "ready",
    "service_api": {
      "declared": 1,
      "supports": [1],
      "accepted": {"min": 1, "max": 1},
      "state": "compatible"
    }
  }
}
```
