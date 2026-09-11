# Agent entry surface

Status: current

The workspace is consumed by Agents. Each business entry accepts the requested
operation, business inputs and actual evidence. Components handle execution
ownership, waiting and cleanup; report tools handle identifiers, transitions and
artifact assembly. The principles in [target-state.md](target-state.md) govern
entry design. Command count is an observation, not a design target.

Runtime APIs belong to remote-dev, vaws-coordinator, vaws-knowledge and vaws-top.
The workspace retains client wiring and vLLM/Ascend business judgment. Local
maintenance tools are classified separately from business entries and copied
remote payloads. Removed wrappers have no compatibility aliases.

Serving has one start/status/stop entry. Report workflows consume case inputs and
results in one call. Performance comparison binds actual baseline and candidate
worktrees and collects alternating measurements internally. Knowledge uses the
installed package's Markdown APIs. Platform bootstrap selects independent win32
and linux environments in a shared checkout.

The AST inventory and classification catalog are maintainer tools. The table
below is generated from current code; schema and numerical invariants are checked
by tests rather than repeated as instructions in every task.

## Current entries

<!-- current-cli-surface-table -->
| Entry point | Style | Verbs | Options | Refs | Responsibility | Support role | Current target | Proposed |
|---|---|---|---|---|---|---|---|---|
| `.agents/hooks/knowledge_summary.py` | argparse | - | 2 | script:1, test:1 | mechanics | hook | .agents/hooks/knowledge_summary.py | - |
| `.agents/hooks/tracked_leak_precommit.py` | argparse | - | 8 | docs:1, policy:1, test:1 | mechanics | hook | .agents/hooks/tracked_leak_precommit.py | - |
| `.agents/hooks/vaws_session.py` | argparse | - | 4 | policy:1, script:1, test:2 | mechanics | hook | .agents/hooks/vaws_session.py | - |
| `.agents/scripts/cli_surface_inventory.py` | argparse | - | 3 | policy:1, test:3 | mechanics | harness | .agents/scripts/cli_surface_inventory.py | vaws lint |
| `.agents/scripts/envelope_lint.py` | argparse | scan, check, run | 5 | test:3 | mechanics | harness | .agents/scripts/envelope_lint.py | vaws lint |
| `.agents/scripts/knowledge_setup.py` | argparse | - | 2 | docs:1, routing:2, skill-doc:3, test:1 | mechanics | supported | .agents/scripts/knowledge_setup.py | - |
| `.agents/scripts/local_tests.py` | delegated | - | 6 | docs:1, other:1 | mechanics | harness | .agents/scripts/local_tests.py | - |
| `.agents/scripts/repo_boundary_check.py` | argparse | - | 6 | docs:1, other:1, policy:3, script:1, test:3 | mechanics | supported | .agents/scripts/repo_boundary_check.py | vaws lint |
| `.agents/scripts/skill_catalog.py` | argparse | - | 2 | other:1, routing:1, test:2 | mechanics | supported | .agents/scripts/skill_catalog.py | vaws lint |
| `.agents/scripts/sync_claude_skills.py` | argparse | - | 1 | mirror:2, other:1, policy:1, routing:1, skill-doc:1, test:2 | mechanics | supported | .agents/scripts/sync_claude_skills.py | vaws lint |
| `.agents/scripts/tracked_leak_scan.py` | argparse | - | 10 | docs:1, hook:1, other:1, policy:1, script:1, test:3 | mechanics | supported | .agents/scripts/tracked_leak_scan.py | vaws lint |
| `.agents/scripts/tracked_path_check.py` | argparse | - | 6 | docs:1, other:1, policy:1, test:2 | mechanics | supported | .agents/scripts/tracked_path_check.py | vaws lint |
| `.agents/scripts/vaws.py` | argparse | status, env, hook, task-server | 1 | docs:1, other:1, policy:1, routing:1, test:4 | mechanics | supported | .agents/scripts/vaws.py | vaws task |
| `.agents/scripts/vaws_client_setup.py` | argparse | - | 5 | docs:4, policy:1, script:1, skill-doc:2, test:3 | mechanics | supported | .agents/scripts/vaws_client_setup.py | vaws workspace |
| `.agents/scripts/vaws_deps.py` | argparse | status, doctor, sync | 1 | client-config:1, docs:8, other:1, policy:1, routing:1, script:4, skill-doc:4, test:7 | mechanics | supported | .agents/scripts/vaws_deps.py | vaws workspace |
| `.agents/scripts/workspace_identity.py` | argparse | summary, ensure, validate-alias, set-alias, decline-alias | 1 | skill-doc:1 | mechanics | supported | .agents/scripts/workspace_identity.py | vaws workspace |
| `.agents/scripts/workspace_profile.py` | argparse | summary, validate, ensure | 4 | routing:1, script:1, skill-doc:4 | mechanics | supported | .agents/scripts/workspace_profile.py | vaws workspace |
| `.agents/skills/ascend-memory-profiling/scripts/mem_analyze.py` | argparse | - | 1 | skill-doc:1 | mixed | supported | .agents/skills/ascend-memory-profiling/scripts/mem_analyze.py | vaws profile |
| `.agents/skills/ascend-memory-profiling/scripts/mem_collect.py` | argparse | - | 26 | script:1, skill-doc:1, test:1 | mechanics | supported | .agents/skills/ascend-memory-profiling/scripts/mem_collect.py | vaws profile |
| `.agents/skills/ascend-memory-profiling/scripts/weight_inspector.py` | argparse | - | 0 | script:1 | mechanics | payload | .agents/skills/ascend-memory-profiling/scripts/weight_inspector.py | - |
| `.agents/skills/ascend-operator-debug/scripts/operator_debug.py` | argparse | - | 3 | skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-operator-debug/scripts/operator_debug.py | guidance |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py` | argparse | - | 14 | script:2, skill-doc:7, test:1 | mechanics | payload | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/classify.py` | argparse | - | 1 | skill-doc:2 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/cross_rank.py` | argparse | - | 1 | skill-doc:3, test:1 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py` | argparse | - | 1 | script:3, skill-doc:5, test:2 | mixed | payload | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report.py` | bare-argv | - | 0 | skill-doc:1 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report_v2/__main__.py` | bare | - | 0 | - | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/normalize.py` | argparse | - | 4 | skill-doc:1 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/report.py` | argparse | - | 6 | script:3, skill-doc:3, test:2 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/segment.py` | argparse | - | 3 | script:2, skill-doc:7, test:1 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/summarize.py` | argparse | - | 8 | skill-doc:4, test:1 | mechanics | internal | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py` | argparse | - | 13 | script:2, skill-doc:4 | mechanics | payload | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/dev/golden_db_vs_csv.py` | argparse | - | 4 | skill-doc:1 | mechanics | harness | .agents/skills/ascend-profiling-analysis/scripts/dev/golden_db_vs_csv.py | - |
| `.agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py` | argparse | - | 29 | script:1, skill-doc:4, test:1 | mechanics | supported | .agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py | vaws profile |
| `.agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py` | argparse | - | 20 | script:1, skill-doc:4 | mechanics | supported | .agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py | vaws profile |
| `.agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py` | argparse | - | 33 | skill-doc:2, test:1 | mechanics | supported | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py | vaws profile |
| `.agents/skills/ascend-profiling-collection/scripts/profile_control.py` | argparse | - | 5 | script:1, skill-doc:2, test:1 | mechanics | internal | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py | - |
| `.agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py` | argparse | - | 10 | script:1, skill-doc:2 | mechanics | internal | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py | - |
| `.agents/skills/ascend-tensor-dump/assets/replay_op.py` | argparse | - | 9 | other:1, script:1, skill-doc:3, test:1 | mechanics | payload | .agents/skills/ascend-tensor-dump/assets/replay_op.py | - |
| `.agents/skills/ascend-tensor-dump/scripts/dump_compare.py` | argparse | scan, diff, tensors | 8 | script:1, skill-doc:2, test:1 | mechanics | supported | .agents/skills/ascend-tensor-dump/scripts/dump_compare.py | guidance |
| `.agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py` | argparse | - | 3 | skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py | guidance |
| `.agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py` | argparse | - | 4 | skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py | guidance |
| `.agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py` | argparse | - | 1 | test:1 | mechanics | supported | .agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py | vaws lint |
| `.agents/skills/ascend-triton-operator-development/scripts/triton_development.py` | argparse | - | 0 | skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-triton-operator-development/scripts/triton_development.py | guidance |
| `.agents/skills/ascend-triton-workflow/scripts/triton_workflow.py` | argparse | - | 2 | skill-doc:2, test:1 | mixed | supported | .agents/skills/ascend-triton-workflow/scripts/triton_workflow.py | guidance |
| `.agents/skills/modelscope/scripts/download_from_modelscope.py` | argparse | - | 12 | mirror:2, script:2, skill-doc:1, test:2 | mechanics | payload | .agents/skills/modelscope/scripts/download_from_modelscope.py | - |
| `.agents/skills/modelscope/scripts/modelscope_auto.py` | argparse | ensure, status, verify, worker | 11 | mirror:1, script:1, skill-doc:1, test:3 | mechanics | supported | .agents/skills/modelscope/scripts/modelscope_auto.py | vaws model |
| `.agents/skills/modelscope/scripts/modelscope_download_status.py` | argparse | - | 3 | mirror:1, script:1, skill-doc:1, test:2 | mechanics | internal | .agents/skills/modelscope/scripts/modelscope_auto.py | - |
| `.agents/skills/modelscope/scripts/verify_modelscope_sha256.py` | argparse | - | 8 | mirror:2, script:2, skill-doc:1, test:2 | mechanics | payload | .agents/skills/modelscope/scripts/verify_modelscope_sha256.py | - |
| `.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py` | argparse | deploy, start, status, restart, stop | 6 | docs:2, policy:1, script:1, skill-doc:1, test:3 | mechanics | supported | .agents/skills/npu-fleet-monitor/scripts/manage_monitor.py | vaws machine |
| `.agents/skills/repo-init/scripts/install_gh_user.py` | bare | - | 0 | script:1 | mechanics | supported | .agents/skills/repo-init/scripts/install_gh_user.py | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_init_probe.py` | argparse | - | 1 | policy:1, skill-doc:4 | mechanics | supported | .agents/skills/repo-init/scripts/repo_init_probe.py | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_init_profile.py` | argparse | plan, apply, apply-alias | 3 | skill-doc:4 | mechanics | supported | .agents/skills/repo-init/scripts/repo_init_profile.py | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_topology.py` | argparse | compare-main, configure, ensure-main | 8 | skill-doc:4 | mechanics | supported | .agents/skills/repo-init/scripts/repo_topology.py | vaws workspace |
| `.agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py` | argparse | - | 1 | skill-doc:4 | mechanics | supported | .agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py | vaws workspace |
| `.agents/skills/vllm-ascend-benchmark/scripts/bench_run.py` | argparse | - | 16 | skill-doc:2, test:1 | mechanics | supported | .agents/skills/vllm-ascend-benchmark/scripts/bench_run.py | vaws bench |
| `.agents/skills/vllm-ascend-change-validation/scripts/change_validation.py` | argparse | - | 8 | docs:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/vllm-ascend-change-validation/scripts/change_validation.py | guidance |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py` | argparse | prepare, normalize | 22 | script:1, skill-doc:5, test:1 | mechanics | supported | .agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py | vaws bench |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py` | argparse | - | 2 | docs:1, skill-doc:3, test:1 | mixed | supported | .agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py | vaws bench |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py` | argparse | - | 2 | script:1, skill-doc:4, test:1 | mechanics | payload | .agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py | - |
| `.agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py` | argparse | - | 3 | skill-doc:2, test:1 | mixed | supported | .agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py | guidance |
| `.agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py` | argparse | - | 4 | docs:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py | guidance |
| `.agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py` | argparse | start, status, stop, smoke | 6 | docs:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py | vaws serve |
| `.agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py` | argparse | - | 4 | docs:1, skill-doc:2, test:1 | mixed | supported | .agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py | guidance |
| `.agents/skills/vllm-ascend-serving/scripts/serving.py` | argparse | start, status, stop | 0 | docs:1, script:4, skill-doc:8, test:4 | mixed | supported | .agents/skills/vllm-ascend-serving/scripts/serving.py | - |
| `.trae/skills/modelscope/scripts/download_from_modelscope.py` | argparse | - | 12 | mirror:2, script:2, skill-doc:1, test:2 | mechanics | payload | .agents/skills/modelscope/scripts/download_from_modelscope.py | - |
| `.trae/skills/modelscope/scripts/modelscope_auto.py` | argparse | ensure, status, verify, worker | 11 | mirror:1, script:1, skill-doc:1, test:3 | mechanics | generated | .agents/skills/modelscope/scripts/modelscope_auto.py | - |
| `.trae/skills/modelscope/scripts/modelscope_download_status.py` | argparse | - | 3 | mirror:1, script:1, skill-doc:1, test:2 | mechanics | generated | .agents/skills/modelscope/scripts/modelscope_download_status.py | - |
| `.trae/skills/modelscope/scripts/verify_modelscope_sha256.py` | argparse | - | 8 | mirror:2, script:2, skill-doc:1, test:2 | mechanics | payload | .agents/skills/modelscope/scripts/verify_modelscope_sha256.py | - |
<!-- /current-cli-surface-table -->

