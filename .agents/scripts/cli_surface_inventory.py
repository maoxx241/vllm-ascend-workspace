#!/usr/bin/env python3
"""Enumerate the scaffold's CLI entry points and emit the inventory as data.

The scaffold ships many Python programs with a ``__main__`` guard. This tool
measures that surface from AST only so the current census, support roles and
external-owner boundaries can be checked as data. It does not implement the
historical thirteen-command dispatcher, import inspected scripts, fetch
providers, access NPU hosts or ask every command for ``--help``.

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

- the parser style: ``argparse`` inline, ``delegated`` to a library function,
  or ``bare`` / ``bare-argv``;
- verbs (``add_parser(...)`` names and ``choices`` of a positional action
  argument);
- option strings from ``add_argument`` calls, including helper functions such
  as ``add_target_args(parser)`` resolved inside the same module;
- who references the script (skill docs, routing documents, other scripts,
  hooks, MCP/coordinator code, generated mirrors, policy, source-map pins,
  tests);
- the curated overlay: responsibility (mechanics / judgment / mixed),
  current support role, current target, optional historical proposed target,
  and optional committed external owner.

Progress is bounded on ``stderr``; one JSON payload is printed on ``stdout``.
The exit code is ``0`` when every discovered entry is classified coherently
and ``1`` when the overlay has drifted.
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

RESPONSIBILITIES = ("mechanics", "judgment", "mixed")
SUPPORT_ROLES = (
    "supported",
    "compatibility",
    "internal",
    "generated",
    "hook",
    "payload",
    "harness",
)
TARGET_KINDS = ("self", "local-entry", "external", "non-command")
NON_COMMAND_TARGETS = frozenset({"guidance", "payload", "hook", "server", "harness"})
DEP_DECLARATIONS = (
    ("remote-dev", "remote-dev.json"),
    ("vaws-coordinator", "coordinator.json"),
    ("vaws-top", "vaws-top.json"),
    ("vaws-knowledge", "vaws-knowledge.json"),
)

CURRENT_TABLE_BEGIN = "<!-- current-cli-surface-table -->"
CURRENT_TABLE_END = "<!-- /current-cli-surface-table -->"
HISTORICAL_TABLE_BEGIN = "<!-- historical-cli-surface-table -->"
HISTORICAL_TABLE_END = "<!-- /historical-cli-surface-table -->"

# Dated original #85 snapshot. Not a current total, CI invariant, or target.
HISTORICAL_SNAPSHOT = {
    "label": "original #85 b6e8559bc6e76743ffd08a383072c8b041a30e12",
    "status": "historical",
    "entry_point_count": 132,
    "by_category": {"mechanics": 81, "judgment": 8, "mixed": 8, "redundant": 35},
    "agent_facing_then": 114,
    "proposed_agent_command_count": 13,
    "proposed_verb_count": 75,
}
HISTORICAL_PROPOSED_COMMANDS = (
    "vaws remote",
    "vaws machine",
    "vaws session",
    "vaws sync",
    "vaws serve",
    "vaws bench",
    "vaws profile",
    "vaws model",
    "vaws workspace",
    "vaws manifest",
    "vaws knowledge",
    "vaws lint",
    "vaws task",
)

# Root-supplied integration facts for this census. The commit that lands the
# three owned files is an output of the work, not an input to this table.
ACCEPTED_PUBLIC_MAIN = "84f7e865a4e698d244c1cbe6cab6a2c5cca21067"
ACCEPTED_PUBLIC_TREE = "d8d5b42bfb07a97895b46281a664c7bec57e746d"
ORIGINAL_PR85 = "b6e8559bc6e76743ffd08a383072c8b041a30e12"
MERGE_PREVIEW_TREE = "626e553438e5b24451c8735682b0c0ce6e76d89c"
SCAFFOLD_SOURCE = "maoxx241/vllm-ascend-workspace"


def _cls(
    responsibility: str,
    support_role: str,
    note: str,
    *,
    target: str | None = None,
    target_kind: str = "self",
    proposed_target: str = "",
    external_owner: str = "",
) -> dict[str, str]:
    rec = {
        "responsibility": responsibility,
        "support_role": support_role,
        "target_kind": target_kind,
        "note": note,
    }
    if target:
        rec["target"] = target
    if proposed_target:
        rec["proposed_target"] = proposed_target
    if external_owner:
        rec["external_owner"] = external_owner
    return rec


# ---------------------------------------------------------------------------
# Curated classification overlay.
#
# Keys are repository-relative paths. ``responsibility`` is mechanics /
# judgment / mixed and is independent of current support. ``support_role`` is
# the current non-overlapping role. ``target`` is the current owner, never a
# future ``vaws <noun>`` command. ``proposed_target`` is the unimplemented
# original #85 option when one was recorded. ``external_owner`` names a
# committed pin in ``.agents/deps/``; provider source is not fetched.
# ---------------------------------------------------------------------------
CLASSIFICATION: dict[str, dict[str, str]] = {
    ".agents/hooks/knowledge_session_end.py": _cls(
        "mechanics", "hook",
        "session-end flush of deferred knowledge candidates; stdin/client hook, not an agent CLI",
    ),
    ".agents/hooks/tracked_leak_precommit.py": _cls(
        "mechanics", "hook",
        "git pre-commit wrapper around tracked_leak_scan; local identifier guard",
    ),
    ".agents/hooks/vaws_session.py": _cls(
        "mechanics", "hook",
        "compatibility adapter that execs the coordinator native-session hook when the checkout is present",
        external_owner="vaws-coordinator",
    ),
    ".agents/maturation/run.py": _cls(
        "mechanics", "harness",
        "deterministic-core maturation harness; --list/--report stay offline; live runs need a separate hardware contract",
    ),
    ".agents/scripts/cli_surface_inventory.py": _cls(
        "mechanics", "supported",
        "this tool; AST inventory and overlay coherence check",
        proposed_target="vaws lint",
    ),
    ".agents/scripts/envelope_lint.py": _cls(
        "mechanics", "supported",
        "Result Envelope v1 scan/check/run; schema owner remains vaws_result_envelope.py",
        proposed_target="vaws lint",
    ),
    ".agents/scripts/knowledge_capture.py": _cls(
        "mixed", "supported",
        "redaction, coordinate capture and atomic write are mechanics; verified/unverified is supplied by the caller",
        proposed_target="vaws knowledge",
    ),
    ".agents/scripts/knowledge_export.py": _cls(
        "mechanics", "supported",
        "source-side export gate: unresolved coordinates, redaction, provenance and content_hash; writes a local proposal bundle",
        proposed_target="vaws knowledge",
    ),
    ".agents/scripts/knowledge_migrate_v2.py": _cls(
        "mechanics", "supported",
        "v1 to v2 migration that writes explicit unresolved coordinates and reports what remains unknown",
        proposed_target="vaws knowledge",
    ),
    ".agents/scripts/knowledge_query.py": _cls(
        "mechanics", "supported",
        "shared/project/candidate query surface with --capabilities and layer coverage; missing shared cache degrades visibly",
        proposed_target="vaws knowledge",
    ),
    ".agents/scripts/knowledge_shared_cache.py": _cls(
        "mechanics", "supported",
        "status/import/clear of a local shared cache; import is a local directory, not a network fetch",
        proposed_target="vaws knowledge",
    ),
    ".agents/scripts/knowledge_validate.py": _cls(
        "mechanics", "supported",
        "schema validation of knowledge documents",
        proposed_target="vaws knowledge",
    ),
    ".agents/scripts/remote_artifact_manifest.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox artifact manifest; distinct store from extracted remote-dev, left for C3",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_artifact_pull.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox artifact pull; distinct store from extracted remote-dev, left for C3",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_artifact_push.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox artifact push; distinct store from extracted remote-dev, left for C3",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_cleanup.py": _cls(
        "mechanics", "supported",
        "dry-run-capable cleanup of jobs/services/leases/known-hosts/temp",
        proposed_target="vaws session",
    ),
    ".agents/scripts/remote_dev.py": _cls(
        "mechanics", "supported",
        "scaffold launcher for extracted remote-dev: status/bootstrap/server/hook/tool/env; tool NAME is not expanded into provider verbs",
        proposed_target="vaws remote",
        external_owner="remote-dev",
    ),
    ".agents/scripts/remote_exec.py": _cls(
        "mechanics", "compatibility",
        "managed-session SSH via vaws_remote_toolbox; not the C1 direct remote.bash path; transports are not unified",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_job_collect.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox job-directory collect; incompatible with remote-dev job ids (C3)",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_job_start.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox job start; incompatible in-flight ids versus extracted remote-dev (C3)",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_job_status.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox job status against .vaws-local/remote-toolbox/jobs",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_job_stop.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox job stop; store remains distinct from extracted remote-dev",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_job_tail.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox job tail; store remains distinct from extracted remote-dev",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_probe.py": _cls(
        "mechanics", "compatibility",
        "managed toolbox probe; C1 direct probe is remote_dev.py tool remote_probe",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_service_logs.py": _cls(
        "mechanics", "supported",
        "current service log tailer used with serve_start/status/stop",
        proposed_target="vaws serve",
    ),
    ".agents/scripts/remote_service_start.py": _cls(
        "mechanics", "compatibility",
        "toolbox adapter that spawns serve_start.py and re-wraps JSON",
        target=".agents/skills/vllm-ascend-serving/scripts/serve_start.py",
        target_kind="local-entry",
        proposed_target="vaws serve",
    ),
    ".agents/scripts/remote_service_status.py": _cls(
        "mechanics", "compatibility",
        "toolbox adapter that spawns serve_status.py",
        target=".agents/skills/vllm-ascend-serving/scripts/serve_status.py",
        target_kind="local-entry",
        proposed_target="vaws serve",
    ),
    ".agents/scripts/remote_service_stop.py": _cls(
        "mechanics", "compatibility",
        "toolbox adapter that spawns serve_stop.py",
        target=".agents/skills/vllm-ascend-serving/scripts/serve_stop.py",
        target_kind="local-entry",
        proposed_target="vaws serve",
    ),
    ".agents/scripts/remote_sync_apply.py": _cls(
        "mechanics", "compatibility",
        "toolbox sync_apply shells out to parity_sync.py; C3 still owns the subprocess chain",
        target=".agents/skills/remote-code-parity/scripts/parity_sync.py",
        target_kind="local-entry",
        proposed_target="vaws sync",
    ),
    ".agents/scripts/remote_sync_plan.py": _cls(
        "mechanics", "compatibility",
        "toolbox sync_plan shells out to parity_sync.py / remote_code_parity.py plan",
        target=".agents/skills/remote-code-parity/scripts/parity_sync.py",
        target_kind="local-entry",
        proposed_target="vaws sync",
    ),
    ".agents/scripts/remote_target_resolve.py": _cls(
        "mechanics", "compatibility",
        "managed selector resolve; C1 host+port and machine/session resolution live in remote-dev plus vaws_remote_dev_plugin",
        proposed_target="vaws remote",
    ),
    ".agents/scripts/remote_toolbox_stress.py": _cls(
        "mechanics", "harness",
        "maturation stress for the managed toolbox; not agent routing",
    ),
    ".agents/scripts/repo_boundary_check.py": _cls(
        "mechanics", "supported",
        "AST boundary scan against .agents/policy/repo-boundaries.json and its dated baseline",
        proposed_target="vaws lint",
    ),
    ".agents/scripts/run_manifest.py": _cls(
        "mechanics", "supported",
        "Run Manifest v1 init/validate; shared owner with vaws_run_manifest.py lifecycle APIs",
        proposed_target="vaws manifest",
    ),
    ".agents/scripts/skill_catalog.py": _cls(
        "mechanics", "supported",
        "repo skill-catalog self-check",
        proposed_target="vaws lint",
    ),
    ".agents/scripts/split_reconcile.py": _cls(
        "mechanics", "supported",
        "compares split-ledger declarations to local destination checkouts; missing checkouts are unverified, never fetched",
        proposed_target="vaws lint",
    ),
    ".agents/scripts/sync_claude_skills.py": _cls(
        "mechanics", "supported",
        "generates Claude SKILL.md shims and the six-file ModelScope Trae projection; --check detects drift",
        proposed_target="vaws lint",
    ),
    ".agents/scripts/tracked_leak_scan.py": _cls(
        "mechanics", "supported",
        "tracked-tree identifier scan with optional --strict-allowlist",
        proposed_target="vaws lint",
    ),
    ".agents/scripts/vaws.py": _cls(
        "mechanics", "supported",
        "coordinator launcher (status/bootstrap/env/hook/task-server) plus attach/session/run/execution/finish delegated outside argparse subparsers",
        proposed_target="vaws task",
        external_owner="vaws-coordinator",
    ),
    ".agents/scripts/vaws_client_setup.py": _cls(
        "mechanics", "supported",
        "writes client MCP/hook config files; does not apply the coordinator JSON helper as a preserving migration",
        proposed_target="vaws workspace",
    ),
    ".agents/scripts/workspace_identity.py": _cls(
        "mechanics", "supported",
        "local UUID/alias state",
        proposed_target="vaws workspace",
    ),
    ".agents/scripts/workspace_profile.py": _cls(
        "mechanics", "supported",
        "local machine-profile state (summary/validate/ensure)",
        proposed_target="vaws workspace",
    ),
    ".agents/skills/ascend-memory-profiling/scripts/mem_analyze.py": _cls(
        "mixed", "supported",
        "dump-to-breakdown table is mechanics; attribution narrative remains judgment",
        proposed_target="vaws profile",
    ),
    ".agents/skills/ascend-memory-profiling/scripts/mem_collect.py": _cls(
        "mechanics", "supported",
        "npu-smi baseline + wrapped serve + workload + pull",
        proposed_target="vaws profile",
    ),
    ".agents/skills/ascend-memory-profiling/scripts/weight_inspector.py": _cls(
        "mechanics", "payload",
        "safetensors header reader spawned on the remote by mem_collect",
    ),
    ".agents/skills/ascend-operator-debug/scripts/operator_debug.py": _cls(
        "mixed", "supported",
        "plan selects the case matrix (judgment); record/analyze keep schema and status-count mechanics; keep rejection tests",
        proposed_target="guidance",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py": _cls(
        "mechanics", "payload",
        "remote-side pipeline entry executed by profile_analyze",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/classify.py": _cls(
        "mechanics", "internal",
        "pipeline stage imported by analyze; stage CLI is diagnostic",
        target=".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/cross_rank.py": _cls(
        "mechanics", "internal",
        "pipeline stage imported by analyze; stage CLI is diagnostic",
        target=".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/diagnostics.py": _cls(
        "mixed", "payload",
        "evidence tables are mechanics; diagnosis_rules.yaml claims are thresholds the agent must weigh",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report.py": _cls(
        "mechanics", "internal",
        "v1 renderer reachable through report.py; standalone argv entry is diagnostic",
        target=".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/html_report_v2/__main__.py": _cls(
        "mechanics", "internal",
        "v2 renderer reachable through report.py; standalone entry is diagnostic",
        target=".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/normalize.py": _cls(
        "mechanics", "internal",
        "pipeline stage imported by analyze; stage CLI is diagnostic",
        target=".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/report.py": _cls(
        "mechanics", "internal",
        "pipeline stage imported by analyze; stage CLI is diagnostic",
        target=".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/segment.py": _cls(
        "mechanics", "internal",
        "pipeline stage imported by analyze; stage CLI is diagnostic",
        target=".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/summarize.py": _cls(
        "mechanics", "internal",
        "pipeline stage imported by analyze; stage CLI is diagnostic",
        target=".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/analyze.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/ascend_profile/sweep.py": _cls(
        "mechanics", "payload",
        "remote-side sweep entry executed by profile_sweep",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/dev/golden_db_vs_csv.py": _cls(
        "mechanics", "harness",
        "golden db-vs-csv equivalence check; test tooling",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/profile_analyze.py": _cls(
        "mechanics", "supported",
        "local driver: session resolve, sync, remote analyze, pull outputs",
        proposed_target="vaws profile",
    ),
    ".agents/skills/ascend-profiling-analysis/scripts/profile_sweep.py": _cls(
        "mechanics", "supported",
        "local driver over many profile roots",
        proposed_target="vaws profile",
    ),
    ".agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py": _cls(
        "mechanics", "supported",
        "single agent-facing collection orchestrator",
        proposed_target="vaws profile",
    ),
    ".agents/skills/ascend-profiling-collection/scripts/profile_control.py": _cls(
        "mechanics", "internal",
        "imported by collect_torch_profile_case; CLI duplicates one orchestrator step",
        target=".agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-profiling-collection/scripts/run_remote_analyse.py": _cls(
        "mechanics", "internal",
        "imported by collect_torch_profile_case; CLI duplicates one orchestrator step",
        target=".agents/skills/ascend-profiling-collection/scripts/collect_torch_profile_case.py",
        target_kind="local-entry",
    ),
    ".agents/skills/ascend-triton-kernel-optimization/scripts/triton_optimization.py": _cls(
        "mixed", "supported",
        "plan/thresholds are judgment; record/analyze consume measurements, artifacts and Run Manifests; keep rejection tests",
        proposed_target="guidance",
    ),
    ".agents/skills/ascend-triton-kernel-validation/scripts/triton_validation.py": _cls(
        "mixed", "supported",
        "plan selects cases; record/analyze keep schema/status mechanics around validate_triton_impl",
        proposed_target="guidance",
    ),
    ".agents/skills/ascend-triton-kernel-validation/scripts/validate_triton_impl.py": _cls(
        "mechanics", "supported",
        "static AST check that a Triton file launches a kernel and does not fall back to torch",
        proposed_target="vaws lint",
    ),
    ".agents/skills/ascend-triton-operator-development/scripts/triton_development.py": _cls(
        "mixed", "supported",
        "plan is judgment; finalize checks candidate hash/parent/case coverage on shared manifests",
        proposed_target="guidance",
    ),
    ".agents/skills/ascend-triton-workflow/scripts/triton_workflow.py": _cls(
        "mixed", "supported",
        "plan is judgment; link/finalize reject unplanned stages, wrong parent/run type and nonterminal children",
        proposed_target="guidance",
    ),
    ".agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py": _cls(
        "mixed", "supported",
        "list/inspect/promote/merge/reject/deprecate/resolve/verify file moves are mechanics; which verb applies is the review",
        proposed_target="vaws knowledge",
    ),
    ".agents/skills/machine-management/scripts/inventory.py": _cls(
        "mechanics", "internal",
        "library imported by _workflow_common; CLI (summary/get/put/upsert/remove) is low-level diagnostic",
        target=".agents/skills/machine-management/scripts/machine_add.py",
        target_kind="local-entry",
        proposed_target="vaws machine",
    ),
    ".agents/skills/machine-management/scripts/machine_add.py": _cls(
        "mechanics", "supported",
        "SSH bootstrap + container + inventory upsert; image track stays an explicit user gate",
        proposed_target="vaws machine",
    ),
    ".agents/skills/machine-management/scripts/machine_remove.py": _cls(
        "mechanics", "supported",
        "inventory removal + optional container teardown",
        proposed_target="vaws machine",
    ),
    ".agents/skills/machine-management/scripts/machine_repair.py": _cls(
        "mechanics", "supported",
        "idempotent re-run of bootstrap steps",
        proposed_target="vaws machine",
    ),
    ".agents/skills/machine-management/scripts/machine_verify.py": _cls(
        "mechanics", "supported",
        "read-only host/container/NPU facts",
        proposed_target="vaws machine",
    ),
    ".agents/skills/machine-management/scripts/manage_machine.py": _cls(
        "mechanics", "internal",
        "host/container library imported by _workflow_common; CLI is low-level diagnostic",
        target=".agents/skills/machine-management/scripts/machine_add.py",
        target_kind="local-entry",
        proposed_target="vaws machine",
    ),
    ".agents/skills/modelscope/scripts/download_from_modelscope.py": _cls(
        "mechanics", "payload",
        "spawned by modelscope_auto; not the agent-facing facade",
    ),
    ".agents/skills/modelscope/scripts/modelscope_auto.py": _cls(
        "mechanics", "supported",
        "ensure/status/verify/worker facade; canonical ModelScope package source for the Trae projection",
        proposed_target="vaws model",
    ),
    ".agents/skills/modelscope/scripts/modelscope_download_status.py": _cls(
        "mechanics", "internal",
        "size-comparison CLI also covered by modelscope_auto status",
        target=".agents/skills/modelscope/scripts/modelscope_auto.py",
        target_kind="local-entry",
    ),
    ".agents/skills/modelscope/scripts/verify_modelscope_sha256.py": _cls(
        "mechanics", "payload",
        "spawned by modelscope_auto; not the agent-facing facade",
    ),
    ".agents/skills/npu-fleet-monitor/scripts/manage_monitor.py": _cls(
        "mechanics", "supported",
        "ensure/status/restart/stop of the loopback dashboard; locates the vaws-top checkout, does not count provider CLIs",
        proposed_target="vaws machine",
        external_owner="vaws-top",
    ),
    ".agents/skills/remote-code-parity/scripts/gc_runtime_cache.py": _cls(
        "mechanics", "supported",
        "prunes the container cache root",
        proposed_target="vaws sync",
    ),
    ".agents/skills/remote-code-parity/scripts/install_consent.py": _cls(
        "mechanics", "supported",
        "records install consent and sync-mode overrides",
        proposed_target="vaws sync",
    ),
    ".agents/skills/remote-code-parity/scripts/parity_sync.py": _cls(
        "mechanics", "supported",
        "canonical local-to-container snapshot sync; C3 still owns long-running code identity",
        proposed_target="vaws sync",
    ),
    ".agents/skills/remote-code-parity/scripts/parity_watch.py": _cls(
        "mechanics", "supported",
        "watcher that re-publishes snapshots",
        proposed_target="vaws sync",
    ),
    ".agents/skills/remote-code-parity/scripts/remote_code_parity.py": _cls(
        "mechanics", "payload",
        "implementation payload spawned by parity_sync, toolbox adapters and the coordinator",
    ),
    ".agents/skills/remote-code-parity/scripts/transport_benchmark.py": _cls(
        "mechanics", "harness",
        "bundle vs receive-pack transport micro-benchmark",
    ),
    ".agents/skills/repo-init/scripts/install_gh_user.py": _cls(
        "mechanics", "supported",
        "user-local gh install; bare sys.argv",
        proposed_target="vaws workspace",
    ),
    ".agents/skills/repo-init/scripts/repo_init_probe.py": _cls(
        "mechanics", "supported",
        "read-only local facts (gh, auth, submodules, remotes)",
        proposed_target="vaws workspace",
    ),
    ".agents/skills/repo-init/scripts/repo_init_profile.py": _cls(
        "mechanics", "supported",
        "plan/apply of workspace_profile ensure; username choice stays a user gate",
        proposed_target="vaws workspace",
    ),
    ".agents/skills/repo-init/scripts/repo_topology.py": _cls(
        "mechanics", "supported",
        "fork/remote topology via gh+git",
        proposed_target="vaws workspace",
    ),
    ".agents/skills/repo-init/scripts/resolve_vllm_ci_pin.py": _cls(
        "mechanics", "supported",
        "parses the vllm-ascend CI pin",
        proposed_target="vaws workspace",
    ),
    ".agents/skills/session-management/scripts/npu_coordination.py": _cls(
        "mechanics", "supported",
        "optional host-shared SQLite NPU queue; observation from vaws-top is not allocation authority",
        proposed_target="vaws session",
    ),
    ".agents/skills/session-management/scripts/session_create.py": _cls(
        "mechanics", "supported",
        "worktree + container + lease creation",
        proposed_target="vaws session",
    ),
    ".agents/skills/session-management/scripts/session_diff.py": _cls(
        "mechanics", "supported",
        "git diff of worktree + submodules",
        proposed_target="vaws session",
    ),
    ".agents/skills/session-management/scripts/session_gc.py": _cls(
        "mechanics", "supported",
        "stale metadata GC with --reap-dead",
        proposed_target="vaws session",
    ),
    ".agents/skills/session-management/scripts/session_group.py": _cls(
        "mechanics", "supported",
        "create/status/list/teardown of session groups",
        proposed_target="vaws session",
    ),
    ".agents/skills/session-management/scripts/session_list.py": _cls(
        "mechanics", "supported",
        "read local session/lease state",
        proposed_target="vaws session",
    ),
    ".agents/skills/session-management/scripts/session_remove.py": _cls(
        "mechanics", "supported",
        "teardown service/container/worktree/leases",
        proposed_target="vaws session",
    ),
    ".agents/skills/session-management/scripts/session_status.py": _cls(
        "mechanics", "supported",
        "one session plus live probe",
        proposed_target="vaws session",
    ),
    ".agents/skills/vllm-ascend-benchmark/scripts/bench_compare.py": _cls(
        "mixed", "supported",
        "multi-state checkout+serve+bench loop is mechanics; exploratory delta table is not a comparability certificate",
        proposed_target="vaws bench",
    ),
    ".agents/skills/vllm-ascend-benchmark/scripts/bench_run.py": _cls(
        "mechanics", "supported",
        "vllm bench serve with warmup/multi-run; writes results",
        proposed_target="vaws bench",
    ),
    ".agents/skills/vllm-ascend-change-validation/scripts/change_validation.py": _cls(
        "mixed", "supported",
        "plan maps diffs by regex (judgment); link/finalize enforce child manifests, run-type coverage and artifacts",
        proposed_target="guidance",
    ),
    ".agents/skills/vllm-ascend-correctness-validation/scripts/aisbench_adapter.py": _cls(
        "mechanics", "supported",
        "prepare/normalize of AISBench CSV",
        proposed_target="vaws bench",
    ),
    ".agents/skills/vllm-ascend-correctness-validation/scripts/correctness_run.py": _cls(
        "mixed", "supported",
        "init/compare plus execution-block identity and shared certificate consume; whether a class is acceptable is judgment",
        proposed_target="vaws bench",
    ),
    ".agents/skills/vllm-ascend-correctness-validation/scripts/remote_correctness_harness.py": _cls(
        "mechanics", "payload",
        "remote-side offline/online case runner",
    ),
    ".agents/skills/vllm-ascend-distributed-debug/scripts/distributed_debug.py": _cls(
        "mixed", "supported",
        "init/ingest/analyze over agent-normalized events; completed-without-mismatch stays distinct from two-state certificates",
        proposed_target="guidance",
    ),
    ".agents/skills/vllm-ascend-graph-debug/scripts/graph_debug_case.py": _cls(
        "mixed", "supported",
        "compare is a JSONL numeric diff with snapshot-sidecar identity; init/record/finalize are the hypothesis ledger",
        proposed_target="guidance",
    ),
    ".agents/skills/vllm-ascend-pd-serving/scripts/pd_serving.py": _cls(
        "mixed", "supported",
        "start/status/smoke/stop spawn serve_start.py per group member; plan encodes connector-config choices",
        proposed_target="vaws serve",
    ),
    ".agents/skills/vllm-ascend-performance-regression/scripts/performance_regression.py": _cls(
        "mixed", "supported",
        "normalize rejects non-finite/order/identity errors; analyze consumes per-measurement observations and shared certificates; thresholds stay judgment",
        proposed_target="guidance",
    ),
    ".agents/skills/vllm-ascend-serving/scripts/serve_probe_npus.py": _cls(
        "mechanics", "compatibility",
        "legacy --machine npu-smi probe still named in AGENTS.md; not deleted in this inventory turn",
        proposed_target="vaws machine",
    ),
    ".agents/skills/vllm-ascend-serving/scripts/serve_start.py": _cls(
        "mechanics", "supported",
        "canonical vllm serve start; still path-spawns parity_sync (C3)",
        proposed_target="vaws serve",
    ),
    ".agents/skills/vllm-ascend-serving/scripts/serve_status.py": _cls(
        "mechanics", "supported",
        "health probe of the tracked service",
        proposed_target="vaws serve",
    ),
    ".agents/skills/vllm-ascend-serving/scripts/serve_stop.py": _cls(
        "mechanics", "supported",
        "stop the tracked service (--force)",
        proposed_target="vaws serve",
    ),
    ".trae/skills/modelscope/scripts/download_from_modelscope.py": _cls(
        "mechanics", "generated",
        "Trae projection generated from the canonical download script by sync_claude_skills.py",
        target=".agents/skills/modelscope/scripts/download_from_modelscope.py",
        target_kind="local-entry",
    ),
    ".trae/skills/modelscope/scripts/modelscope_auto.py": _cls(
        "mechanics", "generated",
        "Trae projection generated from the canonical modelscope_auto.py facade",
        target=".agents/skills/modelscope/scripts/modelscope_auto.py",
        target_kind="local-entry",
    ),
    ".trae/skills/modelscope/scripts/modelscope_download_status.py": _cls(
        "mechanics", "generated",
        "Trae projection generated from the canonical download-status script",
        target=".agents/skills/modelscope/scripts/modelscope_download_status.py",
        target_kind="local-entry",
    ),
    ".trae/skills/modelscope/scripts/verify_modelscope_sha256.py": _cls(
        "mechanics", "generated",
        "Trae projection generated from the canonical verify script",
        target=".agents/skills/modelscope/scripts/verify_modelscope_sha256.py",
        target_kind="local-entry",
    ),
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
    if rel.startswith(".agents/maturation/"):
        return ".agents/maturation"
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
    if rel.startswith(".agents/deps/"):
        return "source-map"
    if rel.startswith((".agents/policy/", ".agents/leak-guard/")):
        return "policy"
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
        return True
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


def load_external_owners(repo_root: Path) -> dict[str, dict]:
    """Read committed provider pins. Never fetch, import or execute providers."""
    owners: dict[str, dict] = {}
    for name, filename in DEP_DECLARATIONS:
        path = repo_root / ".agents" / "deps" / filename
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        surface = data.get("consumed_surface")
        owners[name] = {
            "declaration": f".agents/deps/{filename}",
            "name": data.get("name", name),
            "repository": data.get("repository"),
            "commit": data.get("commit"),
            "root_env": data.get("root_env"),
            "consumed_surface": surface if isinstance(surface, dict) else None,
            "note": data.get("note"),
            "source_availability": "uninspected",
        }
    return owners


def inspect_context(repo_root: Path) -> dict:
    ctx = {
        "accepted_public_main": ACCEPTED_PUBLIC_MAIN,
        "accepted_public_tree": ACCEPTED_PUBLIC_TREE,
        "original_pr85": ORIGINAL_PR85,
        "merge_preview_tree": MERGE_PREVIEW_TREE,
        "scaffold_source": SCAFFOLD_SOURCE,
        "definition": (
            "tracked or untracked-unignored *.py outside submodules/tests "
            "with a __main__ guard or __main__.py"
        ),
        "note": (
            "Census is AST of inspected files plus committed .agents/deps pins. "
            "The commit SHA that lands this overlay is an output, not an input."
        ),
    }
    try:
        head = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        porcelain = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        ctx["inspected_head"] = head
        ctx["inspected_tree"] = tree
        ctx["working_tree_dirty"] = bool(porcelain)
    except (subprocess.CalledProcessError, FileNotFoundError):
        ctx["inspected_head"] = None
        ctx["inspected_tree"] = None
        ctx["working_tree_dirty"] = None
    return ctx


def _resolved_overlay(rel: str, meta: dict[str, str]) -> dict[str, str]:
    resolved = dict(meta)
    resolved.setdefault("target_kind", "self")
    if not resolved.get("target"):
        if resolved["target_kind"] in {"self", "non-command"}:
            resolved["target"] = rel if resolved["target_kind"] == "self" else resolved.get("support_role", rel)
        else:
            resolved["target"] = rel
    return resolved


def _validate_overlay(
    rel: str,
    meta: dict[str, str],
    entries: set[str],
    owners: dict[str, dict],
) -> list[Finding]:
    findings: list[Finding] = []
    responsibility = meta.get("responsibility")
    if responsibility not in RESPONSIBILITIES:
        findings.append(Finding("bad-responsibility", rel, f"unknown responsibility {responsibility!r}"))
    support_role = meta.get("support_role")
    if support_role not in SUPPORT_ROLES:
        findings.append(Finding("bad-support-role", rel, f"unknown support role {support_role!r}"))
    kind = meta.get("target_kind", "self")
    if kind not in TARGET_KINDS:
        findings.append(Finding("bad-target-kind", rel, f"unknown target_kind {kind!r}"))
    target = meta.get("target") or rel
    if kind == "local-entry" and target not in entries:
        findings.append(
            Finding(
                "local-target-missing",
                rel,
                f"local target {target!r} is not a discovered entry point",
            )
        )
    owner = meta.get("external_owner")
    if kind == "external":
        if not owner:
            findings.append(Finding("bad-external-target", rel, "external target_kind requires external_owner"))
        elif owner not in owners:
            findings.append(
                Finding(
                    "unknown-external-owner",
                    rel,
                    f"external_owner {owner!r} is not a committed dependency declaration",
                )
            )
    elif owner and owner not in owners:
        findings.append(
            Finding(
                "unknown-external-owner",
                rel,
                f"external_owner {owner!r} is not a committed dependency declaration",
            )
        )
    proposed = meta.get("proposed_target")
    if proposed and not (proposed.startswith("vaws ") or proposed in NON_COMMAND_TARGETS):
        findings.append(Finding("bad-proposed-target", rel, f"proposed_target {proposed!r} is not a labelled historical option"))
    return findings


def extract_delimited_table(text: str, begin: str, end: str) -> list[str]:
    if begin not in text or end not in text:
        return []
    section = text.split(begin, 1)[1].split(end, 1)[0]
    return [line for line in section.splitlines() if line.startswith("| `")]


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

    owners = load_external_owners(repo_root)
    entry_set = set(entries)
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
        raw = CLASSIFICATION.get(rel)
        classification = _resolved_overlay(rel, raw) if raw else None
        if classification is None:
            findings.append(Finding("unclassified", rel, "entry point has no classification overlay"))
        else:
            findings.extend(_validate_overlay(rel, classification, entry_set, owners))
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
        if rel not in entry_set:
            findings.append(Finding("stale-classification", rel, "classified path is no longer an entry point"))

    by_area: dict[str, int] = {}
    by_responsibility: dict[str, int] = {name: 0 for name in RESPONSIBILITIES}
    by_support_role: dict[str, int] = {name: 0 for name in SUPPORT_ROLES}
    by_style: dict[str, int] = {}
    by_skill: dict[str, int] = {}
    by_target_kind: dict[str, int] = {}
    current_groups: dict[str, list[str]] = {name: [] for name in SUPPORT_ROLES}
    proposed_groups: dict[str, list[str]] = {}
    for record in records:
        by_area[record.area] = by_area.get(record.area, 0) + 1
        by_style[record.parser_style] = by_style.get(record.parser_style, 0) + 1
        if record.skill:
            by_skill[record.skill] = by_skill.get(record.skill, 0) + 1
        if not record.classification:
            continue
        cls = record.classification
        responsibility = cls["responsibility"]
        support_role = cls["support_role"]
        if responsibility in by_responsibility:
            by_responsibility[responsibility] += 1
        if support_role in by_support_role:
            by_support_role[support_role] += 1
            current_groups[support_role].append(record.path)
        kind = cls.get("target_kind", "self")
        by_target_kind[kind] = by_target_kind.get(kind, 0) + 1
        proposed = cls.get("proposed_target")
        if proposed:
            proposed_groups.setdefault(proposed, []).append(record.path)

    unreferenced = [
        r.path
        for r in records
        if not any(ref.kind not in {"test", "mirror"} for ref in r.references)
    ]
    classified = len(records) - len([f for f in findings if f.code == "unclassified"])
    if not quiet:
        progress(f"classified {classified}/{len(records)}", counter=counter)

    return {
        "status": "passed" if not findings else "failed",
        "repo_root_name": repo_root.name,
        "measurement": inspect_context(repo_root),
        "historical_snapshot": dict(HISTORICAL_SNAPSHOT),
        "proposed_surface": {
            "status": "historical-unimplemented",
            "label": "original #85 thirteen-command design; not a current command list or CI invariant",
            "agent_command_count": HISTORICAL_SNAPSHOT["proposed_agent_command_count"],
            "agent_commands": list(HISTORICAL_PROPOSED_COMMANDS),
            "verb_count": HISTORICAL_SNAPSHOT["proposed_verb_count"],
            "from_overlay": {key: sorted(val) for key, val in sorted(proposed_groups.items())},
        },
        "external_owners": owners,
        "entry_point_count": len(records),
        "counts": {
            "argparse_importing_files": argparse_importers,
            "by_area": dict(sorted(by_area.items())),
            "by_parser_style": dict(sorted(by_style.items())),
            "by_responsibility": by_responsibility,
            "by_support_role": by_support_role,
            "by_target_kind": dict(sorted(by_target_kind.items())),
            "by_skill": dict(sorted(by_skill.items())),
            "skills_with_entry_points": len(by_skill),
            "supported": by_support_role["supported"],
        },
        "current_surface": {key: sorted(val) for key, val in current_groups.items()},
        "unreferenced_outside_tests": unreferenced,
        "entry_points": [asdict(r) for r in records],
        "finding_count": len(findings),
        "findings": [asdict(f) for f in findings],
    }


def render_markdown(payload: dict) -> str:
    lines = [
        "| Entry point | Style | Verbs | Options | Refs | Responsibility | Support role | Current target | Proposed |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for record in payload["entry_points"]:
        cls = record["classification"] or {}
        verbs = ", ".join(record["verbs"]) or "-"
        refs = ", ".join(f"{k}:{v}" for k, v in record["reference_kinds"].items()) or "-"
        lines.append(
            "| `{path}` | {style} | {verbs} | {n_opts} | {refs} | {resp} | {role} | {target} | {proposed} |".format(
                path=record["path"],
                style=record["parser_style"],
                verbs=verbs,
                n_opts=len(record["options"]),
                refs=refs,
                resp=cls.get("responsibility", "unclassified"),
                role=cls.get("support_role", "-"),
                target=cls.get("target", "-"),
                proposed=cls.get("proposed_target", "-"),
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
        summary = {
            k: payload[k]
            for k in (
                "status",
                "entry_point_count",
                "counts",
                "measurement",
                "historical_snapshot",
                "proposed_surface",
                "external_owners",
                "finding_count",
                "findings",
            )
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
