#!/usr/bin/env python3
"""Enumerate the scaffold's CLI entry points and emit the inventory as data.

The scaffold accumulated one argparse program per skill script. This tool
measures that surface so the consolidation described in
``docs/cli-surface.md`` is trackable rather than asserted.

Definition of an *entry point* (deliberately mechanical, so the number is
reproducible):

- a tracked ``*.py`` file outside the ``vllm/`` and ``vllm-ascend/``
  submodules and outside ``.git/`` and untracked local state;
- not a test (``tests/`` directory, ``test_*.py``, ``selftest_*.py``,
  ``conftest.py``) and not a package ``__init__.py``;
- that either has an ``if __name__ == "__main__":`` guard or is a
  ``__main__.py`` module.

For every entry point the tool records, via ``ast`` (no imports of the
inspected code are performed):

- the parser style: ``argparse`` inline, ``delegated`` to a library function
  (the 18 ``remote_*.py`` toolbox wrappers and the 18 ``.remote-dev/tools``
  fallbacks), or ``bare`` (``sys.argv`` handling);
- verbs (``add_parser(...)`` names and ``choices`` of a positional action
  argument);
- option strings from ``add_argument`` calls, including helper functions such
  as ``add_target_args(parser)`` resolved inside the same module;
- who references the script (skill docs, routing documents, other scripts,
  hooks, MCP/coordinator code, generated mirrors, tests);
- the curated classification overlay from ``CLASSIFICATION`` and the
  consolidated command it collapses into.

Progress is bounded on ``stderr``; one JSON payload is printed on ``stdout``.
The exit code is ``0`` when every entry point is classified and ``1`` when
the overlay has drifted (new or removed scripts), so the test suite can hold
the number to the documented figure.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable

SUBMODULES = ("vllm", "vllm-ascend")
IGNORED_PREFIXES = (".git/", ".vaws-local/", ".remote-dev/state/")
TEST_FILE_RE = re.compile(r"^(test_.*|.*_test|selftest_.*|conftest)\.py$")
REFERENCE_SUFFIXES = (".md", ".py", ".sh", ".json", ".yaml", ".yml", ".toml", ".mdc")
ACTION_DESTS = {"action", "command", "operation", "mode", "subcommand", "verb", "op"}
MAX_PROGRESS_LINES = 12

CATEGORIES = ("mechanics", "judgment", "mixed", "redundant")

# Targets that are not `vaws <group>` agent commands:
#   guidance  judgment that stops being a command and becomes SKILL.md text
#   payload   executable spawned by a command or run on the container; kept,
#             but removed from agent routing
#   hook      client lifecycle hook fed by stdin JSON
#   server    long-running server started by a client, not by the agent
#   harness   maturation/test tooling, never named in routing
NON_COMMAND_TARGETS = frozenset({"guidance", "payload", "hook", "server", "harness"})

# ---------------------------------------------------------------------------
# Curated classification overlay.
#
# Keys are repository-relative paths. ``category`` is one of ``CATEGORIES``.
# ``target`` is the consolidated command a mechanics/mixed entry point folds
# into, ``guidance`` for judgment that stops being a command, or the surviving
# entry point for a redundant one. ``note`` is the one-line evidence summary;
# the long-form reasoning lives in ``docs/cli-surface.md``.
# ---------------------------------------------------------------------------
CLASSIFICATION: dict[str, dict[str, str]] = {
    # ---- .agents/scripts: shared helpers -----------------------------------
    ".agents/scripts/vaws.py": {
        "category": "mechanics",
        "target": "vaws task",
        "note": "existing facade for the pool task tools (attach/session/run/execution/finish); also exposed as MCP vaws_* tools",
    },
    ".agents/scripts/vaws_client_setup.py": {
        "category": "mechanics",
        "target": "vaws workspace",
        "note": "writes client MCP/hook config files; pure local file mechanics",
    },
    ".agents/scripts/workspace_profile.py": {
        "category": "mechanics",
        "target": "vaws workspace",
        "note": "local machine-profile state (summary/validate/ensure)",
    },
    ".agents/scripts/workspace_identity.py": {
        "category": "mechanics",
        "target": "vaws workspace",
        "note": "local UUID/alias state; same .vaws-local state family as workspace_profile",
    },
    ".agents/scripts/run_manifest.py": {
        "category": "mechanics",
        "target": "vaws manifest",
        "note": "schema-driven init/validate of Run Manifest v1",
    },
    ".agents/scripts/skill_catalog.py": {
        "category": "mechanics",
        "target": "vaws lint",
        "note": "repo self-check; deterministic",
    },
    ".agents/scripts/cli_surface_inventory.py": {
        "category": "mechanics",
        "target": "vaws lint",
        "note": "this tool; repo self-check",
    },
    ".agents/scripts/knowledge_validate.py": {
        "category": "mechanics",
        "target": "vaws knowledge",
        "note": "schema validation of knowledge documents",
    },
    ".agents/scripts/knowledge_query.py": {
        "category": "mechanics",
        "target": "vaws knowledge",
        "note": "deterministic fingerprint match against formal entries; named in AGENTS.md routing",
    },
    ".agents/scripts/knowledge_capture.py": {
        "category": "mixed",
        "target": "vaws knowledge",
        "note": "storage/redaction is mechanics; whether a fix is 'verified' is judgment the agent supplies through flags; named in AGENTS.md routing",
    },
    ".agents/scripts/remote_toolbox_stress.py": {
        "category": "mechanics",
        "target": "harness",
        "note": "maturation harness for the toolbox (concurrent exec, jobs, artifacts, cleanup); test tooling, not agent routing",
    },
    # ---- .agents/scripts: remote_* toolbox wrappers ------------------------
    # Each is a 15-line shim around vaws_remote_toolbox.cli_*; the toolbox
    # keeps its own job store (.vaws-local/remote-toolbox/jobs) and result
    # envelope, distinct from .remote-dev (<root>/.remote-dev/jobs,
    # remote-dev.result.v1).
    ".agents/scripts/remote_target_resolve.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_probe.py",
        "note": "core.endpoint.resolve_endpoint already resolves machine/session/host targets for every remote-dev call",
    },
    ".agents/scripts/remote_probe.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_probe.py",
        "note": "second probe with a different envelope; same verb exists as MCP remote_probe",
    },
    ".agents/scripts/remote_exec.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_bash.py",
        "note": "bounded remote shell; remote.bash owns this mechanic",
    },
    ".agents/scripts/remote_job_start.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_bash.py",
        "note": "remote.bash --run-in-background starts a tracked job",
    },
    ".agents/scripts/remote_job_status.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_job_status.py",
        "note": "same verb in remote-dev; incompatible job store",
    },
    ".agents/scripts/remote_job_tail.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_job_tail.py",
        "note": "same verb in remote-dev; incompatible job store",
    },
    ".agents/scripts/remote_job_stop.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_job_stop.py",
        "note": "same verb in remote-dev; incompatible job store",
    },
    ".agents/scripts/remote_job_collect.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_artifact_pull.py",
        "note": "collecting a job directory is an artifact pull",
    },
    ".agents/scripts/remote_sync_plan.py": {
        "category": "redundant",
        "target": ".agents/skills/remote-code-parity/scripts/parity_sync.py",
        "note": "toolbox sync_plan shells out to parity_sync.py --print-derived-args and remote_code_parity.py plan",
    },
    ".agents/scripts/remote_sync_apply.py": {
        "category": "redundant",
        "target": ".agents/skills/remote-code-parity/scripts/parity_sync.py",
        "note": "toolbox sync_apply shells out to parity_sync.py; three CLI layers for one mechanic",
    },
    ".agents/scripts/remote_service_start.py": {
        "category": "redundant",
        "target": ".agents/skills/vllm-ascend-serving/scripts/serve_start.py",
        "note": "toolbox service adapter spawns serve_start.py and passes unknown flags through",
    },
    ".agents/scripts/remote_service_status.py": {
        "category": "redundant",
        "target": ".agents/skills/vllm-ascend-serving/scripts/serve_status.py",
        "note": "toolbox service adapter spawns serve_status.py",
    },
    ".agents/scripts/remote_service_logs.py": {
        "category": "mechanics",
        "target": "vaws serve",
        "note": "only implementation of service log tailing; becomes `vaws serve logs`",
    },
    ".agents/scripts/remote_service_stop.py": {
        "category": "redundant",
        "target": ".agents/skills/vllm-ascend-serving/scripts/serve_stop.py",
        "note": "toolbox service adapter spawns serve_stop.py",
    },
    ".agents/scripts/remote_artifact_manifest.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_artifact_manifest.py",
        "note": "identical verb in remote-dev and MCP",
    },
    ".agents/scripts/remote_artifact_pull.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_artifact_pull.py",
        "note": "identical verb in remote-dev and MCP",
    },
    ".agents/scripts/remote_artifact_push.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_artifact_push.py",
        "note": "identical verb in remote-dev and MCP",
    },
    ".agents/scripts/remote_cleanup.py": {
        "category": "mechanics",
        "target": "vaws session",
        "note": "dry-run-capable cleanup of jobs/services/leases/known-hosts/temp; merges with session_gc into `vaws session gc`",
    },
    # ---- .remote-dev/tools: substrate CLI fallbacks ------------------------
    # 18 two-line shims around _cli.main(tool); argument lists in _cli.py are a
    # hand-maintained mirror of .remote-dev/mcp/schemas.py.
    ".remote-dev/tools/remote_bash.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote shell; MCP remote_bash"},
    ".remote-dev/tools/remote_monitor.py": {
        "category": "redundant",
        "target": ".remote-dev/tools/remote_bash.py",
        "note": "_cli.run_tool dispatches monitor to remote_bash(run_in_background=True)",
    },
    ".remote-dev/tools/remote_read.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote read; MCP remote_read"},
    ".remote-dev/tools/remote_write.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote write; MCP remote_write"},
    ".remote-dev/tools/remote_edit.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote edit; MCP remote_edit"},
    ".remote-dev/tools/remote_multi_edit.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote multi-edit; MCP remote_multi_edit"},
    ".remote-dev/tools/remote_glob.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote glob; MCP remote_glob"},
    ".remote-dev/tools/remote_grep.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote grep; MCP remote_grep"},
    ".remote-dev/tools/remote_ls.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote ls; MCP remote_ls"},
    ".remote-dev/tools/remote_apply_patch.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical remote patch; MCP remote_apply_patch"},
    ".remote-dev/tools/remote_job_status.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical job status; MCP remote_job_status"},
    ".remote-dev/tools/remote_job_tail.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical job tail; MCP remote_job_tail"},
    ".remote-dev/tools/remote_job_stop.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical job stop; MCP remote_job_stop"},
    ".remote-dev/tools/remote_artifact_manifest.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical artifact manifest; MCP remote_artifact_manifest"},
    ".remote-dev/tools/remote_artifact_pull.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical artifact pull; MCP remote_artifact_pull"},
    ".remote-dev/tools/remote_artifact_push.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical artifact push; MCP remote_artifact_push"},
    ".remote-dev/tools/remote_context_snapshot.py": {"category": "mechanics", "target": "vaws remote", "note": "endpoint context snapshot; MCP remote_context_snapshot"},
    ".remote-dev/tools/remote_probe.py": {"category": "mechanics", "target": "vaws remote", "note": "canonical endpoint probe; MCP remote_probe"},
    ".remote-dev/tools/sync_claude_skills.py": {"category": "mechanics", "target": "vaws lint", "note": "regenerates .claude shims; repo self-check with --check"},
    ".remote-dev/tools/validate_remote_dev_scaffold.py": {"category": "mechanics", "target": "vaws lint", "note": "scaffold validation; --local-only half is lint, the endpoint half is the maturation harness"},
    # ---- servers, supervisors, hooks (not agent-invoked CLIs) ----------------
    ".remote-dev/mcp/server.py": {"category": "mechanics", "target": "server", "note": "stdio MCP server; launched by the client, not by the agent"},
    ".agents/coordinator/server.py": {"category": "mechanics", "target": "server", "note": "optional pool coordinator HTTP server"},
    ".remote-dev/core/managed_jobs.py": {"category": "mechanics", "target": "payload", "note": "detached supervisor process spawned by remote.bash; never agent-invoked"},
    ".remote-dev/hooks/claude_remote_guard.py": {"category": "mechanics", "target": "hook", "note": "client hook; stdin JSON, not a CLI"},
    ".remote-dev/hooks/codex_remote_guard.py": {"category": "mechanics", "target": "hook", "note": "client hook; stdin JSON, not a CLI"},
    ".agents/hooks/vaws_session.py": {"category": "mechanics", "target": "hook", "note": "native-session lifecycle hook (--client/--project)"},
    ".agents/hooks/knowledge_session_end.py": {"category": "mechanics", "target": "hook", "note": "session-end flush hook"},
    ".agents/coordinator/prepare_runtime.py": {"category": "mechanics", "target": "vaws sync", "note": "attest/publish/restore of a built runtime cache; imports remote_code_parity and belongs with sync"},
    # ---- repo-init -----------------------------------------------------------
    ".agents/skills/repo-init/scripts/repo_init_probe.py": {"category": "mechanics", "target": "vaws workspace", "note": "read-only local facts (gh, auth, submodules, remotes)"},
    ".agents/skills/repo-init/scripts/repo_init_profile.py": {"category": "mechanics", "target": "vaws workspace", "note": "plan/apply of workspace_profile ensure with a narrowed username choice; the choice itself stays a user gate"},
    ".agents/skills/repo-init/scripts/repo_topology.py": {"category": "mechanics", "target": "vaws workspace", "note": "fork/remote topology via gh+git (compare-main/configure/ensure-main)"},
    ".agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py": {"category": "mechanics", "target": "vaws workspace", "note": "parses the vllm-ascend CI pin; deterministic"},
    ".agents/skills/repo-init/scripts/install_gh_user.py": {"category": "mechanics", "target": "vaws workspace", "note": "user-local gh install; bare sys.argv, referenced only by a sibling script"},
    # ---- machine-management ---------------------------------------------------
    ".agents/skills/machine-management/scripts/machine_add.py": {"category": "mechanics", "target": "vaws machine", "note": "SSH bootstrap + container + inventory upsert; image track stays an explicit user gate"},
    ".agents/skills/machine-management/scripts/machine_verify.py": {"category": "mechanics", "target": "vaws machine", "note": "read-only host/container/NPU facts"},
    ".agents/skills/machine-management/scripts/machine_repair.py": {"category": "mechanics", "target": "vaws machine", "note": "idempotent re-run of bootstrap steps"},
    ".agents/skills/machine-management/scripts/machine_remove.py": {"category": "mechanics", "target": "vaws machine", "note": "inventory removal + optional container teardown"},
    ".agents/skills/machine-management/scripts/inventory.py": {"category": "redundant", "target": ".agents/skills/machine-management/scripts/machine_add.py", "note": "imported as a library by _workflow_common; its own CLI (summary/get/put/upsert/remove) is documented as low-level only"},
    ".agents/skills/machine-management/scripts/manage_machine.py": {"category": "redundant", "target": ".agents/skills/machine-management/scripts/machine_add.py", "note": "3.5k-line library imported by _workflow_common; its CLI is documented as low-level only"},
    # ---- npu-fleet-monitor ------------------------------------------------------
    ".agents/skills/npu-fleet-monitor/scripts/manage_monitor.py": {"category": "mechanics", "target": "vaws machine", "note": "ensure/status/restart/stop of the loopback dashboard; a service bound to the fleet inventory"},
    # ---- session-management ---------------------------------------------------
    ".agents/skills/session-management/scripts/session_create.py": {"category": "mechanics", "target": "vaws session", "note": "worktree + container + lease creation; named in AGENTS.md routing"},
    ".agents/skills/session-management/scripts/session_list.py": {"category": "mechanics", "target": "vaws session", "note": "read local session/lease state"},
    ".agents/skills/session-management/scripts/session_status.py": {"category": "mechanics", "target": "vaws session", "note": "one session + live probe"},
    ".agents/skills/session-management/scripts/session_remove.py": {"category": "mechanics", "target": "vaws session", "note": "teardown service/container/worktree/leases"},
    ".agents/skills/session-management/scripts/session_group.py": {"category": "mechanics", "target": "vaws session", "note": "create/status/list/teardown of session groups"},
    ".agents/skills/session-management/scripts/session_gc.py": {"category": "mechanics", "target": "vaws session", "note": "stale metadata GC with --reap-dead; overlaps remote_cleanup"},
    ".agents/skills/session-management/scripts/session_diff.py": {"category": "mechanics", "target": "vaws session", "note": "git diff of worktree + submodules"},
    ".agents/skills/session-management/scripts/npu_coordination.py": {"category": "mechanics", "target": "vaws session", "note": "optional host-shared SQLite NPU queue (11 verbs); lease mechanics; named in AGENTS.md routing"},
    # ---- remote-code-parity ----------------------------------------------------
    ".agents/skills/remote-code-parity/scripts/parity_sync.py": {"category": "mechanics", "target": "vaws sync", "note": "canonical local->container snapshot sync; spawned by serve_start, toolbox, mem_collect, profile_analyze"},
    ".agents/skills/remote-code-parity/scripts/remote_code_parity.py": {"category": "mechanics", "target": "payload", "note": "2.3k-line implementation; its plan/sync CLI is spawned by parity_sync, the toolbox and vaws_task_client, so it is a payload, not an agent surface"},
    ".agents/skills/remote-code-parity/scripts/install_consent.py": {"category": "mechanics", "target": "vaws sync", "note": "records install consent and sync-mode overrides; becomes `vaws sync consent`"},
    ".agents/skills/remote-code-parity/scripts/gc_runtime_cache.py": {"category": "mechanics", "target": "vaws sync", "note": "prunes the container cache root; becomes `vaws sync gc`"},
    ".agents/skills/remote-code-parity/scripts/parity_watch.py": {"category": "mechanics", "target": "vaws sync", "note": "watcher that re-publishes snapshots; becomes `vaws sync --watch`"},
    ".agents/skills/remote-code-parity/scripts/transport_benchmark.py": {"category": "mechanics", "target": "harness", "note": "bundle vs receive-pack transport micro-benchmark; maturation tooling"},
    # ---- modelscope --------------------------------------------------------------
    ".agents/skills/modelscope/scripts/modelscope_auto.py": {"category": "mechanics", "target": "vaws model", "note": "ensure/status/verify facade; spawns the download and verify scripts"},
    ".agents/skills/modelscope/scripts/download_from_modelscope.py": {"category": "mechanics", "target": "payload", "note": "spawned by modelscope_auto as a subprocess; not an agent surface"},
    ".agents/skills/modelscope/scripts/verify_modelscope_sha256.py": {"category": "mechanics", "target": "payload", "note": "spawned by modelscope_auto as a subprocess; not an agent surface"},
    ".agents/skills/modelscope/scripts/modelscope_download_status.py": {"category": "redundant", "target": ".agents/skills/modelscope/scripts/modelscope_auto.py", "note": "modelscope_auto status reimplements the size comparison"},
    ".trae/skills/modelscope/scripts/modelscope_auto.py": {"category": "redundant", "target": ".agents/skills/modelscope/scripts/modelscope_auto.py", "note": "byte-identical tracked copy of the .agents script"},
    ".trae/skills/modelscope/scripts/download_from_modelscope.py": {"category": "redundant", "target": ".agents/skills/modelscope/scripts/download_from_modelscope.py", "note": "byte-identical tracked copy of the .agents script"},
    ".trae/skills/modelscope/scripts/modelscope_download_status.py": {"category": "redundant", "target": ".agents/skills/modelscope/scripts/modelscope_auto.py", "note": "byte-identical tracked copy of the .agents script"},
    ".trae/skills/modelscope/scripts/verify_modelscope_sha256.py": {"category": "redundant", "target": ".agents/skills/modelscope/scripts/verify_modelscope_sha256.py", "note": "byte-identical tracked copy of the .agents script"},
    # ---- vllm-ascend-serving ----------------------------------------------------
    ".agents/skills/vllm-ascend-serving/scripts/serve_start.py": {"category": "mechanics", "target": "vaws serve", "note": "canonical vllm serve start (1.4k lines, 16 options + passthrough); spawns parity_sync"},
    ".agents/skills/vllm-ascend-serving/scripts/serve_status.py": {"category": "mechanics", "target": "vaws serve", "note": "health probe of the tracked service"},
    ".agents/skills/vllm-ascend-serving/scripts/serve_stop.py": {"category": "mechanics", "target": "vaws serve", "note": "stop the tracked service (--force)"},
    ".agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py": {"category": "redundant", "target": ".agents/skills/machine-management/scripts/machine_verify.py", "note": "host-side npu-smi probe; npu-smi parsing exists in 7 files, machine verify owns device facts"},
    # ---- vllm-ascend-benchmark --------------------------------------------------
    ".agents/skills/vllm-ascend-benchmark/scripts/bench_run.py": {"category": "mechanics", "target": "vaws bench", "note": "vllm bench serve with warmup/multi-run; writes results"},
    ".agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py": {"category": "mixed", "target": "vaws bench", "note": "multi-state checkout+serve+bench loop is mechanics (`bench run --state a --state b`); the delta table is evidence, the verdict is the agent's"},
    # ---- ascend-memory-profiling -------------------------------------------------
    ".agents/skills/ascend-memory-profiling/scripts/mem_collect.py": {"category": "mechanics", "target": "vaws profile", "note": "npu-smi baseline + msprof-wrapped serve + workload + pull; 25 options"},
    ".agents/skills/ascend-memory-profiling/scripts/mem_analyze.py": {"category": "mixed", "target": "vaws profile", "note": "parsing dumps into a breakdown table is mechanics; the cross-validation narrative and attribution are judgment"},
    ".agents/skills/ascend-memory-profiling/scripts/weight_inspector.py": {"category": "mechanics", "target": "payload", "note": "safetensors header reader designed to run on the remote; spawned by mem_collect"},
    # ---- ascend-profiling-collection ---------------------------------------------
    ".agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py": {"category": "mechanics", "target": "vaws profile", "note": "single agent-facing collection orchestrator (33 options)"},
    ".agents/skills/ascend-profiling-collection/scripts/profile_control.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py", "note": "imported as a library by collect_torch_profile_case (post_remote_action); its CLI duplicates one step of the orchestrator"},
    ".agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py", "note": "imported as a library by collect_torch_profile_case (analyse_profile_root); its CLI duplicates one step"},
    # ---- ascend-profiling-analysis ------------------------------------------------
    ".agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py": {"category": "mechanics", "target": "vaws profile", "note": "local driver: resolve session, tar-sync ascend_profile/, run `python3 -m ascend_profile.analyze` remotely, pull outputs"},
    ".agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py": {"category": "mechanics", "target": "vaws profile", "note": "same driver over many roots"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py": {"category": "mechanics", "target": "payload", "note": "remote-side pipeline entry executed on the container by profile_analyze"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py": {"category": "mechanics", "target": "payload", "note": "remote-side sweep entry executed on the container by profile_sweep"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/normalize.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py", "note": "pipeline stage imported by analyze; stage CLI is debug-only"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/segment.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py", "note": "pipeline stage imported by analyze; stage CLI is debug-only"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/classify.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py", "note": "pipeline stage imported by analyze; stage CLI is debug-only"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/summarize.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py", "note": "pipeline stage imported by analyze; stage CLI is debug-only"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/report.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py", "note": "pipeline stage imported by analyze; stage CLI is debug-only"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/cross_rank.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py", "note": "pipeline stage imported by analyze; stage CLI is debug-only"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py": {"category": "mixed", "target": "payload", "note": "evidence tables are mechanics; the claim generation encodes 'what is wrong' thresholds (diagnosis_rules.yaml) that the agent should weigh, not inherit"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py", "note": "v1 renderer (3.2k lines) reachable through report.py next to v2; standalone sys.argv entry is debug-only"},
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report_v2/__main__.py": {"category": "redundant", "target": ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py", "note": "renderer reachable through report.py; standalone entry is debug-only"},
    ".agents/skills/ascend-profiling-analysis/scripts/dev/golden_db_vs_csv.py": {"category": "mechanics", "target": "harness", "note": "golden equivalence check run on the container; test tooling"},
    # ---- knowledge curation ---------------------------------------------------------
    ".agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py": {"category": "mixed", "target": "vaws knowledge", "note": "list/inspect/promote/merge/reject/deprecate file moves are mechanics; which verb applies is the review judgment"},
    # ---- ledger-style debugging / validation skills ------------------------------------
    # Nine scripts share one shape: plan writes config+manifest, record appends
    # an agent-authored result JSON, analyze tallies statuses into
    # passed/failed/inconclusive. None of them runs anything on hardware (no
    # SSH; change_validation only shells out to local git diff).
    ".agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py": {"category": "mixed", "target": "guidance", "note": "init/record/finalize is a hypothesis ledger (judgment); compare is a real JSONL numeric diff with atol/rtol that belongs to `vaws bench compare`"},
    ".agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py": {"category": "mixed", "target": "vaws bench", "note": "init renders the two commands and compare classifies outputs by tolerance (mechanics); whether a class is acceptable is judgment; zero subprocess calls"},
    ".agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py": {"category": "mechanics", "target": "payload", "note": "remote-side offline/online case runner executed on the container"},
    ".agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py": {"category": "mechanics", "target": "vaws bench", "note": "prepare/normalize of AISBench CSV; deterministic adapter"},
    ".agents/skills/vllm-ascend-change-validation/scripts/change_validation.py": {"category": "judgment", "target": "guidance", "note": "plan maps a diff to required evidence by regex path patterns such as (?i)(custom_op|ops/|kernel); SKILL.md step 3 tells the agent to correct the mapping; only local git diff is executed"},
    ".agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py": {"category": "judgment", "target": "guidance", "note": "plan/record/normalize/analyze over agent-supplied results; bench_compare already runs the alternating A/B loop this skill only tallies"},
    ".agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py": {"category": "judgment", "target": "guidance", "note": "init/ingest/analyze over agent-normalized events; the topology facts come from remote.bash, the finding rules are fixed heuristics"},
    ".agents/skills/ascend-operator-debug/scripts/operator_debug.py": {"category": "judgment", "target": "guidance", "note": "plan/record/analyze case-matrix bookkeeping; record takes a --result JSON the agent wrote"},
    ".agents/skills/ascend-triton-operator-development/scripts/triton_development.py": {"category": "judgment", "target": "guidance", "note": "plan/finalize ledger; no mechanic of its own"},
    ".agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py": {"category": "judgment", "target": "guidance", "note": "plan/record/analyze ledger around agent-run cases; the static gate lives in validate_triton_impl"},
    ".agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py": {"category": "mechanics", "target": "vaws lint", "note": "static AST check that a Triton file launches a kernel and does not fall back to torch; deterministic"},
    ".agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py": {"category": "judgment", "target": "guidance", "note": "plan/record/analyze with weighted-improvement thresholds over agent-supplied measurements"},
    ".agents/skills/ascend-triton-workflow/scripts/triton_workflow.py": {"category": "judgment", "target": "guidance", "note": "plan/link/finalize that only links the other three ledgers; a workflow engine"},
    ".agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py": {"category": "mixed", "target": "vaws serve", "note": "start/status/smoke/stop spawn serve_start.py per group member (mechanics, `serve --group`); plan's 130-line connector validation encodes deployment judgment"},
}


@dataclass
class Reference:
    path: str
    kind: str
    lines: list[int] = field(default_factory=list)


@dataclass
class EntryPoint:
    path: str
    area: str
    skill: str | None
    parser_style: str
    delegate: str | None
    verbs: list[str]
    options: list[str]
    positionals: list[str]
    docstring: str
    references: list[Reference]
    reference_kinds: dict[str, int]
    basename_collision: bool
    classification: dict[str, str] | None


@dataclass
class Finding:
    code: str
    path: str
    message: str


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def progress(message: str, *, counter: list[int]) -> None:
    if counter[0] < MAX_PROGRESS_LINES:
        print(f"[cli-surface] {message}", file=sys.stderr)
    counter[0] += 1


def tracked_files(repo_root: Path) -> list[str]:
    """Tracked plus untracked-but-not-ignored files, so a new script is
    counted before it is committed. Falls back to a filesystem walk when the
    directory is not a Git checkout (used by the unit tests)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=True,
            capture_output=True,
        ).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
    except (subprocess.CalledProcessError, FileNotFoundError):
        names = [
            p.relative_to(repo_root).as_posix()
            for p in repo_root.rglob("*")
            if p.is_file()
        ]
    kept: set[str] = set()
    for name in names:
        top = name.split("/", 1)[0]
        if top in SUBMODULES or name.startswith(IGNORED_PREFIXES):
            continue
        if not (repo_root / name).is_file():
            continue
        kept.add(name)
    return sorted(kept)


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    return "tests" in parts[:-1] or bool(TEST_FILE_RE.match(parts[-1]))


