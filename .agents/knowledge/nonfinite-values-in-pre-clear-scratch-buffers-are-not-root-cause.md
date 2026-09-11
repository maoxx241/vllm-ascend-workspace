# NaN or Inf found in a buffer sampled before it is cleared or overwritten is expected and must not be attributed as the defect

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Avoidance

Do not treat the first non-finite stage in a single dump as the answer. Name the capture point relative to initialization, for example after_clear rather than initial_state, so the manifest cannot be misread later.

## Fingerprints

- nan in initial state before clear
- nan destination before copy expected
- first nonfinite stage also present in passing run

## Resolution

Compare the candidate dump against a known-good request before attributing any non-finite finding, and place capture points after initialization rather than before it. Report the first non-finite stage that a passing run does not also exhibit.

## Root cause

Those stages observe memory that has not been initialized yet for this step. Non-finite content there carries no information about correctness.

## Symptom

A dump scan flags a stage such as an initial state before clear or a destination before copy as the first non-finite stage, and the investigation concludes there is state corruption. A known-good request shows the same non-finite values.

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
    "range": {
      "min": null,
      "max": null
    }
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
      "dump-interpretation",
      "non-finite-value-attribution"
    ]
  }
}

## Provenance

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: nonfinite-values-in-pre-clear-scratch-buffers-are-not-root-cause; first observed: 2026-09-07.
