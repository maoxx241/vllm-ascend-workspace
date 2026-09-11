# SSH and status observation feedback

Status: dated 2026-09-11

## SSH measurements

On native Windows, ten alternating pairs compared three separate related
read-only OS/cwd/Python queries with one SSH request returning the same facts.
Both arms used independent connections to one Linux endpoint. Facts matched
in every pair; no model/device library was imported and no NPU was allocated.

| Method | Median | Sample p95 |
| --- | ---: | ---: |
| Three independent SSH requests | 13.768 s | 14.167 s |
| One request with three facts | 4.601 s | 4.740 s |

The measured reduction was 67%. Ten samples make p95 the sample maximum;
this is endpoint-specific exploratory evidence, not a network SLA. The
remote-dev connection diagnostic now gathers these facts in one fixed request.
It never replays arbitrary business commands. Existing full context probes
already batch their work in one SSH request.

Three real probes of the new diagnostic succeeded. SSH duration ranged
4.587-4.818 s, received TCP milestone 0.300-0.584 s, and authentication milestone
2.891-3.170 s. The small remote facts query took about 0.012 ms and final
stream drain/exit 5.7-7.1 ms. Connection timing includes client startup and
authentication; TCP is a subset. Pure transfer time and unobserved milestones
remain unknown. The remainder includes channel/Python startup and transport.

These phases come from actual local receive events and a remote monotonic
timer. They are not packet-level timings. Ordinary bash results expose total
transport/decoding costs without turning on verbose SSH. Raw endpoint identities
and receipts are kept only in untracked local output.

## Status freshness

The coordinator task MCP/CLI can reuse a status snapshot for up to two seconds.
Responses expose snapshot completion time, age, source, freshness, per-role
sampling times and whether a busy execution deferred refresh. Use:

```powershell
uv run python .agents/scripts/vaws.py execution --execution-id <id> --context-file <context> --refresh
```

TaskClient's library default continues to request fresh status; `refresh=False`
opts into caching. Tail, target, stop, allocation and background progression
retain their existing behavior. A cached observation grants no resource access.

Tests verify that a second cached read makes zero managed-control calls, expiry
or explicit refresh makes a new call, and busy observations return without
waiting for the execution lock. Ownership, state/error/release fields, compact
output and CLI/IPC argument forwarding remain covered. This removes repeated
status probes but is not a live NPU-service latency benchmark.

## Validation

- remote-dev diagnostics/transport/client parity: 88 passed, 22 platform skips,
  18 subtests, including real local pipe milestones and timeout output.
- Coordinator task-client/ownership/freshness: 38 passed, 6 subtests.
- Coordinator task-server contracts: 18 passed, 8 subtests.

Owner PRs are remote-dev #9 and coordinator #14. Consumer pins select those
changes after owner CI and merge; hosted CI supplies the supported-platform
validation for the submitted revisions.
