# Documentation index

Status: current

Every file under `docs/` is listed here. `Status: current` is a
contract. A dated Status line is evidence and is never a direction; Git
is the archive for dated evidence.

## Current contracts

- [runtime-feedback-design.md](runtime-feedback-design.md) — real-machine progress, loaded runtime identity, state projection, compact output and connection diagnostics.
- [README.md](README.md) — this index.
- [agent-first-openviking-spec.md](agent-first-openviking-spec.md) — target behavior and implementation status for permissive Agent workflows, local knowledge, public contributions and prebuilt distribution; Grok review is deferred.
- [agent-feedback-contract.md](agent-feedback-contract.md) — Result Envelope v1: the JSON stdout contract for agent-facing commands.
- [cli-surface.md](cli-surface.md) — current CLI inventory and the unimplemented historical thirteen-command proposal.
- [comparability-certificate.md](comparability-certificate.md) — observational comparability certificate for paired measurements.
- [coordinator-consumption.md](coordinator-consumption.md) — how this scaffold consumes the installed vaws-coordinator package.
- [dependency-plane.md](dependency-plane.md) — `uv.lock` is the only pin; `vaws_deps.py status|doctor|sync`. Covers required `uv sync` and capability reporting.
- [npu-fleet-monitor.md](npu-fleet-monitor.md) — local deploy and lifecycle of the standalone vaws-top fleet monitor.
- [property-testing.md](property-testing.md) — property-based tests for the deterministic cores.
- [remote-dev-consumption.md](remote-dev-consumption.md) — how this scaffold consumes the installed vaws-remote-dev package.
- [target-state.md](target-state.md) — the single definition of the post-split end state: axioms, ownership matrix, and cross-repository contracts.
- [tracked-leak-guard.md](tracked-leak-guard.md) — tracked-file leak scanner, hook, and CI.
- [tracked-path-guard.md](tracked-path-guard.md) — anti-rot guard against dead in-tree paths in tracked docs.

## Dated validation evidence

- [windows-validation-2026-09-11.md](windows-validation-2026-09-11.md) — completed Windows non-NPU validation, repairs, limits and a proposed experience-optimization sequence.
