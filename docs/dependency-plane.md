Status: current

# Dependency plane

This scaffold consumes three extracted repositories as **installed packages**,
not git checkouts, not submodules, and not vendored copies. `uv.lock` is the
only pin. A fourth repository, `vaws-top`, is a uvx service and is not an
import.

## Packages

`pyproject.toml` declares the three in-process packages. `[tool.uv.sources]`
names public git+https sources because `vaws-coordinator` depends on
`vaws-remote-dev`, which is not on PyPI.

| Package | Module | Required version | Role |
|---|---|---|---|
| `vaws-remote-dev` | `remote_dev` | from `pyproject.toml` | process-in import + MCP server |
| `vaws-coordinator` | `vaws_coordinator` | from `pyproject.toml` | process-in import + stdio MCP |
| `vaws-knowledge` | `vaws_knowledge` | from `pyproject.toml` | process-in import + MCP |
| `vaws-top` | — | uvx only | fleet dashboard; not imported |

`uv run --no-project python .agents/scripts/vaws_deps.py sync` prepares a locked,
immutable environment in the operating system's user data directory. Its key
includes dependency inputs, Python identity, platform, architecture and selected
groups/extras. Workspaces with identical inputs reuse that environment; changing
dependencies prepares a new one. `uv.lock` records the resolved commits.
CI validates the lock with `vaws_deps.py sync --packages-only --locked --group dev`. Do not copy those
SHAs into workflows.

Sources may select release tags or validated commit revisions; `uv.lock`
records their resolved commits. Read exact installed/locked identities through
`vaws_deps.py status` instead of maintaining a second SHA table. Acceptance
uses installed packages, including their public APIs and packaged data.

The [workspace updater](forks-and-updates.md) consumes this exact
combination from the official default branch. It invokes that revision's sync
entry in an isolated checkout and prepares its pinned vaws-top wheel.
Preparation happens independently of native task startup; existing clients
and services keep their pins. Component releases do not trigger unrelated
per-component upgrades.

## Loader

`.agents/lib/vaws_dependency.py` answers three questions. It does not run
git.

| Call | Meaning |
|---|---|
| `required_versions()` | exact versions from `pyproject.toml` |
| `locked_packages()` | version + commit from `uv.lock` |
| `installed_spec(name)` | version + commit from `importlib.metadata` / `direct_url.json` |
| `inspect(name)` | never raises; `state` is one of the three values below |

`inspect()` assigns exactly one state:

| state | Meaning |
|---|---|
| `missing` | not installed in this interpreter, or pyproject/lock cannot describe it |
| `off_spec` | installed, but version or commit does not match pyproject / lock |
| `ready` | installed version and commit match the lock |

`off_spec` warns but does not block execution. `missing` makes capabilities
that depend on the package unavailable. The remedy for every package gap is
`uv run --no-project python .agents/scripts/vaws_deps.py sync`.

## Commands

```bash
uv run --no-project python .agents/scripts/vaws_deps.py status
uv run --no-project python .agents/scripts/vaws_deps.py doctor
uv run --no-project python .agents/scripts/vaws_deps.py sync
```

`status` inspects only the three `pyproject.toml` packages. `vaws-top` is
not a package and is not part of `status` or its exit code.

`status` and `doctor` print one JSON object on stdout. Progress goes to
stderr. Doctor defaults to a compact projection with a reference to its complete
Result Envelope v1; `--full` returns the full record. A missing `uvx` degrades
`fleet_observation`; the remedy is
`uv run --no-project python .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py deploy`.

`sync` is the bootstrap and works before packages are installed. It accepts
`--locked`, groups/extras, `--python`, cache placement, link mode and offline
options; unsupported flags and mutable local dependencies fail explicitly. It
installs at the final path under a per-key OS lock and publishes the ready receipt
last. The selected base Python and store paths are resolved to physical paths so
an interpreter alias change cannot replace a running client's dependencies.
An ordinary command reads the ready receipt and never installs packages.

After a successful install, `sync` runs the installed knowledge package's
`prepare --project ROOT` command. This prepares the model and local index before
normal use. The JSON retains the dependency install result and reports
`knowledge.status` and `knowledge.ready` separately. Pending knowledge does not
change a successful dependency install's exit code or block ordinary tools.

For package installation alone, `sync --packages-only` skips knowledge model and
index preparation. The result records that preparation was not requested; it
does not infer whether an existing knowledge instance is ready. Local CI uses
this mode because its knowledge tests use in-memory or mocked owners. Ordinary
sync still prepares knowledge, and real provider readiness is checked separately.

