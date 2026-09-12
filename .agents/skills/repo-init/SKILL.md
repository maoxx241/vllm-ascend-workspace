---
name: repo-init
description: Initialize this workspace or repair its dependency, fork and native-client configuration. Use for workspace setup requests; a standalone Git or package command does not require the full workflow.
---

# Initialize the workspace

Complete the requested setup using existing configuration. Broad initialization
usually needs submodules, dependency installation and a selected native client;
a narrow repair uses only the relevant operation.

Use `uv run --no-project python` before local script paths on Windows, macOS,
Linux and WSL. The bootstrap prepares or reuses an immutable environment without
shell activation. Existing dependency receipts and probe results can be reused.

## Inspect what is missing

`scripts/repo_init_probe.py --compact` reports platform, GitHub authentication,
submodules and remotes without creating an identity or setup choices. Add
`--include-forks` only when personal fork discovery helps the requested topology.
Known state does not require another complete probe.

Preserve established remotes, push URLs, extra remotes, dirty sources and user
choices. Keep `.gitmodules` on community upstream URLs; personal forks are
optional development remotes. Task identity and remote resources belong to the
native attachment and coordinator, so workspace setup needs no machine username
or alias questionnaire.

## Complete the relevant setup

- Establish GitHub authentication when the requested GitHub operation needs it.
  Initialize submodules recursively before configuring their remotes; Git in an
  empty submodule directory can otherwise resolve to its parent repository.
- For requested CI-pinned alignment, use `resolve_vllm_ci_pin.py` and report the
  source of the ref. Preserve dirty submodules and existing intentional pins.
- Configure missing or requested forks/remotes with `repo_topology.py`. Its
  `configure` action sets the specified fetch and push URLs, so use it only when
  that change is intended.
- For missing package dependencies, run
  `uv run --no-project python .agents/scripts/vaws_deps.py sync`. Reuse a ready
  environment. `doctor` is available for unresolved capability or pin questions;
  it is not an extra step after an already conclusive result.
- Run the selected client's `.agents/scripts/vaws_client_setup.py --apply`
  entry. Preserve unrelated client configuration and native hook trust.

Successful dependency setup also prepares knowledge. Pending model/index work
leaves ordinary tools usable. Knowledge MCP maintains itself while alive;
`.agents/scripts/knowledge_setup.py` is for an explicit preparation retry or
configuration change. Default setup uses local knowledge and shared downloads;
`--contribute` enables explicitly requested public contribution. Existing
publishing choices are preserved. Ordinary development requires no knowledge
maintenance sequence or additional summary.

Windows and WSL clients of the same Windows-mounted workspace share its Windows
knowledge process. A missing interpreter leaves knowledge preparation pending;
an independent Linux workspace uses its native environment.

Report the requested configuration changes, relevant verification and unresolved
limitations. Reuse existing authorization; ask only for missing information that
affects the requested result.

Read [command recipes](references/command-recipes.md) for individual operations.
