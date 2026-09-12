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

`python .agents/scripts/vaws_deps.py sync` runs uv with an automatically selected
environment under `.vaws-local/venvs/<sys.platform>`. Windows uses `win32` and WSL
uses `linux`, so a shared checkout retains both installations. `uv.lock` records
the resolved commits.
CI validates the lock by running `vaws_deps.py sync --locked`. Do not copy those
SHAs into workflows.

Sources may select release tags or validated commit revisions; `uv.lock`
records their resolved commits. Read exact installed/locked identities through
`vaws_deps.py status` instead of maintaining a second SHA table. Acceptance
uses installed packages, including their public APIs and packaged data.

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
`python .agents/scripts/vaws_deps.py sync`.

## Commands

```bash
python3 .agents/scripts/vaws_deps.py status
python3 .agents/scripts/vaws_deps.py doctor
python3 .agents/scripts/vaws_deps.py sync
```

`status` inspects only the three `pyproject.toml` packages. `vaws-top` is
not a package and is not part of `status` or its exit code.

`status` and `doctor` print one JSON object on stdout. Progress goes to
stderr. `doctor` is Result Envelope v1 (`vaws.result-envelope.v1`). That
envelope is not `remote-dev.result.v1`. A missing `uvx` degrades
`fleet_observation`; the remedy is
`python3 .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py deploy`.

`sync` is the bootstrap and works before packages are installed. It forwards uv
options such as `--locked`, `--group dev`, cache placement and offline mode. It
sets `UV_PROJECT_ENVIRONMENT` itself. An ordinary command never installs packages
as a side effect.

After a successful install, `sync` runs the installed knowledge package's
`prepare --project ROOT` command. This prepares the model and local index before
normal use. The JSON retains the dependency install result and reports
`knowledge.status` and `knowledge.ready` separately. Pending knowledge does not
change a successful dependency install's exit code or block ordinary tools.

Entry scripts select the platform environment when their packages are missing.
An explicitly supplied interpreter with usable packages is respected. Interpreter
flags and `-m` module calls survive re-execution; native Windows launches use UTF-8
and retain child-process ownership. A missing installation returns the bootstrap
command as its remedy. Client setup selects the interpreter for each MCP/hook
entry. In a checkout shared by Windows and WSL, managed task entries use the
installed Windows coordinator and Windows paths so both clients share one owner.
Same-drive Kimi project MCP entries use a project-relative Windows interpreter
path, which launches from either platform; other entries use absolute paths.
Run `vaws_client_setup.py --client CLIENT --project PATH --apply` to generate the
platform configuration. Known generated task entries from this checkout's old
`.venv` are migrated, while custom launchers and policy remain unchanged.

Use the platform Python launcher (`py -3` on Windows, `python3` on WSL) for the
bootstrap or workspace entry scripts. The [Windows installation guide](windows-installation.md)
also covers same-filesystem caching and offline transfer.

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
explicit retry or configuration change, `python3 .agents/scripts/knowledge_setup.py`
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
