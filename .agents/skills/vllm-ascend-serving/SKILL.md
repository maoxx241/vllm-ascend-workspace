---
name: vllm-ascend-serving
description: Start, check, or stop a single-node vLLM Ascend service through coordinator TaskClient. Use for 拉服务 / 看服务状态 / 停掉服务. Do not use for machine bootstrap or generic remote I/O.
---

# vllm-ascend-serving

Start, inspect or stop one managed single-node vLLM Ascend service.

Reuse the native task context and actual business source bindings. Choose model, parallelism and serving options from the request. Resource state and HTTP/models/first-token readiness are separate observations.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/vllm-ascend-serving/scripts/serving.py start --model /models/example --tp 1
```

Use serving.py status or serving.py stop with --execution-id or --service. A service reference is resolved by coordinator within the current task. Pending states retain their execution reference. Restart or release follows the requested lifecycle; no separate allocation, parity command or status ledger is needed.

Use pd-serving for prefill/decode topology, benchmark for measurement, and profiling-collection for profiler-window control.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
