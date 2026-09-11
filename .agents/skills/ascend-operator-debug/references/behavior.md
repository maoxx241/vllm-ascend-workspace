# Behavior

Reduce a reproduced failure to one operator and compare its actual outputs against a trusted reference.

The config contains operator identity, tolerance and cases. Result files contain observed case metrics or failures. The report computes coverage and classification; absent cases remain inconclusive.

Keep dtype, shape, physical layout, strides and eager/compile/graph mode explicit in the business cases. Prefer the smallest input that still reproduces the failure. A passing isolated call supports that call only; a model-level fix needs a model rerun.

Use ascend-tensor-dump while the first divergent stage is unknown. Use the Triton skills for a Triton candidate.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