## Historical inventory

The original #85 snapshot at b6e8559bc6e76743ffd08a383072c8b041a30e12 had
132 entries (mechanics 81, judgment 8, mixed 8, redundant 35). The old proposed
13-command dispatcher was never implemented. These rows document that snapshot
only and are not callable current interfaces.

<!-- historical-cli-surface-table -->

| Entry point | Style | Verbs | Options | Refs | Category | Target |
|---|---|---|---|---|---|---|
| `.agents/coordinator/prepare_runtime.py` | argparse | attest, publish, restore | 5 | routing:1 | mechanics | vaws sync |
| `.agents/coordinator/server.py` | argparse | - | 3 | routing:1, test:1 | mechanics | server |
| `.agents/hooks/knowledge_session_end.py` | bare | - | 0 | client-config:1, routing:1, test:2 | mechanics | hook |
| `.agents/hooks/vaws_session.py` | argparse | - | 2 | script:1, test:1 | mechanics | hook |
| `.agents/scripts/cli_surface_inventory.py` | argparse | - | 3 | test:1 | mechanics | vaws lint |
| `.agents/scripts/knowledge_capture.py` | argparse | - | 6 | routing:2, skill-doc:2, test:2 | mixed | vaws knowledge |
| `.agents/scripts/knowledge_query.py` | argparse | - | 6 | routing:2, test:2 | mechanics | vaws knowledge |
| `.agents/scripts/knowledge_validate.py` | argparse | - | 1 | other:2, routing:1, skill-doc:1, test:1 | mechanics | vaws knowledge |
| `.agents/scripts/remote_artifact_manifest.py` | delegated | - | 4 | routing:1, skill-doc:3 | redundant | .remote-dev/tools/remote_artifact_manifest.py |
| `.agents/scripts/remote_artifact_pull.py` | delegated | - | 5 | routing:1, skill-doc:4 | redundant | .remote-dev/tools/remote_artifact_pull.py |
| `.agents/scripts/remote_artifact_push.py` | delegated | - | 5 | routing:1, skill-doc:1 | redundant | .remote-dev/tools/remote_artifact_push.py |
| `.agents/scripts/remote_cleanup.py` | delegated | - | 13 | routing:1, skill-doc:3 | mechanics | vaws session |
| `.agents/scripts/remote_exec.py` | delegated | - | 8 | routing:1, skill-doc:2 | redundant | .remote-dev/tools/remote_bash.py |
| `.agents/scripts/remote_job_collect.py` | delegated | - | 5 | routing:1, skill-doc:1 | redundant | .remote-dev/tools/remote_artifact_pull.py |
| `.agents/scripts/remote_job_start.py` | delegated | - | 10 | other:1, routing:1, skill-doc:3 | redundant | .remote-dev/tools/remote_bash.py |
| `.agents/scripts/remote_job_status.py` | delegated | - | 4 | routing:1, skill-doc:2, test:1 | redundant | .remote-dev/tools/remote_job_status.py |
| `.agents/scripts/remote_job_stop.py` | delegated | - | 5 | routing:1, skill-doc:1 | redundant | .remote-dev/tools/remote_job_stop.py |
| `.agents/scripts/remote_job_tail.py` | delegated | - | 6 | routing:1, skill-doc:2 | redundant | .remote-dev/tools/remote_job_tail.py |
| `.agents/scripts/remote_probe.py` | delegated | - | 4 | routing:1, skill-doc:3, test:1 | redundant | .remote-dev/tools/remote_probe.py |
| `.agents/scripts/remote_service_logs.py` | delegated | - | 4 | routing:1, skill-doc:1 | mechanics | vaws serve |
| `.agents/scripts/remote_service_start.py` | delegated | - | 3 | routing:1, skill-doc:3 | redundant | .agents/skills/vllm-ascend-serving/scripts/serve_start.py |
| `.agents/scripts/remote_service_status.py` | delegated | - | 3 | routing:1, skill-doc:1 | redundant | .agents/skills/vllm-ascend-serving/scripts/serve_status.py |
| `.agents/scripts/remote_service_stop.py` | delegated | - | 4 | routing:1, skill-doc:1 | redundant | .agents/skills/vllm-ascend-serving/scripts/serve_stop.py |
| `.agents/scripts/remote_sync_apply.py` | delegated | - | 6 | routing:1, skill-doc:5 | redundant | .agents/skills/remote-code-parity/scripts/parity_sync.py |
| `.agents/scripts/remote_sync_plan.py` | delegated | - | 5 | routing:1, skill-doc:6 | redundant | .agents/skills/remote-code-parity/scripts/parity_sync.py |
| `.agents/scripts/remote_target_resolve.py` | delegated | - | 3 | routing:1, skill-doc:3 | redundant | .remote-dev/tools/remote_probe.py |
| `.agents/scripts/remote_toolbox_stress.py` | argparse | - | 13 | routing:1, skill-doc:1 | mechanics | harness |
| `.agents/scripts/run_manifest.py` | argparse | init, validate | 10 | other:2, routing:2 | mechanics | vaws manifest |
| `.agents/scripts/skill_catalog.py` | argparse | - | 2 | other:1, test:1 | mechanics | vaws lint |
| `.agents/scripts/vaws.py` | argparse | attach, session, run, execution, finish | 12 | other:1, routing:1, test:1 | mechanics | vaws task |
| `.agents/scripts/vaws_client_setup.py` | argparse | - | 4 | routing:1, skill-doc:2, test:1 | mechanics | vaws workspace |
| `.agents/scripts/workspace_identity.py` | argparse | summary, ensure, validate-alias, set-alias, decline-alias | 1 | routing:1, skill-doc:5 | mechanics | vaws workspace |
| `.agents/scripts/workspace_profile.py` | argparse | summary, validate, ensure | 4 | routing:2, script:1, skill-doc:8 | mechanics | vaws workspace |
| `.agents/skills/ascend-memory-profiling/scripts/mem_analyze.py` | argparse | - | 1 | routing:1, skill-doc:1 | mixed | vaws profile |
| `.agents/skills/ascend-memory-profiling/scripts/mem_collect.py` | argparse | - | 25 | routing:1, script:1, skill-doc:2, test:1 | mechanics | vaws profile |
| `.agents/skills/ascend-memory-profiling/scripts/weight_inspector.py` | argparse | - | 0 | routing:1, script:1, skill-doc:1 | mechanics | payload |
| `.agents/skills/ascend-operator-debug/scripts/operator_debug.py` | argparse | plan, record, analyze | 3 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py` | argparse | - | 14 | routing:1, script:2, skill-doc:9, test:1 | mechanics | payload |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/classify.py` | argparse | - | 1 | skill-doc:3 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/cross_rank.py` | argparse | - | 1 | skill-doc:4, test:1 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py` | argparse | - | 1 | script:3, skill-doc:6, test:2 | mixed | payload |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report.py` | bare-argv | - | 0 | skill-doc:2 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report_v2/__main__.py` | bare | - | 0 | test:1 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/normalize.py` | argparse | - | 4 | skill-doc:2 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/report.py` | argparse | - | 6 | script:3, skill-doc:4, test:2 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/segment.py` | argparse | - | 3 | script:2, skill-doc:8, test:1 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/summarize.py` | argparse | - | 8 | skill-doc:5, test:1 | redundant | .agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py |
| `.agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py` | argparse | - | 13 | routing:1, script:2, skill-doc:4 | mechanics | payload |
| `.agents/skills/ascend-profiling-analysis/scripts/dev/golden_db_vs_csv.py` | argparse | - | 4 | skill-doc:2 | mechanics | harness |
| `.agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py` | argparse | - | 25 | routing:1, script:1, skill-doc:5, test:1 | mechanics | vaws profile |
| `.agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py` | argparse | - | 16 | routing:1, script:1, skill-doc:4 | mechanics | vaws profile |
| `.agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py` | argparse | - | 33 | routing:1, skill-doc:3, test:1 | mechanics | vaws profile |
| `.agents/skills/ascend-profiling-collection/scripts/profile_control.py` | argparse | - | 4 | routing:1, script:1, skill-doc:3 | redundant | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py |
| `.agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py` | argparse | - | 7 | routing:1, script:1, skill-doc:3 | redundant | .agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py |
| `.agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py` | argparse | plan, record, analyze | 3 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py` | argparse | plan, record, analyze | 4 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py` | argparse | - | 1 | skill-doc:1, test:1 | mechanics | vaws lint |
| `.agents/skills/ascend-triton-operator-development/scripts/triton_development.py` | argparse | plan, finalize | 6 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/ascend-triton-workflow/scripts/triton_workflow.py` | argparse | plan, link, finalize | 4 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py` | argparse | list, inspect, promote, merge, reject, deprecate | 9 | other:1, routing:1, skill-doc:2, test:2 | mixed | vaws knowledge |
| `inventory.py` (deleted after original #85) | argparse | summary, get, put, upsert, remove | 15 | routing:1, script:4, skill-doc:4, test:1 | redundant | .agents/skills/machine-management/scripts/machine_add.py |
| `.agents/skills/machine-management/scripts/machine_add.py` | argparse | - | 13 | routing:1, script:3, skill-doc:4 | mechanics | vaws machine |
| `machine_remove.py` (deleted after original #85) | argparse | - | 1 | routing:1, script:1, skill-doc:4 | mechanics | vaws machine |
| `machine_repair.py` (deleted after original #85) | argparse | - | 8 | routing:1, script:1, skill-doc:4 | mechanics | vaws machine |
| `.agents/skills/machine-management/scripts/machine_verify.py` | argparse | - | 2 | mirror:1, routing:1, script:1, skill-doc:4 | mechanics | vaws machine |
| `manage_machine.py` (deleted after original #85) | argparse | probe-host, bootstrap-host-key, bootstrap-container, smoke, verify-machine, mesh-export-key, mesh-add-peer, mesh-remove-peer, clean-local-known-hosts, remove-container | 25 | routing:1, script:5, skill-doc:4 | redundant | .agents/skills/machine-management/scripts/machine_add.py |
| `.agents/skills/modelscope/scripts/download_from_modelscope.py` | argparse | - | 12 | mirror:2, routing:1, script:1, skill-doc:1 | mechanics | payload |
| `.agents/skills/modelscope/scripts/modelscope_auto.py` | argparse | ensure, status, verify, worker | 11 | mirror:1, routing:1, skill-doc:1 | mechanics | vaws model |
| `.agents/skills/modelscope/scripts/modelscope_download_status.py` | argparse | - | 3 | mirror:1, routing:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/modelscope_auto.py |
| `.agents/skills/modelscope/scripts/verify_modelscope_sha256.py` | argparse | - | 8 | mirror:2, routing:1, script:1, skill-doc:1 | mechanics | payload |
| `.agents/skills/npu-fleet-monitor/scripts/manage_monitor.py` | argparse | ensure, status, restart, stop | 2 | docs:1, routing:3, skill-doc:1, test:1 | mechanics | vaws machine |
| `.agents/skills/remote-code-parity/scripts/gc_runtime_cache.py` | argparse | - | 7 | routing:1, skill-doc:3 | mechanics | vaws sync |
| `install_consent.py` (deleted after original #85) | argparse | resolve, set, batch-set, resolve-sync-mode, set-sync-mode | 7 | routing:1, script:1, skill-doc:4 | mechanics | vaws sync |
| `.agents/skills/remote-code-parity/scripts/parity_sync.py` | argparse | - | 17 | mirror:1, routing:2, script:3, skill-doc:8, test:2 | mechanics | vaws sync |
| `.agents/skills/remote-code-parity/scripts/parity_watch.py` | argparse | - | 2 | routing:1, skill-doc:3, test:1 | mechanics | vaws sync |
| `.agents/skills/remote-code-parity/scripts/remote_code_parity.py` | argparse | plan, sync | 9 | routing:1, script:3, skill-doc:3, test:2 | mechanics | payload |
| `.agents/skills/remote-code-parity/scripts/transport_benchmark.py` | argparse | - | 4 | skill-doc:2 | mechanics | harness |
| `.agents/skills/repo-init/scripts/install_gh_user.py` | bare | - | 0 | script:1 | mechanics | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_init_probe.py` | argparse | - | 1 | mirror:1, routing:1, skill-doc:4 | mechanics | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_init_profile.py` | argparse | plan, apply, apply-alias | 3 | routing:1, skill-doc:4 | mechanics | vaws workspace |
| `.agents/skills/repo-init/scripts/repo_topology.py` | argparse | compare-main, configure, ensure-main | 8 | routing:1, skill-doc:4 | mechanics | vaws workspace |
| `.agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py` | argparse | - | 1 | skill-doc:4 | mechanics | vaws workspace |
| `npu_coordination.py` (deleted after original #85) | argparse | submit, acquire, preflight, activate, heartbeat, release, cancel, status, gc, hold-add, hold-remove | 37 | mcp:1, routing:3, script:1, skill-doc:4 | mechanics | vaws session |
| `session_create.py` (deleted after original #85) | argparse | - | 20 | routing:2, script:2, skill-doc:12, test:3 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_diff.py` | argparse | - | 3 | script:1, skill-doc:4 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_gc.py` | argparse | - | 3 | routing:1, skill-doc:5, test:1 | mechanics | vaws session |
| `.agents/skills/session-management/scripts/session_group.py` | argparse | create, status, list, teardown | 7 | routing:1, skill-doc:3, test:1 | mechanics | vaws session |
| `session_list.py` (deleted after original #85) | argparse | - | 1 | routing:1, skill-doc:2 | mechanics | vaws session |
| `session_remove.py` (deleted after original #85) | argparse | - | 6 | routing:1, script:2, skill-doc:5, test:1 | mechanics | vaws session |
| `session_status.py` (deleted after original #85) | argparse | - | 2 | other:1, routing:1, skill-doc:1 | mechanics | vaws session |
| `.agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py` | argparse | - | 28 | skill-doc:3 | mixed | vaws bench |
| `.agents/skills/vllm-ascend-benchmark/scripts/bench_run.py` | argparse | - | 16 | routing:1, skill-doc:6 | mechanics | vaws bench |
| `.agents/skills/vllm-ascend-change-validation/scripts/change_validation.py` | argparse | plan, link, finalize | 11 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py` | argparse | prepare, normalize | 19 | skill-doc:2, test:1 | mechanics | vaws bench |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py` | argparse | init, compare | 13 | routing:1, skill-doc:2, test:1 | mixed | vaws bench |
| `.agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py` | argparse | - | 2 | skill-doc:2, test:1 | mechanics | payload |
| `.agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py` | argparse | init, ingest, analyze | 3 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py` | argparse | init, record, compare, finalize | 25 | routing:1, skill-doc:3, test:1 | mixed | guidance |
| `.agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py` | argparse | plan, start, status, smoke, stop | 4 | routing:1, skill-doc:2, test:1 | mixed | vaws serve |
| `.agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py` | argparse | plan, record, normalize, analyze | 9 | routing:1, skill-doc:2, test:1 | judgment | guidance |
| `.agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py` | argparse | - | 3 | mirror:1, routing:2, skill-doc:6 | redundant | .agents/skills/machine-management/scripts/machine_verify.py |
| `.agents/skills/vllm-ascend-serving/scripts/serve_start.py` | argparse | - | 16 | mirror:1, routing:1, script:4, skill-doc:12, test:1 | mechanics | vaws serve |
| `.agents/skills/vllm-ascend-serving/scripts/serve_status.py` | argparse | - | 2 | mirror:1, routing:1, script:1, skill-doc:4 | mechanics | vaws serve |
| `.agents/skills/vllm-ascend-serving/scripts/serve_stop.py` | argparse | - | 3 | mirror:1, routing:1, script:4, skill-doc:13, test:1 | mechanics | vaws serve |
| `.remote-dev/core/managed_jobs.py` | bare-argv | - | 0 | mcp:1, test:1 | mechanics | payload |
| `.remote-dev/hooks/claude_remote_guard.py` | bare | - | 0 | client-config:1, test:1 | mechanics | hook |
| `.remote-dev/hooks/codex_remote_guard.py` | bare | - | 0 | client-config:1, test:1 | mechanics | hook |
| `.remote-dev/mcp/server.py` | bare | - | 0 | client-config:2, other:3, routing:1, script:1, test:1 | mechanics | server |
| `.remote-dev/tools/remote_apply_patch.py` | delegated | - | 19 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_artifact_manifest.py` | delegated | - | 17 | routing:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_artifact_pull.py` | delegated | - | 18 | routing:1, skill-doc:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_artifact_push.py` | delegated | - | 18 | routing:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_bash.py` | delegated | - | 20 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_context_snapshot.py` | delegated | - | 17 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_edit.py` | delegated | - | 20 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_glob.py` | delegated | - | 20 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_grep.py` | delegated | - | 23 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_job_status.py` | delegated | - | 17 | routing:1, test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_job_stop.py` | delegated | - | 18 | routing:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_job_tail.py` | delegated | - | 19 | routing:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_ls.py` | delegated | - | 19 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_monitor.py` | delegated | - | 20 | test:1 | redundant | .remote-dev/tools/remote_bash.py |
| `.remote-dev/tools/remote_multi_edit.py` | delegated | - | 18 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_probe.py` | delegated | - | 16 | routing:1, test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_read.py` | delegated | - | 20 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/remote_write.py` | delegated | - | 21 | test:1 | mechanics | vaws remote |
| `.remote-dev/tools/sync_claude_skills.py` | argparse | - | 1 | other:2, routing:1, script:1, test:1 | mechanics | vaws lint |
| `.remote-dev/tools/validate_remote_dev_scaffold.py` | argparse | - | 14 | other:2, routing:1 | mechanics | vaws lint |
| `.trae/skills/modelscope/scripts/download_from_modelscope.py` | argparse | - | 12 | mirror:2, routing:1, script:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/download_from_modelscope.py |
| `.trae/skills/modelscope/scripts/modelscope_auto.py` | argparse | ensure, status, verify, worker | 11 | mirror:1, routing:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/modelscope_auto.py |
| `.trae/skills/modelscope/scripts/modelscope_download_status.py` | argparse | - | 3 | mirror:1, routing:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/modelscope_auto.py |
| `.trae/skills/modelscope/scripts/verify_modelscope_sha256.py` | argparse | - | 8 | mirror:2, routing:1, script:1, skill-doc:1 | redundant | .agents/skills/modelscope/scripts/verify_modelscope_sha256.py |

<!-- /historical-cli-surface-table -->
