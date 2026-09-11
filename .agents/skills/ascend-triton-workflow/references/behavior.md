# Behavior

Carry an operator through development, validation and optimization when the request spans those stages.

The config contains op_name, source, target, cases and required_stages. One report call verifies stage scope, actual artifacts, passing cases and kernel identity. Missing or unrelated evidence cannot complete the workflow. Stage identifiers and linking are internal.

Choose the stages required by the requested outcome. Reuse relevant existing evidence. Development owns implementation, validation owns the correctness matrix, and optimization owns measured tuning decisions.

For only one stage, use its owning skill directly.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
