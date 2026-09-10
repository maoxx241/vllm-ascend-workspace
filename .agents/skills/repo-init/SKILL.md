---
name: repo-init
description: Initialize this workspace or repair its dependency, fork and native-client configuration. Use for workspace setup requests; a standalone Git or package command does not require the full workflow.
---

# Initialize the workspace

Reuse existing configuration and complete the requested setup. A broad init
covers project identity, submodules, dependency installation, client wiring
and knowledge setup. A narrow request changes only the relevant part.

Start with `scripts/repo_init_probe.py --compact` to inspect the current state.
Use `python3` on POSIX or `py -3` on Windows before the environment exists;
installed package commands can use `uv run python` on either platform.

## Decisions that matter

- Preserve existing remotes, push URLs, extra remotes, dirty sources and user
  choices. Initialize submodules before configuring their remotes; an empty
  submodule directory otherwise resolves Git commands to the parent repo.
- A missing machine username belongs to local initialization, not machine
  provisioning. Use an already supplied username directly. If the choice is
  missing, the profile helper offers the Git username, random or custom options.
  Ask for a custom literal only when the user has not provided it.
- Resolve a missing alias only when relevant; a persisted choice of no alias
  is complete. Do not turn helper question payloads into repeated confirmations.
- Keep `.gitmodules` on community upstream URLs. Personal forks are development
  remotes. Preserve an existing topology unless its change is requested.

## Execute the requested setup

1. Reuse or configure the local identity with `repo_init_profile.py` and
   `.agents/scripts/workspace_profile.py`. Provisioning and remote environment
   preparation belong to coordinator, not this skill.
2. Establish the requested GitHub auth and submodules. For CI-pinned alignment,
   use `resolve_vllm_ci_pin.py` and report its source; do not overwrite a dirty
   submodule to enforce a default.
3. Configure missing or explicitly requested forks/remotes with
   `repo_topology.py`. Do not rewrite established fetch/push settings.
4. Run `uv sync` for package-dependent work and inspect capabilities with
   `python3 .agents/scripts/vaws_deps.py doctor`.
5. For authorized broad init, run `.agents/scripts/knowledge_setup.py` and the
   selected client's `.agents/scripts/vaws_client_setup.py --apply` entry.
   A read-only knowledge setup can use `--read-only`; it does not require NPU
   or machine configuration. Setup preserves unrelated client settings and
   does not bypass native hook trust.

Known choices and existing authorization are reused throughout. Auth/offline
failures leave independent local work usable. Do not make an optional fleet
monitor or public knowledge review a prerequisite for ordinary development.

Report the configuration changes, source/version combination when changed,
capabilities from doctor, and any unresolved setup choice. No second summary
or per-category approval checklist is required.

Read only the relevant reference for less common operations:

- [Configuration behavior](references/behavior.md)
- [Command recipes](references/command-recipes.md)
- [Setup acceptance](references/acceptance.md)
