# Benchmarking Assets

This directory stores reusable benchmark assets for the public vLLM-Ascend workspace.

## Layout

Use this structure:

```text
benchmarking/
  <task-type>/
    <topic>/
      <model-family>/
```

The first public preset is:

- `serving/gdn/qwen3_5/`
  - Qwen3.5 GDN serving benchmark script
  - runbook for remote execution, result extraction, and common pitfalls
