---
name: ascend-profiling-analysis
description: Analyze existing Ascend profiler databases, kernel_details.csv, traces or communication summaries for step, layer, operator and cross-rank timing. Use when profiler data is available and timing analysis or a profiling report is requested. New trace collection uses profiling-collection; a slow-service report without traces does not by itself select this analyzer.
---

# ascend-profiling-analysis

Analyze existing Ascend profiler data and produce evidence-linked step, layer, operator and cross-rank findings.

Tie findings to actual rank/time/row evidence and retain uncertainty in model structure or hardware context. Use config.json or verified profile-visible evidence for model dimensions. Knowledge provides optional context; it does not override current measurements, configuration or source evidence. Missing references do not block analysis. Deterministic classification and validation belong in the analyzer code or policy data with tests.

Report generation preserves existing reference links and performs no knowledge
lookup or index startup. The Agent can query the knowledge MCP tools separately
when a finding warrants more context.

## Agent entry

Run from the repository root using the platform's Python launcher. The workspace
selects its installed platform environment automatically.

```text
python .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py --manifest collection/manifest.json
```

Use --remote-profile-root for an existing root and profile_sweep.py for multiple roots. The normal fast mode returns analysis_summary.json and compact report artifacts. --mode full adds detailed HTML/XLSX outputs. Remote parsing keeps large traces near their storage; explicit remote endpoints and execution references select the analysis target.

Use profiling-collection only when new traces are needed. HBM component attribution belongs to memory-profiling.

Read the relevant detail only when needed:

- [behavior](references/behavior.md)
