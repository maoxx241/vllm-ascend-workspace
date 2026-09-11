# A verdict computed only over stages present on both sides reports agreement when one run captured almost nothing

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Avoidance

Read the one-sided stage lists before the metrics. Non-empty lists usually mean the two runs did not execute the same instrumented code, and no numeric conclusion holds until the paths are aligned.

## Fingerprints

- dump diff aligned but only_in_left non-empty
- tensor comparison pass with zero compared items
- eager versus graph dump verdict aligned

## Resolution

Rank coverage asymmetry as its own verdict between clean and divergent, and make the gating flag fail on it. Reserve a clean verdict for runs whose stage-key sets actually match.

## Root cause

Divergence is only defined over the intersection of stage keys, so shrinking the intersection makes the verdict look better. Coverage asymmetry is the stronger signal and it was being discarded.

## Symptom

Comparing an eager dump against a graph dump returns an aligned verdict and exit code 0 while listing over a hundred stages as present on only one side. A tensor comparison with zero comparable pairs likewise reports a pass.

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
    "values": [
      "eager",
      "aclgraph"
    ]
  },
  "component": {
    "values": [
      "dump-comparison",
      "comparison-verdict"
    ]
  }
}

## Provenance

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: dump-comparison-reports-aligned-despite-one-sided-coverage; first observed: 2026-09-07.
