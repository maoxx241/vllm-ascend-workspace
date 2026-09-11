# Behavior

Implement a first correct Ascend Triton operator or migrate an existing GPU Triton kernel.

The business config contains op_name, mode, source, reference, target, cases and tolerances. The report consumes the actual kernel and validation manifest, checking kernel identity and passing case coverage. Optional --semantic-report and --sketch attach useful design artifacts.

Resolve semantics from the reference and callers before selecting a grid or tile. Separate logical shape from physical layout and reductions. GPU launch assumptions need an Ascend-specific design; choose a simple correct candidate before tuning.

Run ascend-triton-kernel-validation for the candidate. Continue to optimization only after the planned correctness cases pass.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
