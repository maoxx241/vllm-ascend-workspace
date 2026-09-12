# Python dump calls placed inside a graph-captured forward stop recording on replay, so a graph-mode dump is silently missing most of its stages

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Avoidance

Never compare an eager dump against a graph dump on stage-count alone, and never gate a graph capture on a runtime armed() check: the copy node is fixed at capture time. Treat a graph-mode manifest whose record count is far below the eager one as missing data, not as agreement.

## Search terms

- graph mode dump manifest far fewer records than eager
- aclgraph replay python capture not executed
- per-layer dump stages missing only in graph mode

## Resolution

For stages inside the graph, pre-allocate a buffer with graph_slot() at module construction time and write it with capture_graph(), which bakes a device-to-device copy into the captured graph. Read the buffers back after replay in finish().

## Root cause

Graph replay executes the recorded device kernels without re-entering Python, so a capture call in the model's forward body only runs during capture, not during replay. Capture points in the model runner remain outside the graph and keep working, which makes the loss look partial rather than systematic.

## Symptom

The same instrumentation that produces a full manifest in eager mode produces a nearly empty one under aclgraph. Only capture points outside the captured region survive. Nothing errors, and the manifest still looks structurally valid.

## Recorded context

- soc: A3.
- torch: 2.10.0.
- torch npu: 2.10.0.post4.
- vllm ascend: 0.19.1rc2.
- execution mode: aclgraph.
- component: tensor-dump-instrumentation, in-forward-capture-points.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: aclgraph-replay-skips-in-forward-python-dump-calls; first observed: 2026-09-07.
