# Business input example

This is an illustrative configuration shape. Select cases and values for the
actual task. The tool generates report metadata internally; observed output
files are produced by the relevant execution or measurement harness.

```json
{
  "op_name": "softmax",
  "mode": "gpu-migration",
  "source": {
    "kind": "gpu-triton",
    "path": "/src/softmax.py"
  },
  "reference": {
    "path": "/src/ref.py"
  },
  "target": {
    "soc": "Ascend910B2"
  },
  "tolerances": {
    "float16": {
      "atol": 0.001,
      "rtol": 0.001
    }
  },
  "cases": [
    {
      "id": "case-1",
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
      ]
    }
  ]
}
```
