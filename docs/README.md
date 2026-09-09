# Documentation index

Status: current

Every file under `docs/` is listed here. `Status: current` is a
contract. A dated Status line is evidence and is never a direction; Git
is the archive for dated evidence.

## Current contracts

- [README.md](README.md) — this index.
- [agent-feedback-contract.md](agent-feedback-contract.md) — Result Envelope v1: the JSON stdout contract for agent-facing commands.
- [cli-surface.md](cli-surface.md) — current CLI inventory and the unimplemented historical thirteen-command proposal.
- [comparability-certificate.md](comparability-certificate.md) — observational comparability certificate for paired measurements.
- [coordinator-consumption.md](coordinator-consumption.md) — how this scaffold consumes the installed vaws-coordinator package.
- [dependency-plane.md](dependency-plane.md) — `uv.lock` is the only pin; `vaws_deps.py status|doctor|sync`. Covers required `uv sync` and capability reporting.
- [npu-fleet-monitor.md](npu-fleet-monitor.md) — local deploy and lifecycle of the standalone vaws-top fleet monitor.
- [property-testing.md](property-testing.md) — property-based tests for the deterministic cores.
- [remote-dev-consumption.md](remote-dev-consumption.md) — how this scaffold consumes the installed vaws-remote-dev package.
- [target-state.md](target-state.md) — the single definition of the post-split end state: axioms, ownership matrix, contracts, deletion inventory, acceptance predicates.
- [tracked-leak-guard.md](tracked-leak-guard.md) — tracked-file leak scanner, hook, and CI.
- [tracked-path-guard.md](tracked-path-guard.md) — anti-rot guard against dead in-tree paths in tracked docs.

## Ledger

- [audits/split-ledger-2026-09-07.json](audits/split-ledger-2026-09-07.json) — machine-readable split-arrival ledger retained for consumer tests.
