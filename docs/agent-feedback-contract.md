# Agent feedback contract

Status: current

Tools produce compact outcomes and retain detailed evidence so an Agent can
continue from a failure without recreating it. The Agent-only design principles
in [target-state.md](target-state.md) govern this contract.

The complete Result Envelope v1 is implemented by
`.agents/lib/vaws_result_envelope.py` and
`.agents/schemas/result-envelope-v1.schema.json`. It is a tool-generated record,
not an Agent-authored form or an extra prerequisite for native shell/Git work.
The presentation layer may expose a compact result with a readback reference.

The retained record identifies the attempted command, outcome, failure layer,
observed environment, evidence references, bounded previews and any retry or
child-result facts. Environment values that were not observed remain unknown.
Credential values are excluded. Attribution requires evidence; unknown failures
remain low confidence. Truncated previews point to retained text. Fan-out outcome
is derived from its parts, and nested failures retain their originating evidence.
The library and maintainer tests enforce these invariants.

Coordinator is authoritative for task identity, execution phase and resource
release. Business readiness, such as a successful first token, is reported
separately. Reports do not create a second lifecycle ledger. Reports may link
actual Run Manifest artifacts internally; callers provide business inputs and
observed outputs rather than identifiers, transitions or coverage declarations.

Deterministic failure handling belongs to the owning component's code and tests.
Useful contextual explanations may be kept as ordinary Markdown with their
conditions, evidence and uncertainty. Knowledge is optional reference; lookup
returns availability separately from an empty result. Configured capture hooks
reuse ordinary task summaries. Neither lookup nor capture is a completion step,
and no second summary is needed. Public contribution uses only package-prepared
redacted copies.

For implementation details see [coordinator consumption](coordinator-consumption.md),
[dependency selection](dependency-plane.md) and the schema. Envelope lint and local
tests are maintainer tools, not steps inserted into every business task.
