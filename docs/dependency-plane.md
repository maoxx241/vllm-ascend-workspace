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
CI runs `uv lock --check`. Do not copy those SHAs into workflows.

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

Entry scripts select the platform environment when their packages are missing.
An explicitly supplied interpreter with usable packages is respected. Interpreter
flags and `-m` module calls survive re-execution; native Windows launches use UTF-8
and retain child-process ownership. A missing installation returns the bootstrap
command as its remedy. Client setup writes the selected interpreter's full path
into native MCP/hook configuration.

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
| `conformance_kit` | `vaws-knowledge` |

## Shared knowledge corpus

The installed `vaws-knowledge` package provides the engine and a bootstrap
corpus. Run `python3 .agents/scripts/knowledge_setup.py` after `python .agents/scripts/vaws_deps.py sync` to enable
the public Markdown corpus, a personal contribution fork and background Release
updates. `--read-only` enables downloads without GitHub authentication or a fork.
Then refresh selected clients with `vaws_client_setup.py --apply` so MCP receives
`.vaws-local/knowledge/service.json` and supported final-response hooks.

While MCP is alive, the package submits redacted public copies and consumes
GitHub Releases from `vllm-ascend-workspace/vaws-knowledge-corpus`. Shared updates
verify the exact Git identity, model files and dense OVPack before switching;
project and candidate knowledge stay local. Knowledge PRs currently require
human review and merge. Grok review and automatic merging are deferred.
Use `vaws-knowledge publishing status --config PATH` to inspect retries and the
active sync result. Native hook trust remains managed by each client.

## What was removed

- hand-written pin JSON files and the dependency-v1 schema
- git checkout states and locator helpers
- former checkout-root environment variables and the off-pin override
- the `bootstrap` subcommand
- the local launcher that shadowed the `remote_dev` package name

## Optional package skill

`knowledge` package skill through its configured interpreter (`python -m vaws_knowledge skill`) reads the installed curation skill
without starting OpenViking. `--install-dir <client-skill-directory>` installs
that same packaged resource for native discovery. Workspace does not keep a
second canonical copy or require curation for ordinary capture.

Doctor also reads the running coordinator identity without launching a daemon. Its loaded version/commit can differ from the installed package after sync; use `vaws-coordinator daemon --action restart-if-idle` after owned executions and leases finish. Task MCP responses carry their own startup identity; refresh their native-client process separately when stale. Missing loaded identity remains unknown.
