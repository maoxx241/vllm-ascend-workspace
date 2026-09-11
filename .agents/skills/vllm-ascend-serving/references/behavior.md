# Behavior

Start, inspect or stop one managed single-node vLLM Ascend service.

Use serving.py status or serving.py stop with --execution-id or --service. A service reference is resolved by coordinator within the current task. Pending states retain their execution reference. Restart or release follows the requested lifecycle; no separate allocation, parity command or status ledger is needed.

Reuse the native task context and actual business source bindings. Choose model, parallelism and serving options from the request. Resource state and HTTP/models/first-token readiness are separate observations.

Use pd-serving for prefill/decode topology, benchmark for measurement, and profiling-collection for profiler-window control.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
