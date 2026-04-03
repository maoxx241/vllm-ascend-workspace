# Agent Tooling Foundation

## Scope

This document defines the Phase 1 contract that all future atomic tools, discovery manifests, and skill recipes must follow.

Phase 1 does not rewrite shared skill bodies. It defines the contract that later skill rewrites must implement.

## Atomic Tool Result Contract

Required fields:

- `status`
- `observations`

Optional fields:

- `reason`
- `side_effects`
- `next_probes`
- `retryable`
- `idempotent`
- `payload`

Rules:

- probe tools may return only the required fields
- mutation-capable tools must declare `side_effects`
- empty boilerplate fields are forbidden
- output must be valid JSON-compatible data
- `payload` is reserved for compact machine-readable handles and summaries such as `service_id`, `run_id`, `result_path`, or endpoint coordinates
- `payload` must be a JSON-compatible mapping, not an arbitrary scalar or list

## Action Kinds

- `probe`: observe without mutation
- `repair`: targeted mutation intended to restore an already-known capability
- `bootstrap`: first-time setup or materialization
- `cleanup`: explicit teardown or residue removal
- `execute`: intentional workload execution that may mutate runtime or produce durable outputs but is not itself a repair, bootstrap, or cleanup flow

## Recipe Rules

- recipes are advisory, not mandatory
- recipes must name stop conditions
- recipes must name escalation points
- recipes may branch when later choices depend on live observations
- recipes must be expressed in agent-visible files, not hidden workflow code

## Discovery Guarantees

- a fresh agent must be able to find the right tool family without reverse-engineering `tools/lib/*.py`
- a family manifest must say whether each tool is probe-only or mutation-capable
- a family manifest must list the tool path, output contract, and common next probes

## Phase 1 Exit Bar

- the repository contains one tracked contract document for atomic-tool behavior
- the repository contains one tracked discovery tree with at least one working family manifest
- the repository contains at least two probe-only reference atomic tools
- the repository contains a tracked inventory of load-bearing `tools/lib/remote.py` behaviors
- do not edit shared skills in phase 1 beyond adding non-behavioral discovery pointers in adapter files

## vaws Namespace Decision

`vaws` remains a thin compatibility namespace during Phase 1 and Phase 2.

Rules:

- do not add new workflow ownership to `vaws`
- do not hide multi-step repair flows behind new `vaws` verbs
- future atomic tools may exist outside `vaws`
- Phase 4 decides whether `vaws` survives after wrapper collapse
