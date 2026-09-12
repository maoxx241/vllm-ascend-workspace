# Documentation index

Status: current

Every file under `docs/` is listed here. `Status: current` is a
contract. Dated validation evidence records the tested state; it is not a current
command recipe. Superseded designs and duplicate inventories are retained in Git.

## Current contracts

- [platform-contract.md](platform-contract.md) — common Windows/macOS/Linux entry points, literal process arguments, native owners and immutable environments.
- [native-workspace-isolation.md](native-workspace-isolation.md) — independent native editing directories, exact Git state copying, attachment ownership and fixed local dependency environments.

- [design-principles.md](design-principles.md) — nine governing principles; total Agent task cost takes priority, tools stay bounded, knowledge is advisory, and valid work is reused.
- [windows-installation.md](windows-installation.md) — PowerShell setup, same-filesystem uv cache and verified offline transfer.
- [local-tests.md](local-tests.md) — local test progress, subprocess lifetime, retained evidence and validated retries.
- [runtime-feedback-design.md](runtime-feedback-design.md) — real-machine progress, loaded runtime identity, state projection, compact output and connection diagnostics.
- [README.md](README.md) — this index.
- [agent-feedback-contract.md](agent-feedback-contract.md) — Result Envelope v1: the JSON stdout contract for agent-facing commands.
- [comparability-certificate.md](comparability-certificate.md) — observational comparability certificate for paired measurements.
- [coordinator-consumption.md](coordinator-consumption.md) — how this scaffold consumes the installed vaws-coordinator package.
- [dependency-plane.md](dependency-plane.md) — `uv.lock` is the only pin; `vaws_deps.py status|doctor|sync`. Covers immutable preparation and capability reporting.
- [npu-fleet-monitor.md](npu-fleet-monitor.md) — local deploy and lifecycle of the standalone vaws-top fleet monitor.
- [property-testing.md](property-testing.md) — property-based tests for the deterministic cores.
- [remote-dev-consumption.md](remote-dev-consumption.md) — how this scaffold consumes the installed vaws-remote-dev package.
- [target-state.md](target-state.md) — current component ownership and runtime contracts, including the single minimal knowledge contract: optional reference, plain Markdown and no per-task bookkeeping.
- [tracked-leak-guard.md](tracked-leak-guard.md) — tracked-file leak scanner, hook, and CI.
- [tracked-path-guard.md](tracked-path-guard.md) — anti-rot guard against dead in-tree paths in tracked docs.

## Dated design and validation evidence

- [core-workflow-validation-2026-09-12.md](core-workflow-validation-2026-09-12.md) — fixed execution inputs, native rebuild/reuse/switchback, Windows/WSL clients and real-host validation, with explicit scope limits.
- [workflow-usability-validation-2026-09-11.md](workflow-usability-validation-2026-09-11.md) — skill boundary audit, bounded service startup, source reuse and real four-host fleet lifecycle observations.
- [agent-only-validation-2026-09-11.md](agent-only-validation-2026-09-11.md) — Agent-only entry consolidation and Windows PowerShell/WSL acceptance, including corrections and hardware limits.
- [installation-feedback-2026-09-11.md](installation-feedback-2026-09-11.md) — fresh/cached Windows installation timings, cache relocation and dependency extras decision.
- [observation-feedback-2026-09-11.md](observation-feedback-2026-09-11.md) — SSH batching/timing evidence and status freshness behavior.
- [cli-feedback-2026-09-11.md](cli-feedback-2026-09-11.md) — paired Windows CLI startup measurements and parser side-effect checks.
- [windows-validation-2026-09-11.md](windows-validation-2026-09-11.md) — completed Windows non-NPU validation, repairs and limits at the recorded commits.
