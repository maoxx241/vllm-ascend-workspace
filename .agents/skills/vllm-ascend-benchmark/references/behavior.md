# Behavior

Measure a vLLM service with one or several benchmark iterations and return raw and normalized metrics.

Use --execution-id to measure an existing service, or let the workflow start and clean up its own service. --serve-args and --bench-args forward business options; --preset supplies reusable defaults. The managed interpreter and actual launch observations are recorded with measurements.

Choose input/output lengths, concurrency, request rate and endpoint for the intended workload. User choices override presets and nightly examples. Report variance and failures alongside throughput and latency.

Use performance-regression for code comparisons: it binds actual local worktrees and handles alternating runs. Use correctness-validation for accuracy claims.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
