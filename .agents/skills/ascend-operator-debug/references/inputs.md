# Business input example

This is an illustrative configuration shape. Select cases and values for the
actual task. The tool generates report metadata internally; observed output
files are produced by the relevant execution or measurement harness.

```json
{
  "operator": {
    "name": "npu_example",
    "invocation": "torch_npu.npu_example(x)",
    "reference": "torch_ref(x.cpu())"
  },
  "tolerance": {
    "atol": 0.001,
    "rtol": 0.001
  },
  "source_model_failure": "model output diverges",
  "cases": [
    {
      "id": "fp16-eager",
      "mode": "eager",
      "inputs": [
        {
          "name": "x",
          "shape": [
            2,
            4
          ],
          "strides": [
            4,
            1
          ],
          "dtype": "float16",
          "layout": "ND"
        }
      ],
      "attributes": {
        "transpose": false
      }
    },
    {
      "id": "fp16-graph",
      "mode": "graph",
      "inputs": [
        {
          "name": "x",
          "shape": [
            2,
            4
          ],
          "strides": [
            4,
            1
          ],
          "dtype": "float16",
          "layout": "ND"
        }
      ],
      "attributes": {
        "transpose": false
      }
    }
  ]
}
```
