# Adding a device-to-host copy or synchronization during graph capture changes the behavior being validated; graph dumps must copy device-to-device and read back after replay

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Avoidance

Do not gate a graph-internal copy on a runtime flag: the copy node is baked in at capture time. If the only way to obtain data is to force the dumped forward onto the eager path, state explicitly that the resulting conclusion does not cover the graph path. Health check 503 during a 15-20 minute capture is not a failure.

## Fingerprints

- d2h during graph capture changes behavior
- python probe not executed during graph replay
- aclgraph capture dump synchronize

## Resolution

Pre-allocate buffers at module construction time, copy into them with copy_ inside the graph so the copy node is captured, and synchronize plus read back outside the graph after replay. ascend-tensor-dump exposes this as graph_slot plus capture_graph, and finish() returns without reading when the stream is capturing.

## Root cause

Two distinct effects. A host read-back inside the capture window becomes part of the captured graph and alters its execution. Separately, replay does not re-enter Python, so Python-level probes and print statements never run during replay and cannot prove anything about graph-internal state.

## Symptom

Graph-mode validation either cannot reproduce the divergence it is meant to measure, or the instrumented graph behaves differently from the production graph. Python-level probes appear to record nothing during decode.

## Recorded conditions

{
  "soc": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "cann": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "driver": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "python_abi": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "torch": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "torch_npu": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "vllm": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "vllm_ascend": {
    "values": [
      "v0.26.0rc"
    ]
  },
  "model": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "topology": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "execution_mode": {
    "values": [
      "aclgraph"
    ]
  },
  "component": {
    "values": [
      "graph-mode-instrumentation",
      "graph-internal-dump"
    ]
  }
}

## Provenance

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: graph-capture-device-to-host-probe-changes-measured-behavior; first observed: 2026-09-07.
