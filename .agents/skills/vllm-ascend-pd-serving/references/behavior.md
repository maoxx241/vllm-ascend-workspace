# Behavior

Start and inspect a prefill/decode deployment as one coordinator-owned topology.

The business config contains services, connector, proxy and smoke workload; group_id and startup_order are optional. status and stop accept --service or --execution-id without a local lifecycle file. status may take --config for proxy health; smoke takes --config. Coordinator owns resource state and teardown.

Choose prefill/decode roles, parallelism, connector options and proxy routing from the deployment requirement. A successful HTTP response proves request handling; KV transfer needs connector-specific evidence.

Use ordinary serving for a colocated service. Route rank/connector hangs to distributed-debug.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
