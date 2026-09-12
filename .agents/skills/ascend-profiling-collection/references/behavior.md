# Capture behavior and result interpretation

The collection entry opens a torch-profiler window, sends the requested workload,
closes the window, exports rank data and returns observed results. Coordinator
owns managed service execution; remote-dev owns explicit endpoint I/O. Collection
leaves a supplied live execution running and stops a service it started itself.
On a workload exception it closes the profile window it opened, including when
using an existing service.

## Workload and control

| Mode | Forwarded serving option |
|---|---|
| `enforce_eager` | `--enforce-eager` |
| `full_decode_only` | `--compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY"}'` |
| `piecewise_graph` | `--compilation-config '{"cudagraph_mode":"PIECEWISE"}'` |

Zero speculative tokens omits speculative configuration. Positive values pass
`method` and `num_speculative_tokens`. Use the active vLLM version's supported
mode and method for the actual case.

Profiler control has its own timeout because all ranks participate in setup and
finalization. Workload requests have a separate timeout. The default wave uses
10 requests at concurrency 5 followed by a short tail request for a steady decode
sample. Choose token counts and concurrency for the question being investigated.
`workload_status` records wave success rate, its configured threshold and the tail
request result. A failed workload does not prove representative steady load.

Requests use a temporary local forward owned by remote-dev. Image encoding uses
Pillow on Windows, macOS and Linux and preserves the source image. Multimodal
payloads are built locally. `--api-server-count 1` can help isolate profiler
control routing when investigating an empty capture.

## Export and coverage

| Export | Requested output | Verification |
|---|---|---|
| `db` (default) | `Constant.Db` | A nonempty `ascend_pytorch_profiler_*.db` for each observed rank |
| `text` | `Constant.Text` | `kernel_details.csv` and `trace_view.json` exist |
| `both` | Both exports | Both text files plus a nonempty DB |

DB export avoids large text artifacts when they are unnecessary. The constant
comes from the installed container's torch_npu profiler package. An unsupported
export is reported with its actual rank error; use text export when that runtime
requires it. Existing usable output can be analysed without another capture.

Collection compares observed rank count with the requested topology. The result
retains each rank's output paths and `analysis_status`; `missing_kernel_details`
also covers a missing/empty DB in DB mode, with `expected_output_kind` identifying
the expected format. A failed overall collection means the requested workload or
coverage was not established. Surviving rank data can still answer narrower or
failure-diagnosis questions. Do not discard it or automatically re-collect.

Inspect capture paths, rank logs and export errors to distinguish absent device
records from a failed post-processing step. Re-running `analyse()` can recover an
export failure; it cannot recreate device events that were never recorded.
Re-collect only when the missing evidence matters to the current question.

The workflow waits internally for the post-stop flush window. Per-rank export is
bounded by the analysis timeout and parallelism options. Its result reports
collection and export timing; the Agent need not repeat output checks already
performed by the tool.

## Artifacts and lifecycle

A fresh local directory under `.vaws-local/ascend-profiling-collection/runs/`
contains the manifest; large profiler artifacts remain in the container. Optional
`--archive-dir` copies verified outputs near the data and reports archive failures
separately. The manifest records actual task/execution identity when resolved;
service names are labels, not task identifiers.

Status and cleanup address the same task-owned execution reference. A stop error
retains the relevant reference for recovery. When only profile export is missing,
reuse the capture rather than allocating another service to repeat successful
work. Knowledge remains optional context and adds no collection prerequisite.
