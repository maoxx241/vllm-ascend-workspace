# Internal Routing: Workspace Init

Public contract: `../SKILL.md`

Use this file only after the public `workspace-init` contract has been selected.

## Internal Delegation

### Public Action -> Internal Contract

- `workspace-init` -> `foundation`, `git-profile`, `machine-runtime`, `storage-root`, `code-parity`, `runtime-environment`

### Internal Contract -> Backend

- `foundation` -> `tools/lib/preflight.py::ensure_local_control_plane_deps()` and `tools/lib/foundation.py::run_foundation()`
- `git-profile` -> `tools/lib/git_profile.py::git_profile()` and `tools/lib/repo_targets.py::resolve_repo_targets()`
- `machine-runtime` -> `tools/lib/fleet.py::add_fleet_server()`, `tools/lib/remote.py::ensure_runtime()`, and `tools/lib/remote.py::verify_runtime()`
- `storage-root` -> current remote runtime bootstrap flow in `tools/lib/remote.py` plus persisted runtime state in `tools/lib/runtime.py`
- `code-parity` -> `tools/lib/repo_targets.py::resolve_repo_targets()`, `tools/lib/code_parity.py::verify_code_parity()`, and `tools/lib/code_parity.py::ensure_code_parity()`
- `runtime-environment` -> `tools/lib/runtime_env.py::ensure_runtime_environment()`

## Action Routing

- `prepare this repo for development`
  - internal contracts: `foundation`, `git-profile`, `machine-runtime`, `storage-root`
  - backend entrypoints: `tools/lib/init_flow.py::run_init()` plus the delegated modules above
- `first-time Git setup and first-machine setup`
  - internal contracts: `git-profile`, `machine-runtime`, `code-parity`, `runtime-environment`
  - backend entrypoints: `tools/lib/init_flow.py::run_init()`, `tools/lib/code_parity.py::ensure_code_parity()`, `tools/lib/runtime_env.py::ensure_runtime_environment()`

## Internal State Touched

- `.workspace.local/state.json`
- `.workspace.local/repos.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/servers.yaml`
- `.workspace.local/targets.yaml`

## Related Tests

- `tests/test_vaws_init.py`
- `tests/test_vaws_init_bootstrap.py`
