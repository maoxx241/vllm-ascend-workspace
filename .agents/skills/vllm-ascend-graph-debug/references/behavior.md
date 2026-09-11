# Behavior

Locate the first divergence between eager and graph executions or investigate graph compile, capture and replay failures.

Snapshot identity is read from sidecars, or --eager-identity and --graph-identity. The report compares observed identities and samples with finite tolerances, emits the first divergence and a comparability certificate, and retains missing identity as inconclusive. Its conclusion applies to the supplied snapshots.

Fix inputs and compare corresponding stages, ranks and steps. Separate compile, capture and replay; instrumentation that synchronizes the device can change the failure. Use bounded tensor capture only when existing outputs cannot distinguish the hypothesis.

Use correctness-validation to establish the reproduction, tensor-dump to capture intermediate stages, and operator-debug after reducing to one call.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
