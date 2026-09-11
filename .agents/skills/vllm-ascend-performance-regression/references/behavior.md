# Behavior

Run or analyze a controlled baseline-versus-candidate serving experiment and apply metric-specific regression thresholds.

The business config names baseline.sources and candidate.sources (actual vllm and vllm-ascend worktrees), benchmark options, runs, warmups and thresholds. The collector binds each source, waits for its managed service, warms each launch, alternates A/B order, records runtime observations, and releases owned executions. --results accepts existing measurement files for report-only use. Missing runtime evidence yields an inconclusive report.

Choose the workload and metrics that reflect the user-visible change. Set threshold and direction per metric. Keep non-code conditions comparable and inspect variance, outliers and failed requests before attributing a delta to code.

For a single-state throughput measurement use benchmark. For root-cause timing attribution use profiling collection and analysis.

Progress is written to stderr; stdout contains the structured result. Remote
device execution uses coordinator ownership. Local report construction does not
allocate devices or alter an execution. Reports describe the supplied evidence;
missing evidence is not a passing result.
