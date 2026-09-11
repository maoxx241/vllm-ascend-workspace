# Behavior

Optimize an already validated Ascend Triton kernel using measured bottlenecks and repeatable latency evidence.

The config contains op_name, kernel and its validation evidence, target, cases, baseline measurements and objective. Round results carry candidate measurements and validation. The report computes KEEP/DISCARD and verifies kernel lineage and case coverage.

Choose one bottleneck hypothesis per round. Consider UB live set, physical cores and MTE/Vector/Scalar overlap. Compare repeated per-shape measurements with a reference baseline; retain a change only when the gain exceeds noise and correctness still covers the candidate.

Use ascend-triton-kernel-validation when correctness is incomplete. Use profiling-analysis for whole-model performance attribution.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
