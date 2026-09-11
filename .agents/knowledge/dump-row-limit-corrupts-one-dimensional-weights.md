# A row limit meant to bound token count destroys 1-D weights in a captured operator input set, so the replay fails or compares the wrong thing

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Avoidance

When a captured input set is meant to be replayable, verify the captured shapes against the manifest's recorded shapes before replaying. A parameter whose captured shape equals the row limit is corrupt, not small.

## Fingerprints

- aclnnAddRmsNorm failed error code 561103 gamma shape
- shape of gamma should be equal to the last 1 dim of x1
- captured weight shape equals dump row limit

## Resolution

Apply the row limit only to tensors with 2 or more dimensions and keep 1-D tensors whole. Weights are small, so this costs nothing in dump size.

## Root cause

Row limiting slices dim 0. For an activation that is the token axis and slicing is harmless; for a 1-D weight or per-channel parameter dim 0 is the only axis, so the limit truncates the parameter itself.

## Symptom

Standalone replay of a captured operator call fails with a shape complaint against a parameter rather than an activation, for example aclnnAddRmsNorm error 561103 reporting gamma [8] against x1 [1, 1024]. The manifest records the true shapes, so nothing looks wrong until the replay runs.

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
    "values": [
      "2.10.0.post4"
    ]
  },
  "vllm": {
    "range": {
      "min": null,
      "max": null
    }
  },
  "vllm_ascend": {
    "values": [
      "0.19.1rc2"
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
    "range": {
      "min": null,
      "max": null
    }
  },
  "component": {
    "values": [
      "tensor-dump-instrumentation",
      "captured-tensor-row-limiting"
    ]
  }
}

## Provenance

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: dump-row-limit-corrupts-one-dimensional-weights; first observed: 2026-09-07.
