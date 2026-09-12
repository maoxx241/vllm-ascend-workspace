---
name: vllm-ascend-serving
description: Start, check, or stop a single-node vLLM Ascend service through coordinator TaskClient. Use for 拉服务 / 看服务状态 / 停掉服务. Do not use for machine bootstrap or generic remote I/O.
---

# vllm-ascend-serving

Start, inspect or stop one managed single-node vLLM Ascend service.

Reuse the native task context and actual business source bindings. Choose model, parallelism and serving options from the request. Resource state and HTTP/models/first-token readiness are separate observations.

## Agent entry

Run from the repository root. The entry reuses the installed platform environment.

```text
uv run --no-project python .agents/skills/vllm-ascend-serving/scripts/serving.py start --model /models/example --tp 1
```

Use serving.py status or serving.py stop with --execution-id or --service. A service reference is resolved by coordinator within the current task. Pending states retain their execution reference. Restart or release follows the requested lifecycle; no separate allocation, parity command or status ledger is needed.

Start follows preparation and HTTP/models/first-token readiness within
--health-timeout. Use --no-wait when an immediate execution receipt is wanted.
After a bounded wait, continue with the same reference. The CLI reuses its
native context: Codex can resolve its actual thread ID and Claude exports the
context through its session environment. MCP input injection does not imply
shell environment injection; other client boundaries are recorded in
[native context support](../../../docs/native-workspace-isolation.md#context-in-mcp-and-shell).

Use pd-serving for prefill/decode topology, benchmark for measurement, and profiling-collection for profiler-window control.

Use `--host` before `--` to select a coordinator placement host. When the user
authorizes sharing an occupied card, pass one explicit `--devices` card and
`--allow-external-busy` with TP1/DP1. Coordinator still owns the execution and
lease. Relaunch preserves these settings; `--no-allow-external-busy` restores
exclusive admission.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
