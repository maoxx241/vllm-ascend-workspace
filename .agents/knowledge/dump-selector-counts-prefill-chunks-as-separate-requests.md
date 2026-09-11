# A dump selector that counts forward calls treats one chunked-prefill request as several requests and dumps the wrong unit of work

Status: historical, unverified. Confidence: low.

Imported from the project note dated 2026-09-07. The source did not provide a complete reproducible evidence chain. Claims of verification in the historical description are not current support guarantees.

Known conditions and unknown dimensions are preserved below. Recheck actual code, model configuration and runtime facts before applying this note.

## Avoidance

Before analyzing any dump, read label, match_index, occurrence and metadata from the manifest and confirm the selected unit is the intended request. Do not assume 'first match' means 'first request'.

## Fingerprints

- dump match index selects prefill chunk not request
- same request id multiple dump matches num_computed_tokens
- dumped tensors do not match failing request

## Resolution

Key the selector on the request id and count distinct labels, not calls. ascend-tensor-dump splits this into DUMP_PROBE_MATCH (which distinct label) and DUMP_PROBE_OCCURRENCE (which call within that label), and refuses to re-arm a label it already dumped.

## Root cause

Chunked prefill calls the model forward once per chunk for a single request. Selecting the Nth forward call therefore selects the Nth chunk, not the Nth request. With a 7680-token chunk size, the first three 'matches' were all the first request.

## Symptom

The dump manifest looks structurally valid but its metadata does not match the request under investigation: successive 'matches' all carry the same request id with increasing num_computed_tokens, and the dumped tensors do not correspond to the failing generation. An entire dump round is wasted.

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
    "range": {
      "min": null,
      "max": null
    }
  },
  "component": {
    "values": [
      "tensor-dump-instrumentation",
      "dump-arming-selector"
    ]
  }
}

## Provenance

Source: vllm-ascend-workspace/vllm-ascend-workspace; legacy identifier: dump-selector-counts-prefill-chunks-as-separate-requests; first observed: 2026-09-07.
