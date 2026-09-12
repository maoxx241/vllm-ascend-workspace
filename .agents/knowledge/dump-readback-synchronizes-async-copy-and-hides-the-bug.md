# A dump that synchronizes or copies to host inside the forward pass can complete the async copy the bug depends on, so every dumped value looks correct

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

## Avoidance

Run a probe on/off comparison before trusting any dump: issue the same deterministic request with the probe disabled and enabled and confirm the symptom is identical. If it is not, fix the probe before reading its output. Do not place probes inside submodule forwards that clone, reduce or synchronize on device.

## Search terms

- symptom disappears when dump enabled
- dump synchronize completes non_blocking copy
- instrumented run correct uninstrumented run wrong

## Resolution

Compute statistics on device, accumulate them, and perform exactly one synchronize plus device-to-host transfer after the values already have a consumer, typically once logits exist. ascend-tensor-dump does this in finish() and nowhere else.

## Root cause

Reading device values mid-forward forces a synchronization that closes the race window. Asynchronous copies issued with non_blocking=True may legitimately complete after the forward pass, and the probe silently changes their observed ordering.

## Symptom

With instrumentation enabled the intermediate tensors are all consistent and the end-to-end symptom weakens or disappears; removing the instrumentation brings the symptom back. Investigation drifts toward blaming copy timing while the real defect stays hidden.

## Recorded context

- vllm ascend: v0.26.0rc.
- component: tensor-dump-instrumentation, device-to-host-read-back-placement.

Other environment and version details were not recorded.

## Source

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: dump-readback-synchronizes-async-copy-and-hides-the-bug; first observed: 2026-09-07.
