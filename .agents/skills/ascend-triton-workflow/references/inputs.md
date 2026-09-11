# Business input example

This is an illustrative configuration shape. Select cases and values for the
actual task. The tool generates report metadata internally; observed output
files are produced by the relevant execution or measurement harness.

```json
{
  "op_name": "softmax",
  "source": {
    "kind": "gpu-triton",
    "path": "/src/softmax.py"
  },
  "target": {
    "soc": "Ascend910B2"
  },
  "required_stages": [
    "development",
    "validation"
  ]
}
```