def has_main_guard(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.Compare) and len(test.comparators) == 1:
            left, right = test.left, test.comparators[0]
            names = {
                getattr(left, "id", None),
                getattr(right, "value", None),
            }
            if "__name__" in names and "__main__" in names:
                return True
    return False


def area_of(rel: str) -> str:
    if rel.startswith(".agents/skills/"):
        return ".agents/skills"
    if rel.startswith(".agents/scripts/"):
        return ".agents/scripts"
    if rel.startswith(".agents/hooks/"):
        return ".agents/hooks"
    if rel.startswith(".agents/coordinator/"):
        return ".agents/coordinator"
    if rel.startswith(".agents/"):
        return ".agents"
    if rel.startswith(".remote-dev/"):
        return ".remote-dev"
    if rel.startswith(".trae/"):
        return ".trae"
    if rel.startswith(".claude/"):
        return ".claude"
    return "other"


def skill_of(rel: str) -> str | None:
    parts = rel.split("/")
    if len(parts) > 3 and parts[0] in {".agents", ".trae", ".claude"} and parts[1] == "skills":
        return parts[2]
    return None


# ---------------------------------------------------------------------------
# argparse extraction
# ---------------------------------------------------------------------------


def _str_constants(node: ast.AST) -> list[str]:
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def _call_attr(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    if isinstance(node.func, ast.Name):
        return node.func.id
    return None


def _keyword(node: ast.Call, name: str) -> ast.AST | None:
    for kw in node.keywords:
        if kw.arg == name:
            return kw.value
    return None


class ParserCollector:
    """Collect argparse verbs and option strings from a body of statements."""

    def __init__(self, module: ast.Module, *, tool_literal: str | None = None) -> None:
        self.module = module
        self.tool_literal = tool_literal
        self.functions = {
            n.name: n for n in module.body if isinstance(n, ast.FunctionDef)
        }
        self.verbs: list[str] = []
        self.options: list[str] = []
        self.positionals: list[str] = []
        self.parser_calls = 0
        self._seen: set[str] = set()
        # ``for name in ("start", "stop"): sub.add_parser(name)`` is common;
        # remember constant loop iterables so the verb names resolve.
        self._loop_vars: dict[str, list[str]] = {}

    def collect_function(self, name: str, depth: int = 0) -> None:
        if name in self._seen or depth > 3:
            return
        fn = self.functions.get(name)
        if fn is None:
            return
        self._seen.add(name)
        self._walk(fn.body, depth)

    def collect_module(self) -> None:
        self._walk(self.module.body, 0)
        for fn in self.functions.values():
            self.collect_function(fn.name, 1)

    def _branch_applies(self, test: ast.AST) -> bool:
        if self.tool_literal is None:
            return True
        literals = _str_constants(test)
        if not literals:
            return True
        return self.tool_literal in literals

    def _walk(self, body: Iterable[ast.stmt], depth: int) -> None:
        for stmt in body:
            if isinstance(stmt, ast.If):
                if self._branch_applies(stmt.test):
                    self._walk(stmt.body, depth)
                # ``else`` branches apply when no literal matched or when the
                # branch is unconditional; both are conservative supersets.
                if not self._branch_applies(stmt.test) or not _str_constants(stmt.test):
                    self._walk(stmt.orelse, depth)
                continue
            if isinstance(stmt, (ast.For, ast.While, ast.With, ast.Try)):
                if isinstance(stmt, ast.For) and isinstance(stmt.target, ast.Name):
                    if isinstance(stmt.iter, (ast.Tuple, ast.List)):
                        self._loop_vars[stmt.target.id] = _str_constants(stmt.iter)
                inner = list(getattr(stmt, "body", []))
                for attr in ("orelse", "finalbody"):
                    inner.extend(getattr(stmt, attr, []) or [])
                for handler in getattr(stmt, "handlers", []) or []:
                    inner.extend(handler.body)
                self._walk(inner, depth)
                continue
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            for node in ast.walk(stmt):
                if not isinstance(node, ast.Call):
                    continue
                attr = _call_attr(node)
                if attr == "ArgumentParser":
                    self.parser_calls += 1
                elif attr == "add_parser" and node.args:
                    first = node.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        self.verbs.append(first.value)
                    elif isinstance(first, ast.Name) and first.id in self._loop_vars:
                        self.verbs.extend(self._loop_vars[first.id])
                elif attr == "add_argument":
                    self._add_argument(node)
                elif isinstance(node.func, ast.Name) and node.func.id in self.functions:
                    self.collect_function(node.func.id, depth + 1)

    def _add_argument(self, node: ast.Call) -> None:
        names = [
            a.value
            for a in node.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
        if not names:
            return
        flags = [n for n in names if n.startswith("-")]
        if flags:
            self.options.append(flags[0])
            return
        positional = names[0]
        self.positionals.append(positional)
        dest_node = _keyword(node, "dest")
        dest = dest_node.value if isinstance(dest_node, ast.Constant) else positional
        choices = _keyword(node, "choices")
        if choices is not None and str(dest) in ACTION_DESTS:
            for value in _str_constants(choices):
                self.verbs.append(value)


def _dedupe(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _import_map(tree: ast.Module) -> dict[str, tuple[str, str]]:
    """Map local names to (module, attribute) for ``from X import Y`` forms."""
    mapping: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                mapping[alias.asname or alias.name] = (node.module, alias.name)
    return mapping


def _main_guard_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in tree.body:
        if isinstance(node, ast.If) and has_main_guard(ast.Module(body=[node], type_ignores=[])):
            for inner in ast.walk(node):
                if isinstance(inner, ast.Call):
                    calls.append(inner)
    return calls


def _candidate_module_paths(repo_root: Path, entry: Path, module: str) -> list[Path]:
    name = module.split(".")[-1] + ".py"
    return [
        entry.parent / name,
        repo_root / ".agents" / "lib" / name,
        repo_root / ".remote-dev" / "tools" / name,
        repo_root / ".remote-dev" / module.replace(".", "/") / "__init__.py",
        repo_root / ".remote-dev" / (module.replace(".", "/") + ".py"),
        entry.parent / module.replace(".", "/") / "__init__.py",
    ]


def analyse_entry(repo_root: Path, rel: str) -> tuple[str, str | None, list[str], list[str], list[str], str]:
    path = repo_root / rel
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    doc = (ast.get_docstring(tree) or "").strip().splitlines()
    summary = doc[0] if doc else ""

    collector = ParserCollector(tree)
    collector.collect_module()
    if collector.parser_calls:
        return (
            "argparse",
            None,
            _dedupe(collector.verbs),
            _dedupe(collector.options),
            _dedupe(collector.positionals),
            summary,
        )

    imports = _import_map(tree)
    for call in _main_guard_calls(tree):
        target = call.func
        name = target.id if isinstance(target, ast.Name) else None
        if name is None or name not in imports:
            continue
        module, attr = imports[name]
        tool_literal = None
        if call.args and isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
            tool_literal = call.args[0].value
        for candidate in _candidate_module_paths(repo_root, path, module):
            if not candidate.is_file():
                continue
            lib_tree = ast.parse(candidate.read_text(encoding="utf-8"), filename=str(candidate))
            lib = ParserCollector(lib_tree, tool_literal=tool_literal)
            lib.collect_function(attr)
            # ``main(tool)`` style dispatchers build the parser elsewhere.
            for fn_name in ("build_parser", "_build_parser", "make_parser", "add_target_args", "add_endpoint_args"):
                if fn_name in lib.functions:
                    lib.collect_function(fn_name)
            if lib.parser_calls or lib.options or lib.verbs:
                label = f"{module}.{attr}" + (f"({tool_literal!r})" if tool_literal else "")
                lib_doc = (ast.get_docstring(lib_tree) or "").strip().splitlines()
                return (
                    "delegated",
                    label,
                    _dedupe(lib.verbs),
                    _dedupe(lib.options),
                    _dedupe(lib.positionals),
                    summary or (lib_doc[0] if lib_doc else ""),
                )
    uses_argv = any(
        isinstance(n, ast.Attribute) and n.attr == "argv" for n in ast.walk(tree)
    )
    return ("bare-argv" if uses_argv else "bare", None, [], [], [], summary)


# ---------------------------------------------------------------------------
# References
# ---------------------------------------------------------------------------

# Files that mention every entry point by construction; they are not callers.
INVENTORY_ARTIFACTS = frozenset({".agents/scripts/cli_surface_inventory.py", "docs/cli-surface.md"})

ROUTING_DOCS = {
    "AGENTS.md",
    "CLAUDE.md",
    "README.md",
    "README.en.md",
    ".agents/README.md",
    ".remote-dev/README.md",
    ".agents/coordinator/README.md",
}


def reference_kind(rel: str) -> str:
    if rel in ROUTING_DOCS or rel.startswith((".cursor/rules/", ".trae/rules/")):
        return "routing"
    if rel.startswith((".claude/skills/", ".trae/skills/")):
        return "mirror"
    if rel.startswith((".claude/", ".codex/", ".cursor/")):
        return "client-config"
    if is_test_path(rel):
        return "test"
    if "/hooks/" in rel:
        return "hook"
    if rel.startswith((".remote-dev/mcp/", ".agents/coordinator/")):
        return "mcp"
    if rel.startswith("docs/"):
        return "docs"
    if rel.startswith(".agents/skills/") and rel.endswith(".md"):
        return "skill-doc"
    if rel.startswith(".agents/skills/") and rel.endswith((".yaml", ".yml", ".json")):
        return "skill-doc"
    if rel.endswith((".py", ".sh")):
        return "script"
    return "other"


def _collision_owner(line: str, rel: str, siblings: list[str]) -> bool:
    """Decide whether a line mentioning a colliding basename points at ``rel``.

    Only the path token directly attached to the basename is consulted, so
    ``.agents/scripts/x.py`` and ``.remote-dev/tools/x.py`` on one line are
    each attributed once. A bare basename with no directory is ambiguous and
    is attributed to every sibling.
    """
    base = rel.rsplit("/", 1)[-1]
    mine = rel.rsplit("/", 1)[0]
    others = [sib.rsplit("/", 1)[0] for sib in siblings if sib != rel]
    decided = False
    for match in re.finditer(r"([\w./-]*)" + re.escape(base), line):
        prefix = match.group(1)
        if prefix.rstrip("/").endswith(mine) or mine in prefix:
            return True
        if any(other in prefix for other in others):
            decided = True
            continue
        return True  # no directory hint: ambiguous, attribute to all
    return not decided


def collect_references(
    repo_root: Path, files: list[str], entries: list[str]
) -> dict[str, list[Reference]]:
    by_base: dict[str, list[str]] = {}
    for rel in entries:
        by_base.setdefault(rel.rsplit("/", 1)[-1], []).append(rel)
    refs: dict[str, dict[str, Reference]] = {rel: {} for rel in entries}
    for rel in files:
        if not rel.endswith(REFERENCE_SUFFIXES) or rel in INVENTORY_ARTIFACTS:
            continue
        try:
            text = (repo_root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for base, owners in by_base.items():
                if base not in line:
                    continue
                for owner in owners:
                    if owner == rel:
                        continue
                    if len(owners) > 1 and not _collision_owner(line, owner, owners):
                        continue
                    ref = refs[owner].setdefault(rel, Reference(rel, reference_kind(rel)))
                    ref.lines.append(lineno)
    return {rel: sorted(items.values(), key=lambda r: r.path) for rel, items in refs.items()}


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------


def build_inventory(repo_root: Path, *, quiet: bool = False) -> dict:
    counter = [0]
    files = tracked_files(repo_root)
    if not quiet:
        progress(f"scanning {len(files)} tracked files under {repo_root.name}", counter=counter)

    entries: list[str] = []
    argparse_importers = 0
    for rel in files:
        if not rel.endswith(".py") or is_test_path(rel) or rel.endswith("__init__.py"):
            continue
        if rel.endswith("__main__.py"):
            entries.append(rel)
            continue
        try:
            tree = ast.parse((repo_root / rel).read_text(encoding="utf-8"), filename=rel)
        except (SyntaxError, UnicodeDecodeError):
            continue
        if any(
            (isinstance(n, ast.Import) and any(a.name == "argparse" for a in n.names))
            or (isinstance(n, ast.ImportFrom) and n.module == "argparse")
            for n in ast.walk(tree)
        ):
            argparse_importers += 1
        if has_main_guard(tree):
            entries.append(rel)
    if not quiet:
        progress(f"found {len(entries)} entry points", counter=counter)

    references = collect_references(repo_root, files, entries)
    if not quiet:
        progress("resolved cross-references", counter=counter)

    basenames: dict[str, int] = {}
    for rel in entries:
        base = rel.rsplit("/", 1)[-1]
        basenames[base] = basenames.get(base, 0) + 1

    records: list[EntryPoint] = []
    findings: list[Finding] = []
    for rel in entries:
        style, delegate, verbs, options, positionals, summary = analyse_entry(repo_root, rel)
        refs = references.get(rel, [])
        kinds: dict[str, int] = {}
        for ref in refs:
            kinds[ref.kind] = kinds.get(ref.kind, 0) + 1
        classification = CLASSIFICATION.get(rel)
        if classification is None:
            findings.append(Finding("unclassified", rel, "entry point has no classification overlay"))
        elif classification["category"] not in CATEGORIES:
            findings.append(Finding("bad-category", rel, f"unknown category {classification['category']!r}"))
        records.append(
            EntryPoint(
                path=rel,
                area=area_of(rel),
                skill=skill_of(rel),
                parser_style=style,
                delegate=delegate,
                verbs=verbs,
                options=options,
                positionals=positionals,
                docstring=summary,
                references=refs,
                reference_kinds=dict(sorted(kinds.items())),
                basename_collision=basenames[rel.rsplit("/", 1)[-1]] > 1,
                classification=classification,
            )
        )
    for rel in sorted(CLASSIFICATION):
        if rel not in entries:
            findings.append(Finding("stale-classification", rel, "classified path is no longer an entry point"))
    entry_set = set(entries)
    for rel, meta in CLASSIFICATION.items():
        if meta["category"] == "redundant" and meta["target"] not in entry_set:
            findings.append(Finding("redundant-target-missing", rel, f"target {meta['target']!r} is not an entry point"))
        if meta["category"] in {"mechanics", "mixed"} and not (
            meta["target"].startswith("vaws ") or meta["target"] in NON_COMMAND_TARGETS
        ):
            findings.append(Finding("bad-target", rel, f"target {meta['target']!r} is neither a `vaws` command nor a known non-command kind"))

    by_area: dict[str, int] = {}
    by_category: dict[str, int] = {c: 0 for c in CATEGORIES}
    by_style: dict[str, int] = {}
    by_skill: dict[str, int] = {}
    by_target_kind: dict[str, int] = {}
    targets: dict[str, list[str]] = {}
    for record in records:
        by_area[record.area] = by_area.get(record.area, 0) + 1
        by_style[record.parser_style] = by_style.get(record.parser_style, 0) + 1
        if record.skill:
            by_skill[record.skill] = by_skill.get(record.skill, 0) + 1
        if record.classification:
            category = record.classification["category"]
            target = record.classification["target"]
            by_category[category] += 1
            kind = (
                "agent-command" if target.startswith("vaws ")
                else "surviving-entry-point" if category == "redundant"
                else target
            )
            by_target_kind[kind] = by_target_kind.get(kind, 0) + 1
            if category in {"mechanics", "mixed"}:
                targets.setdefault(target, []).append(record.path)
    agent_commands = sorted(t for t in targets if t.startswith("vaws "))
    # Entry points an agent can be routed to today: everything except hooks,
    # servers, remote payloads, and maturation harnesses.
    agent_facing_today = [
        r.path
        for r in records
        if r.classification and r.classification["target"] not in {"hook", "server", "payload", "harness"}
    ]
    unreferenced = [
        r.path
        for r in records
        if not any(ref.kind not in {"test", "mirror"} for ref in r.references)
    ]
    if not quiet:
        progress(f"classified {len(records) - len([f for f in findings if f.code == 'unclassified'])}/{len(records)}", counter=counter)

    return {
        "status": "passed" if not findings else "failed",
        "repo_root_name": repo_root.name,
        "definition": "tracked or untracked-unignored *.py outside submodules/tests with a __main__ guard or __main__.py",
        "entry_point_count": len(records),
        "counts": {
            "argparse_importing_files": argparse_importers,
            "by_area": dict(sorted(by_area.items())),
            "by_parser_style": dict(sorted(by_style.items())),
            "by_category": by_category,
            "by_target_kind": dict(sorted(by_target_kind.items())),
            "by_skill": dict(sorted(by_skill.items())),
            "skills_with_entry_points": len(by_skill),
            "agent_facing_today": len(agent_facing_today),
        },
        "target_surface": {
            "agent_command_count": len(agent_commands),
            "agent_commands": agent_commands,
            "collapse": {k: sorted(v) for k, v in sorted(targets.items())},
        },
        "unreferenced_outside_tests": unreferenced,
        "entry_points": [asdict(r) for r in records],
        "finding_count": len(findings),
        "findings": [asdict(f) for f in findings],
    }


def render_markdown(payload: dict) -> str:
    lines = [
        "| Entry point | Style | Verbs | Options | Refs | Category | Target |",
        "|---|---|---|---|---|---|---|",
    ]
    for record in payload["entry_points"]:
        cls = record["classification"] or {}
        verbs = ", ".join(record["verbs"]) or "-"
        refs = ", ".join(f"{k}:{v}" for k, v in record["reference_kinds"].items()) or "-"
        lines.append(
            "| `{path}` | {style} | {verbs} | {n_opts} | {refs} | {cat} | {target} |".format(
                path=record["path"],
                style=record["parser_style"],
                verbs=verbs,
                n_opts=len(record["options"]),
                refs=refs,
                cat=cls.get("category", "unclassified"),
                target=cls.get("target", "-"),
            )
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="workspace root; defaults to the repository containing this script",
    )
    parser.add_argument(
        "--format",
        choices=("json", "markdown", "summary"),
        default="json",
        help="json is the machine-readable payload; markdown renders the inventory table; summary prints counts only",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress stderr progress")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    payload = build_inventory(args.repo_root.resolve(), quiet=args.quiet)
    if args.format == "markdown":
        sys.stdout.write(render_markdown(payload))
    elif args.format == "summary":
        summary = {k: payload[k] for k in ("status", "entry_point_count", "counts", "target_surface", "finding_count", "findings")}
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
