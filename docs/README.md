# Documentation index

Status: current

Every Markdown file under `docs/` is listed here. `Status: current` is a
contract. `Status: dated` is evidence and is never a direction.

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
- [repo-boundaries.md](repo-boundaries.md) — current consumer-side boundary contract and guard summary.
- [tracked-leak-guard.md](tracked-leak-guard.md) — tracked-file leak scanner, hook, and CI.
- [tracked-path-guard.md](tracked-path-guard.md) — anti-rot guard against dead in-tree paths in tracked docs.

## Dated evidence (do not follow as direction)

- [audits/access-family.md](audits/access-family.md) — 2026-09-07 access and infrastructure family audit.
- [audits/measurement-family.md](audits/measurement-family.md) — 2026-09-07 measurement and analysis family audit.
- [audits/operator-triton-family.md](audits/operator-triton-family.md) — 2026-09-07 operator and Triton family audit.
- [audits/repo-boundaries-2026-09-07.md](audits/repo-boundaries-2026-09-07.md) — 2026-09-07 boundary snapshot, 71-row inventory, and historical plan.
- [audits/split-reconciliation-2026-09-07.md](audits/split-reconciliation-2026-09-07.md) — retired split-ledger record; 26/26 arrived.
- [audits/run-deliver-family.md](audits/run-deliver-family.md) — 2026-09-07 run-and-deliver family audit.
- [audits/verdict-debug-family.md](audits/verdict-debug-family.md) — 2026-09-07 validation/debug family audit.
- [deterministic-core-maturation.md](deterministic-core-maturation.md) — first hardware pass of the maturation harness.
- [leak-remediation.md](leak-remediation.md) — leak-exposure report; not an authorization to rewrite history.