Entry scripts select a prepared platform environment. Interpreter flags and `-m`
module calls survive re-execution; native Windows launches use UTF-8 and retain
child-process ownership. A missing installation returns the bootstrap command
as its remedy. Native client setup pins each MCP/hook process to its ready
receipt, so later syncs do not change its imported dependencies.

In a checkout shared by Windows and WSL, managed tasks and knowledge use the
prepared Windows owner. A managed CLI switches owner before reading stdin or
performing work; local analysis and explicit endpoint I/O stay native. Same-drive
Kimi project MCP entries use a permanent per-environment junction with a relative
Windows interpreter path. Run `uv run --no-project python
.agents/scripts/vaws_client_setup.py --client CLIENT --project PATH --apply` to
generate configuration; managed entries retain custom fields and foreign
launchers. The [platform contract](platform-contract.md) describes the common
native client entry. The [Windows installation guide](windows-installation.md)
covers caching and offline transfer.

## Capabilities

`.agents/lib/vaws_capability.py` keeps these capabilities. A
`missing` package makes the capabilities that list it unavailable.
`fleet_observation` is not a package: it needs `uvx` plus the `vaws-top`
release wheel.

| Capability | Depends on |
|---|---|
| `remote_endpoints` | `vaws-remote-dev` |
| `task_pool` | `vaws-coordinator` |
| `host_npu_authority` | `vaws-coordinator` |
| `fleet_observation` | `uvx`, `vaws-top` |
| `shared_knowledge` | `vaws-knowledge` (importable, with packaged corpus) |

## Shared knowledge corpus

The installed `vaws-knowledge` package provides the engine and a bootstrap
corpus. Dependency installation prepares the local model and index. For an
explicit retry or configuration change, `uv run --no-project python .agents/scripts/knowledge_setup.py`
uses the same package preparation entry. New setup enables local knowledge and
shared downloads; it does not create a fork or enable public contribution.
Existing publishing configuration is preserved. `--contribute` explicitly enables
authorized contribution; `--read-only` disables contribution while keeping shared
downloads. A repository change alone preserves the existing contribution choice.
Then refresh selected clients with `vaws_client_setup.py --apply` so MCP receives
`.vaws-local/knowledge/service.json` and supported final-response hooks.

Knowledge MCP starts its internal model/index maintenance while alive, independent
of public contribution. Shared synchronization is enabled by default and consumes
GitHub Releases from `vllm-ascend-workspace/vaws-knowledge-corpus`. Shared updates
verify the exact Git identity, model files and dense OVPack before switching;
project and candidate knowledge stay local. Knowledge PRs currently require
human review and merge.
Only configured, authorized public contribution submits redacted public copies.
Use `vaws-knowledge publishing status --config PATH` to inspect retries and the
active sync result. Native hook trust remains managed by each client.

The [knowledge contract](target-state.md#54-knowledge) keeps lookup and capture
optional and uses ordinary Markdown. This setup is not a prerequisite or a
maintenance sequence for ordinary tasks; a knowledge outage does not block
independent development.

For one Windows-mounted workspace, knowledge MCP, preparation and summary hooks
use its Windows interpreter and native paths from both Windows and WSL. A missing
Windows interpreter leaves preparation pending instead of starting another
Linux database process in the shared state directory. An independent Linux
workspace uses its Linux environment. Existing generated knowledge launchers
migrate to this owner; custom launchers and storage choices are preserved.

All five clients use the knowledge MCP tools. Configured Codex, Claude Code,
Cursor and Grok adapters reuse their native final-response text. Kimi Code
currently supplies no final text in `Stop`, so it receives MCP/session wiring
without automatic summary capture. No client needs a second summary or transcript
scan to complete a task; the package's publishing documentation records the
event fields and native sources.

## What was removed

- hand-written pin JSON files and the dependency-v1 schema
- git checkout states and locator helpers
- former checkout-root environment variables and the off-pin override
- the `bootstrap` subcommand
- the local launcher that shadowed the `remote_dev` package name

## Optional package skill

`python -m vaws_knowledge skill` through the configured interpreter reads the optional maintenance skill
without starting OpenViking. `--install-dir <client-skill-directory>` installs
that same packaged resource for native discovery. Workspace does not keep a
second canonical copy or require curation for ordinary capture.

Doctor also reads the running coordinator identity without launching a daemon. Its loaded version/commit can differ from the installed package after sync; use `vaws-coordinator daemon --action restart-if-idle` after owned executions and leases finish. Task MCP responses carry their own startup identity; refresh their native-client process separately when stale. Missing loaded identity remains unknown.
