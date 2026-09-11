# Behavior

Compare inference outputs across code or execution configurations with explicit comparability and numerical criteria.

The remote_correctness_harness.py payload captures offline runtime observations from the managed execution. Online/AISBench results use the server execution reference through aisbench_adapter.py. The comparison derives metadata from actual outputs, emits its certificate and report, and reports missing identity as inconclusive.

Select deterministic prompts or token IDs, sampling, model and topology that exercise the change. Token equality and dataset task metrics answer different questions. Declare only the intended varying dimensions with --allowed-difference.

Route an eager-passes/graph-fails reproduction to graph-debug, a rank-dependent failure to distributed-debug, and a reduced operator failure to operator-debug.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
