# Adding a device-to-host copy or synchronization during graph capture changes the behavior being validated; graph dumps must copy device-to-device and read back after replay

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Avoidance

Do not gate a graph-internal copy on a runtime flag: the copy node is baked in at capture time. If the only way to obtain data is to force the dumped forward onto the eager path, state explicitly that the resulting conclusion does not cover the graph path. Health check 503 during a 15-20 minute capture is not a failure.

## Search terms

- d2h during graph capture changes behavior
- python probe not executed during graph replay
- aclgraph capture dump synchronize

## Resolution

Pre-allocate buffers at module construction time, copy into them with copy_ inside the graph so the copy node is captured, and synchronize plus read back outside the graph after replay. ascend-tensor-dump exposes this as graph_slot plus capture_graph, and finish() returns without reading when the stream is capturing.

## Root cause

Two distinct effects. A host read-back inside the capture window becomes part of the captured graph and alters its execution. Separately, replay does not re-enter Python, so Python-level probes and print statements never run during replay and cannot prove anything about graph-internal state.

## Symptom

Graph-mode validation either cannot reproduce the divergence it is meant to measure, or the instrumented graph behaves differently from the production graph. Python-level probes appear to record nothing during decode.

## Recorded context

- vllm ascend: v0.26.0rc.
- execution mode: aclgraph.
- component: graph-mode-instrumentation, graph-internal-dump.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: graph-capture-device-to-host-probe-changes-measured-behavior; first observed: 2026-09-07.
