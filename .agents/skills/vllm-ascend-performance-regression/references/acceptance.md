# Maintainer verification

Run the affected tests in `../tests/` with the local test runner. Exercise the
public call with business inputs and actual result fixtures, including incomplete
or mismatched evidence. Verify that conclusions do not exceed the observed scope.
These checks belong to implementation maintenance, not a per-task Agent checklist.

The business config names baseline.sources and candidate.sources (actual vllm and vllm-ascend worktrees), benchmark options, runs, warmups and thresholds. The collector binds each source, waits for its managed service, warms each launch, alternates A/B order, records runtime observations, and releases owned executions. --results accepts existing measurement files for report-only use. Missing runtime evidence yields an inconclusive report.
