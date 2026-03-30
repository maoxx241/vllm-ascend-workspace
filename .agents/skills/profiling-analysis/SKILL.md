---
name: profiling-analysis
description: Public workspace-local guidance for analyzing profiling artifacts.
---

# Profiling Analysis

Use this skill when reviewing profiling output from the workspace runtime.

- Focus on device idle gaps, host-side blocking, exposed AICPU, and comm-wait pollution.
- Interpret profiling artifacts in the context of the active session under `/vllm-workspace`.
- Keep analysis notes in workspace-local locations, not tracked public docs.
- Avoid embedding private hosts, tokens, or local-only paths in shared guidance.
- Prefer concise findings that map directly to the active feature session.

This is workspace-local reference material only.
