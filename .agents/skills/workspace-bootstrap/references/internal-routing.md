# Internal Routing: Workspace Bootstrap

Public contract: `../SKILL.md`

Use this file only after the public bootstrap contract has been selected.

## Command Mapping

- Primary entrypoint: `tools/vaws.py init --bootstrap`
- Supporting verification command: `tools/vaws.py doctor`

## Internal Inputs

- first remote server details, or explicit local-only intent
- `vllm-ascend` origin URL
- optional `vllm` origin URL
- pre-staged auth handles or safe auth refs

## Internal State Touched

- `.workspace.local/repos.yaml`
- `.workspace.local/auth.yaml`
- `.workspace.local/servers.yaml`
- `.workspace.local/targets.yaml`
- `.workspace.local/state.json`
- git remotes under `vllm/` and `vllm-ascend/`

## Related Tests

- `tests/test_vaws_init_bootstrap.py`
- `tests/test_secret_boundary.py`
