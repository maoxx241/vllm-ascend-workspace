Status: current

# Dependency plane

This scaffold consumes three extracted repositories as **installed packages**,
not git checkouts, not submodules, and not vendored copies. `uv.lock` is the
only pin. A fourth repository, `vaws-top`, is a uvx service and is not an
import.

## Packages

`pyproject.toml` declares the three in-process packages. `[tool.uv.sources]`
must name git+https tag sources because `vaws-coordinator` depends on
`vaws-remote-dev`, which is not on PyPI.

| Package | Module | Source tag | Role |
|---|---|---|---|
| `vaws-remote-dev` | `remote_dev` | `0.5.0` at `d0f963c` | process-in import + MCP server |
| `vaws-coordinator` | `vaws_coordinator` | `v0.3.1` | process-in import + stdio MCP |
| `vaws-knowledge` | `vaws_knowledge` | `0.3.0` at `222402b` | process-in import + MCP |
| `vaws-top` | — | uvx only | fleet dashboard; not imported |

`uv sync` writes `.venv` and records the resolved git commits in `uv.lock`.
CI runs `uv lock --check`. Do not copy those SHAs into workflows.

The workspace consumes these release tags through the lockfile. Acceptance
uses the installed packages, including their public APIs and packaged data.

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
`uv sync`.

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

`sync` wraps `uv sync`. It never runs automatically from an entry script.

Equivalent form that creates the environment first:

```bash
uv run python3 .agents/scripts/vaws_deps.py doctor
```

System `python3` cannot see `.venv`. Entry scripts re-exec
`.venv/bin/python` when a sentinel package is missing and the venv exists.
`VAWS_SKIP_VENV_REEXEC=1` disables the hop. A missing `.venv` is an error
whose remedy is `uv sync`.

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

The knowledge **engine** and its **corpus** both ship in the installed
`vaws-knowledge` package. After `uv sync`, `shared_knowledge` is available
and query reads `vaws_knowledge.corpus`. The remedy for a missing corpus is
`uv sync`. Do not clone the commons or import YAML by hand.

## What was removed

- hand-written pin JSON files and the dependency-v1 schema
- git checkout states and locator helpers
- former checkout-root environment variables and the off-pin override
- the `bootstrap` subcommand
- the local launcher that shadowed the `remote_dev` package name
