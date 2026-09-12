# Instrumentation written to a host path that the container does not import runs never, while the sync step still reports success

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Avoidance

Treat 'no probe output at all' as a delivery problem first and a logic problem second. Do not add more instrumentation until the resolved import path has been confirmed.

## Search terms

- probe file written but never executed
- sync success but no dump output
- host workspace path vs container package path

## Resolution

Resolve the tree the service actually imports before writing anything: read the package __file__ inside the container and write the probe next to it. Confirm the file is present at that resolved path.

## Root cause

The service imports the package from a different tree than the one that was written. A host-side workspace path and the container-visible site-packages or editable install path are not the same location.

## Symptom

Parity or file write reports success and the file exists, but the probe produces no output, no log line and no dump directory. Time is then spent debugging the probe logic itself.

## Recorded context

- component: remote-instrumentation-delivery, source-tree-resolution.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: debug-instrumentation-written-to-host-path-invisible-in-container; first observed: 2026-09-07.
