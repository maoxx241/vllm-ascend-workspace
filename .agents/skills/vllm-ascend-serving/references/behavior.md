# Behavior

Start, inspect or stop one managed single-node vLLM Ascend service.

Start uses one bounded wait across preparation, launch and HTTP/models/first-token
readiness. --health-timeout sets that budget; --no-wait returns the coordinator
receipt immediately. A pending timeout retains the execution reference and is
not a readiness result. Status and stop resolve that same task-owned reference.
Preparation-step changes appear in progress. A terminal start reads its owned
log tail once and includes the import or runtime exception in the result.
Business launch settings are saved per service name after admission succeeds.
Relaunching one named service cannot reuse another service's model or options;
a rejected change leaves the previous settings available.

`--host` before the `--` separator selects the coordinator placement host;
vLLM listening arguments belong after the separator. Explicitly authorized
sharing uses `--allow-external-busy`, one `--devices` card and TP1/DP1. The
coordinator keeps lease, owned process, port and release checks. The default
requires an idle card. Host and sharing settings survive named relaunch;
`--no-allow-external-busy` explicitly restores exclusive admission.
Shared mode takes TP/DP through the wrapper; extra parallel-size options and
vLLM `--config` files are rejected so they cannot override the single-card request.

The managed service process parses vLLM arguments once during startup. There
is no extra remote import solely to parse the same arguments before launch.
An argument or import failure is reported through the owned execution log.

Use serving.py status or serving.py stop with --execution-id or --service. A service reference is resolved by coordinator within the current task. Pending states retain their execution reference. Restart or release follows the requested lifecycle; no separate allocation, parity command or status ledger is needed.

Reuse the native task context and actual business source bindings. Choose model, parallelism and serving options from the request. Resource state and HTTP/models/first-token readiness are separate observations.

An optional `--sources` repository-to-path JSON map selects sources for one
launch; omission keeps native task defaults and `{}` selects no project sources.
The performance collector supplies this internally for each A/B run without
changing the task's default sources.

Use pd-serving for prefill/decode topology, benchmark for measurement, and profiling-collection for profiler-window control.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.

If an import failure comes from a version-conditional plugin branch after a
snapshot install, compare the bound source's release identity with the reported
installed version. A verified release may require the plugin's `VLLM_VERSION`
compatibility setting in `--extra-env`; record that choice in the execution.
Do not infer a release from a model name or change package metadata to conceal
a source/runtime mismatch.
