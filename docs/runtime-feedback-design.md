# Runtime progress and diagnostics

Status: current

A status result should show the current operation, recent progress, loaded
runtime identity, waiting/failure evidence and where to read more. Owners
maintain these facts; Agents do not reconstruct management records.

## Ownership and observations

Coordinator owns execution state, role progress and resource release. Remote-dev
owns connection/process observations and remote results. Business skills add
HTTP readiness, inference checks and measurement outcomes. Knowledge and fleet
observation do not supervise executions.

Process state, business readiness and resource release are distinct. A running
process may still be loading; a stopped process does not by itself prove resource
release or a successful business check. Reports retain the observations actually
made, and do not move a completed record backwards when an older observation arrives.

Preparation, source publication and build steps retain progress and original
stdout/stderr references. Reading a preparing execution does not wait for its
build or restart it. A quiet log alone does not establish a deadlock. Errors keep
the original category and message; uncertain attribution remains unknown.

## Runtime identity and updates

A process records the package version, available Git commit, Python path and PID
it loaded. Later installed metadata is a separate observation. Doctor and daemon
status distinguish those identities; an updated installation does not imply a
running process reloaded it.

Observation and cleanup remain available during version drift. Coordinator's
restart-if-idle operation checks active execution and resource state itself.
Native clients own MCP process restarts. Updating a runtime does not justify
killing a shared SSH master or guessing which unrelated PID should stop.

## Business requests

Readiness checks expose the actual target, proxy mode and failure category.
Internal service requests use explicit direct connections; callers can select
environment proxy use for destinations that require it. These choices do not
change global proxy settings. A health-check failure does not replay a generation
request or prove a model defect.

Where a serving tool validates engine arguments, it runs the current remote CLI's
parser in the selected environment before loading weights. It does not maintain
a stale local whitelist of vLLM flags. The owning tool retains the original error.

## Compact results and readback

Python APIs retain complete records. Agent-facing task MCP/CLI responses offer a
compact view and the full record reference; explicit full/target/tail operations
expose detail when needed. Omitted arrays retain counts, and truncated logs keep
readable references. Error evidence has priority over repeated environment data.
A failed record write is reported without inventing a reference or hiding the
operation result.

SSH diagnostics retain effective connection settings, timeout budget, duration,
exit and uncertainty about remote completion. A timeout does not prove the remote
command never ran. Read-only connection probes can compare connection modes;
they never replay the original business command. Proxy credentials remain out of
summaries and records.

Affected behavior is checked in the owning package or business suite. Reuse
existing machine evidence when it still applies; the ordinary [local test
runner](local-tests.md) handles control-plane checks without a mandatory hardware
or full-rebuild sequence.
