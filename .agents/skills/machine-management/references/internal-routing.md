# Internal Routing: Machine Management

Public contract: `../SKILL.md`

Use this file only after the public `machine-management` contract has been selected.

## Internal Delegation

### Public Action -> Internal Contract

- `machine-management` -> `machine-runtime`, `storage-root`, `code-parity`, `runtime-environment`

### Internal Contract -> Backend

- `machine-runtime` -> `tools/lib/fleet.py::add_fleet_server()`, `tools/lib/fleet.py::verify_fleet_server()`, `tools/lib/fleet.py::list_fleet()`, and `tools/lib/remote.py::{ensure_runtime,verify_runtime}`
- `storage-root` -> current remote runtime bootstrap and persisted runtime state in `tools/lib/remote.py` and `tools/lib/runtime.py`
- `code-parity` -> `tools/lib/code_parity.py::verify_code_parity()` and `tools/lib/code_parity.py::ensure_code_parity()`
- `runtime-environment` -> `tools/lib/runtime_env.py::ensure_runtime_environment()`

## Action Routing

- `attach this machine`
  - internal contracts: `machine-runtime`, `storage-root`
  - backend entrypoints: `tools/lib/fleet.py::add_fleet_server()` and `tools/lib/remote.py::ensure_runtime()`
- `check whether this machine is ready`
  - internal contracts: `machine-runtime`, `code-parity`, `runtime-environment`
  - backend entrypoints: `tools/lib/fleet.py::verify_fleet_server()`, `tools/lib/code_parity.py::verify_code_parity()`, `tools/lib/runtime_env.py::ensure_runtime_environment()`
- `remove this machine`
  - internal contracts: `machine-runtime`
  - backend entrypoints: current machine removal and teardown flow under `tools/lib/reset.py` and persisted machine state in `tools/lib/runtime.py`

## Internal State Touched

- `.workspace.local/servers.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/state.json`
- `.workspace.local/targets.yaml`

## Related Tests

- `tests/test_vaws_fleet.py`
- `tests/test_vaws_target.py`
