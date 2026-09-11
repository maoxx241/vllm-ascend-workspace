# Behavior

Validate one Ascend Triton kernel against its reference over the cases needed by its consumers.

The config contains op_name, reference, target, cases and tolerances. The tool checks kernel source for missing launches and computation fallback, combines observed case results, and emits coverage, analysis and a manifest automatically.

Select shapes, dtype, layout, strides, scalar options and execution modes from the operator contract and affected callers. Include boundary and non-contiguous cases where semantics require them. Numerical agreement must come from the launched candidate kernel.

A failing candidate returns to ascend-triton-operator-development. A fully passing matrix can proceed to ascend-triton-kernel-optimization.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
