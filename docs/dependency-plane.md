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

`default_checkout` is a template. Placeholders are `{shared_workspace_root}`,
`{home}`, and `{repo_dirname}`. vaws-top keeps its existing default:

`{home}/vaws-worktrees/{repo_dirname}/npu-fleet-monitor`

Do not relocate an existing user checkout.

Coordinator-only facts (`tree`, `pinned_mirrors`, `arrival_blobs`) live under
`extensions` and stay byte-identical to the previous top-level values.

## Loader

`.agents/lib/vaws_dependency.py` is the only identity check.

| Call | Meaning |
|---|---|
| `load_pin(name)` | validate one pin; raise `DependencyPinError` with a field path |
| `all_pins()` | every tracked pin |
| `checkout_path(name)` | resolved path and source (`env` or `default`) |
| `inspect(name)` | never raises; `state` is `missing`, `not_git`, `wrong_origin`, `off_pin`, or `ready` |
| `resolve(name, required=True)` | execution path. `ready` and `off_pin` both return the path |
| `bootstrap(name)` | clone at the pin. Private pins fall back to `gh repo clone` |

Identity is uniform: `git rev-parse HEAD` must work, `identity.required_files`
must exist, and when `identity.canonical_origin` is true the origin URL must
match `url`. A directory of empty required files is `not_git`.

Existing locators stay as thin wrappers:

* `vaws_remote_dev.remote_dev_root` / `checkout_status` / `looks_like_checkout`
* `vaws_coordinator.coordinator_root` / `checkout_status`
* `manage_monitor` locate / ensure (still refuses `off_pin` on ensure)
* `knowledge_kit.resolve_kit_root` (still requires an explicit kit path)

## Pin drift

Drift is never an execution gate. A developer checkout that is `off_pin` still
runs. That matches `docs/coordinator-consumption.md`.

Drift is never silent. `remote_dev.py status`, `vaws.py status`,
`manage_monitor.py status`, and `vaws_deps.py status` exit 1 when an inspected
dep is `off_pin`, and the payload names the mismatch.

`VAWS_DEPS_ALLOW_OFF_PIN` (comma-separated names, or `*`) downgrades that
status exit to 0 for the named deps. `vaws_deps.py doctor` records the names
under `acknowledged_drift`. The escape hatch is visible, not hidden.

In CI (`CI` is set), `test_pin_matches_the_configured_checkout` fails on
mismatch. Locally it may skip.

## Capability report

`python3 .agents/scripts/vaws_deps.py doctor` is the first Result Envelope v1
producer. The report lives in `extensions.capability_report`.

Capabilities:

| Capability | Needs |
|---|---|
| `remote_endpoints` | remote-dev `ready` or `off_pin` |
| `resolver_registration` | remote-dev, the tracked plugin file, and `REMOTE_DEV_RESOLVERS` in tracked MCP config |
| `task_pool` | vaws-coordinator |
| `fleet_observation` | vaws-top |
| `shared_knowledge` | the shared knowledge cache (same inspector as the knowledge client) |
| `conformance_kit` | vaws-knowledge checkout |

Each capability uses the knowledge-client degradation fields: `layer`,
`detail`, `effect`, `remedy`, `expected_source_repo`, `expected_source_ref`.
`shared_knowledge` reuses that client's shared-layer entry.

`resolver_registration` names the layer:

* remote-dev missing → `dependency`
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
still exit 0 when the dep is missing. A missing substrate must not block the
client's own tools.

They now name the lost capability and the exact bootstrap command on stderr,
and append one line to
`<shared_workspace_root>/.vaws-local/deps-degradation.log`:

`ISO-time hook=<name> dep=<name> state=<state>`

The ledger keeps the last 200 lines. `doctor` surfaces the last 10 as
`recent_hook_degradations`.

## Commands

```
python3 .agents/scripts/vaws_deps.py status [name...]
python3 .agents/scripts/vaws_deps.py bootstrap <name|all> [--dest] [--reset]
python3 .agents/scripts/vaws_deps.py doctor
```

`--reset` fetches and checks out the pin only when the working tree is clean.
An `off_pin` dest is never reset without that flag.
