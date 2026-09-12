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

Preserve extra remotes, dirty sources and user choices. Keep `.gitmodules` on
community upstream URLs. All development forks belong to personal GitHub User
accounts, with personal `origin` and official `upstream`. Task identity and remote resources belong to the
native attachment and coordinator, so workspace setup needs no machine username
or alias questionnaire.

## Complete the relevant setup

- Establish GitHub authentication when the requested GitHub operation needs it.
  Initialize submodules recursively before configuring their remotes; Git in an
  empty submodule directory can otherwise resolve to its parent repository.
- For requested CI-pinned alignment, use `resolve_vllm_ci_pin.py` and report the
  source of the ref. Preserve dirty submodules and existing intentional pins.
- Configure development forks with the general entry
  `.agents/scripts/workspace_forks.py --github-user USER --apply`. This command
  works without the skill or installed runtime packages; omitting `--apply`
  returns a read-only plan. On first setup ask only for the personal GitHub ID,
  using the authenticated login as a suggestion. Reuse a saved confirmation on
  subsequent runs. The command verifies the authenticated account, personal
  ownership and official fork network before rewiring remotes. It preserves
  correctly configured fetch/push protocol splits and extra remotes; conflicting
  or multiple primary URLs require an explicit replacement with a local backup.
  Existing business branches, commits and dirty files stay in place.
- For missing package dependencies, run
  `uv run --no-project python .agents/scripts/vaws_deps.py sync`. Reuse a ready
  environment. `doctor` is available for unresolved capability or pin questions;
  it is not an extra step after an already conclusive result.
- For first initialization, run
  `.agents/scripts/vaws_client_setup.py --client all --apply` once. It detects
  installed clients, prepares hooks/providers and supported native defaults,
  and records completed changes and remaining native actions in the primary
  worktree's `.vaws-local/client-initialization.json`. Complete those native
  actions during initialization using available client tools or computer use;
  do not defer discovery to the first business task or silently call wiring a
  mode selection. The record is not a recurring task gate. Explicit single-client
  repair still uses `--client CLIENT --apply`. For Codex/Cursor, the native
  Worktree mode/environment choice is separate from writing setup files.
  New worktree setup and session attachment then run through the client without
  an Agent launcher call. See the [client boundaries](../../../docs/native-workspace-isolation.md).
  Codex's `--codex-global-hooks` option installs a fixed user hook scoped to
  this Git worktree family. Review its native hook definitions once during
  initialization; configuration generation does not grant trust. The same
  definitions serve later worktrees and read each directory's saved environment.
  Cursor's `--cursor-global-mcp` option installs the fixed VAWS providers once
  in the user configuration, avoiding separate project MCP setup for every new
  directory. Select New Worktree as its default environment. Claude uses WorktreeCreate in native worktree mode. Grok uses
  its native Git worktree preference and the project Git creation callback. Kimi
  requires an explicitly installed SessionSetup extension for automatic directories.
  Configuration files alone do not prove environment selection or enabled MCPs;
  one small real task can verify the requested setup. Preserve unrelated client
  configuration and native hook trust.

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

The saved `.vaws-local/github.json` is only client configuration. Server identity,
resource ownership and authorization remain the coordinator's responsibility.
