# Single-Node Replica Serving Runbook

## Scope
- Explicit `vaws serving start/status/stop` flow for one machine and one `vllm serve` process.

## Standard Environment
- Managed machine is `container_access=ready`.
- `serving` preset defines `ASCEND_RT_VISIBLE_DEVICES`, port, served-model alias, and serve args.

## Inputs
- `--server-name`
- `--preset`
- `--weights-path`

## Run
- `vaws serving start --server-name <name> --preset <preset> --weights-path <remote-model-path>`
- `vaws serving status <service-id>`
- `vaws serving stop <service-id>`

## Validation
- `/health`
- `/v1/chat/completions`

## Known Pitfalls
- Reuse is exact-fingerprint only.
- Device bindings must not overlap.
