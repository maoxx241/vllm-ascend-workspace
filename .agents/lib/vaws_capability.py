"""Workspace capability report built on the knowledge degradation shape.

Capabilities are what an agent can do, not which files exist. Each
capability uses the same degradation entry fields as
``vaws_knowledge_client`` so one learned shape covers both surfaces.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from vaws_dependency import (
    REMEDY,
    USABLE_STATES,
    VAWS_TOP_NAME,
    all_packages,
    inspect,
    require_package,
)
from vaws_knowledge_client import _probe_shared, default_paths
from vaws_knowledge_shared import AVAILABLE as SHARED_AVAILABLE
from vaws_result_envelope import (
    dumps,
    make_attempt,
    make_command,
    make_environment,
    make_evidence,
    make_failure,
    make_next_step,
    make_operation,
    make_part,
    new_envelope,
    outcome_from_parts,
    validate_envelope,
)

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_RELATIVE = ".agents/lib/vaws_remote_dev_plugin.py"
TRACKED_MCP_CONFIGS = (".mcp.json", ".cursor/mcp.json")
CAPABILITY_ORDER = (
    "remote_endpoints",
    "resolver_registration",
    "task_pool",
    "host_npu_authority",
    "fleet_observation",
    "shared_knowledge",
    "conformance_kit",
)
CAPABILITY_DEPS = {
    "remote_endpoints": ("vaws-remote-dev",),
    "resolver_registration": ("vaws-remote-dev",),
    "task_pool": ("vaws-coordinator",),
    "host_npu_authority": ("vaws-coordinator",),
    "fleet_observation": (VAWS_TOP_NAME,),
    "shared_knowledge": (),
    "conformance_kit": ("vaws-knowledge",),
}
SOURCE_REPOS = {
    "vaws-remote-dev": "vllm-ascend-workspace/remote-dev",
    "vaws-coordinator": "vllm-ascend-workspace/vaws-coordinator",
    "vaws-knowledge": "vllm-ascend-workspace/vaws-knowledge",
    VAWS_TOP_NAME: "vllm-ascend-workspace/vaws-top",
}


def _usable(state: str) -> bool:
    return state in USABLE_STATES


def _dep_degradation(
    info: Mapping[str, Any],
    *,
    layer: str = "dependency",
    effect: str,
) -> dict[str, Any]:
    name = str(info.get("name") or "")
    if _usable(str(info.get("state") or "")) and info.get("state") != "ready":
        effect = (
            f"runs an off-spec install of {name} "
            f"(installed {info.get('installed_version')} "
            f"commit {info.get('installed_commit')}; "
            f"lock {info.get('locked_version')} "
            f"commit {info.get('locked_commit')}); behaviour may differ"
        )
    return {
        "layer": layer,
        "detail": (
            f"{name} is {info.get('state')}"
            + (f": {'; '.join(info.get('problems') or ())}" if info.get("problems") else "")
        ),
        "effect": effect,
        "remedy": str(info.get("remedy") or REMEDY),
        "expected_source_repo": SOURCE_REPOS.get(name),
        "expected_source_ref": info.get("locked_commit") or info.get("required_version"),
    }


def _shared_degradation(repo_root: Path) -> dict[str, Any] | None:
    """Reuse the knowledge client's shared-layer degradation entry verbatim."""
    capability = _probe_shared(default_paths(repo_root)["shared_dir"])
    if capability["status"] == SHARED_AVAILABLE:
        return None
    return {
        "layer": "shared",
        "status": capability.get("status", "absent"),
        "detail": capability.get("detail", ""),
        "remedy": capability.get("remedy") or (
            "python3 .agents/scripts/knowledge_shared_cache.py import "
            "--from <clone>/corpus/verified "
            "--source-repo vllm-ascend-workspace/vaws-knowledge --source-ref <commit-sha>"
        ),
        "source_repo": capability.get("source_repo"),
        "source_ref": capability.get("source_ref"),
        "expected_source_repo": capability.get("expected_source_repo"),
        "expected_source_ref": capability.get("expected_source_ref"),
        "effect": "degraded to project+candidate; shared facts were not consulted",
    }


def _mcp_resolvers_configured(repo_root: Path) -> bool:
    for relative in TRACKED_MCP_CONFIGS:
        path = repo_root / relative
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        servers = data.get("mcpServers")
        if not isinstance(servers, dict):
            continue
        for entry in servers.values():
            if not isinstance(entry, dict):
                continue
            env = entry.get("env")
            if isinstance(env, dict) and str(env.get("REMOTE_DEV_RESOLVERS") or "").strip():
                return True
    return False


def _capability(
    *,
    available: bool,
    degraded: bool,
    depends_on: tuple[str, ...] | list[str],
    degradation: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "available": available,
        "degraded": degraded,
        "depends_on": list(depends_on),
        "degradation": degradation,
    }


def evaluate_capabilities(
    *,
    repo_root: Path = ROOT,
    env: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    del env  # interpreter state is the source of truth; env is kept for envelope shape
    deps = all_packages(repo_root)
    capabilities: dict[str, Any] = {}

    remote = deps["vaws-remote-dev"]
    remote_ok = _usable(remote["state"])
    remote_deg: list[dict[str, Any]] = []
    if remote["state"] != "ready":
        remote_deg.append(
            _dep_degradation(
                remote,
                effect="remote companion tools cannot start; host+port recipes that go through the package fail closed",
            )
        )
    capabilities["remote_endpoints"] = _capability(
        available=remote_ok,
        degraded=bool(remote_deg),
        depends_on=CAPABILITY_DEPS["remote_endpoints"],
        degradation=remote_deg,
    )

    resolver_deg: list[dict[str, Any]] = []
    plugin = repo_root / PLUGIN_RELATIVE
    if remote["state"] != "ready":
        resolver_deg.append(
            _dep_degradation(
                remote,
                layer="dependency",
                effect=(
                    "only host+port endpoints resolve; --machine / --session-id / "
                    "worktree auto-bind silently unavailable"
                ),
            )
        )
    if not plugin.is_file():
        resolver_deg.append(
            {
                "layer": "scaffold",
                "detail": f"{PLUGIN_RELATIVE} is not reachable from the scaffold root",
                "effect": (
                    "only host+port endpoints resolve; --machine / --session-id / "
                    "worktree auto-bind silently unavailable"
                ),
                "remedy": "restore .agents/lib/vaws_remote_dev_plugin.py from the scaffold repository",
                "expected_source_repo": "vllm-ascend-workspace/vllm-ascend-workspace",
                "expected_source_ref": "main",
            }
        )
    if not _mcp_resolvers_configured(repo_root):
        resolver_deg.append(
            {
                "layer": "client_config",
                "detail": "REMOTE_DEV_RESOLVERS is absent from tracked .mcp.json / .cursor/mcp.json",
                "effect": (
                    "only host+port endpoints resolve; --machine / --session-id / "
                    "worktree auto-bind silently unavailable"
                ),
                "remedy": (
                    "set REMOTE_DEV_RESOLVERS=.agents/lib/vaws_remote_dev_plugin.py:setup "
                    "in the tracked remote-dev MCP server env"
                ),
                "expected_source_repo": "vllm-ascend-workspace/vllm-ascend-workspace",
                "expected_source_ref": "main",
            }
        )
    capabilities["resolver_registration"] = _capability(
        available=remote_ok and plugin.is_file() and _mcp_resolvers_configured(repo_root),
        degraded=bool(resolver_deg),
        depends_on=CAPABILITY_DEPS["resolver_registration"],
        degradation=resolver_deg,
    )

    coord = deps["vaws-coordinator"]
    coord_ok = _usable(coord["state"])
    coord_deg: list[dict[str, Any]] = []
    if coord["state"] != "ready":
        coord_deg.append(
            _dep_degradation(
                coord,
                effect="vaws_* task tools and the native session hook cannot exec the coordinator",
            )
        )
    capabilities["task_pool"] = _capability(
        available=coord_ok,
        degraded=bool(coord_deg),
        depends_on=CAPABILITY_DEPS["task_pool"],
        degradation=coord_deg,
    )

    host_ok = False
    if coord_ok:
        try:
            from vaws_coordinator.host import vaws_npu_coordination as _host

            host_ok = bool(getattr(_host, "__file__", None))
        except ImportError:
            host_ok = False
    host_deg: list[dict[str, Any]] = []
    if not host_ok:
        host_deg.append(
            _dep_degradation(
                coord,
                effect="host NPU queue protocol cannot be imported from vaws_coordinator.host",
            )
        )
    elif coord["state"] != "ready":
        host_deg.append(
            _dep_degradation(
                coord,
                effect="host NPU queue protocol cannot be imported from vaws_coordinator.host",
            )
        )
    capabilities["host_npu_authority"] = _capability(
        available=host_ok,
        degraded=bool(host_deg),
        depends_on=CAPABILITY_DEPS["host_npu_authority"],
        degradation=host_deg,
    )

    top = deps[VAWS_TOP_NAME]
    top_ok = _usable(top["state"])
    top_deg: list[dict[str, Any]] = []
    if top["state"] != "ready":
        top_deg.append(
            _dep_degradation(
                top,
                effect=f"npu-fleet-monitor cannot exec uvx {VAWS_TOP_NAME}",
            )
        )
    capabilities["fleet_observation"] = _capability(
        available=top_ok,
        degraded=bool(top_deg),
        depends_on=CAPABILITY_DEPS["fleet_observation"],
        degradation=top_deg,
    )

    shared_entry = _shared_degradation(repo_root)
    capabilities["shared_knowledge"] = _capability(
        available=shared_entry is None,
        degraded=shared_entry is not None,
        depends_on=list(CAPABILITY_DEPS["shared_knowledge"]),
        degradation=[shared_entry] if shared_entry else [],
    )

    kit = deps["vaws-knowledge"]
    kit_ok = _usable(kit["state"])
    kit_deg: list[dict[str, Any]] = []
    if kit["state"] != "ready":
        kit_deg.append(
            _dep_degradation(
                kit,
                effect="the vaws-knowledge engine is not available to knowledge client tests",
            )
        )
    capabilities["conformance_kit"] = _capability(
        available=kit_ok,
        degraded=bool(kit_deg),
        depends_on=CAPABILITY_DEPS["conformance_kit"],
        degradation=kit_deg,
    )

    flat: list[dict[str, Any]] = []
    for name in CAPABILITY_ORDER:
        flat.extend(capabilities[name]["degradation"])
    return {
        "deps": deps,
        "capabilities": capabilities,
        "degraded": any(capabilities[name]["degraded"] for name in CAPABILITY_ORDER),
        "degradation": flat,
        "warnings": [],
        "acknowledged_drift": [],
        "recent_hook_degradations": [],
    }


def _sync_actions(report: Mapping[str, Any]) -> list[dict[str, str | None]]:
    seen: set[str] = set()
    actions: list[dict[str, str | None]] = []
    for entry in report.get("degradation") or []:
        remedy = str(entry.get("remedy") or "").strip()
        if not remedy or remedy in seen:
            continue
        seen.add(remedy)
        actions.append(
            {
                "description": str(entry.get("effect") or entry.get("detail") or "install a missing dependency"),
                "command": remedy,
                "ref": None,
            }
        )
    if not actions:
        actions.append(
            {
                "description": "no sync required",
                "command": None,
                "ref": None,
            }
        )
    return actions


def _parts_for(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    parts = [
        make_part(
            unit="dependency_lock",
            unit_kind="check",
            outcome="success",
            summary="pyproject.toml and uv.lock describe the three workspace packages",
        )
    ]
    for name in CAPABILITY_ORDER:
        cap = report["capabilities"][name]
        if cap["degraded"]:
            outcome = "blocked"
            layer = "tool"
            reason = "dependency_unavailable"
        else:
            outcome = "success"
            layer = None
            reason = None
        parts.append(
            make_part(
                unit=name,
                unit_kind="capability",
                outcome=outcome,
                layer=layer,
                reason_code=reason,
                summary=(
                    f"{name} degraded"
                    if cap["degraded"]
                    else f"{name} available"
                ),
            )
        )
    return parts


def build_doctor_envelope(
    *,
    argv: list[str],
    repo_root: Path = ROOT,
    env: Mapping[str, str] | None = None,
    pin_error: Exception | None = None,
) -> dict[str, Any]:
    command = make_command(
        argv=argv,
        cwd=str(repo_root),
        env_keys=["HOME", "CI", "VAWS_SKIP_VENV_REEXEC", "VAWS_KNOWLEDGE_KIT_ROOT"],
    )
    attempt = make_attempt(command=command, reproduce=command["display"])
    environment = make_environment(source="unknown")
    operation = make_operation(
        entry_point=".agents/scripts/vaws_deps.py",
        action="doctor",
        skill=None,
        target_kind="local",
    )
    if pin_error is not None:
        envelope = new_envelope(
            operation=operation,
            outcome="failure",
            exit_code=2,
            summary=f"dependency spec is invalid: {pin_error}"[:400],
            attempt=attempt,
            environment=environment,
            evidence=make_evidence(),
            next_step=make_next_step(
                actions=[
                    {
                        "description": "fix pyproject.toml or uv.lock named in the failure",
                        "command": REMEDY,
                        "ref": None,
                    }
                ]
            ),
            failure=make_failure(
                layer="caller",
                reason_code="bad_arguments",
                message=str(pin_error),
                attribution_basis=["inspect() rejected pyproject.toml or uv.lock"],
                confidence="high",
                ruled_out=["transport", "remote_env", "remote_workload", "device"],
            ),
            extensions={"capability_report": None},
        )
        validate_envelope(envelope)
        return envelope

    report = evaluate_capabilities(repo_root=repo_root, env=env)
    parts = _parts_for(report)
    derived = outcome_from_parts(parts)
    degraded = bool(report["degraded"])
    outcome = derived
    if degraded and outcome == "success":
        outcome = "partial"
    exit_code = 0 if outcome == "success" else 1
    missing = [name for name, cap in report["capabilities"].items() if cap["degraded"]]
    summary = (
        "workspace capabilities are available"
        if not degraded
        else f"workspace capabilities degraded: {', '.join(missing)}"
    )[:400]
    failure = None
    if outcome in {"partial", "failure", "blocked"}:
        failure = make_failure(
            layer="tool",
            reason_code="path_missing",
            message=summary,
            attribution_basis=[
                "vaws_deps doctor inspected installed packages, uv.lock, and the local knowledge cache",
                f"{len(report['degradation'])} degradation entries were recorded",
            ],
            confidence="high",
            ruled_out=["transport", "remote_env", "remote_workload", "device"],
        )
    envelope = new_envelope(
        operation=operation,
        outcome=outcome,
        exit_code=exit_code,
        summary=summary,
        attempt=attempt,
        environment=environment,
        evidence=make_evidence(),
        next_step=make_next_step(actions=_sync_actions(report)),
        failure=failure,
        parts=parts,
        extensions={"capability_report": report},
    )
    validate_envelope(envelope)
    return envelope


def dumps_doctor(envelope: Mapping[str, Any]) -> str:
    return dumps(dict(envelope))


def checkout_usable(name: str, env: Mapping[str, str] | None = None) -> bool:
    del env
    return inspect(name)["state"] in USABLE_STATES


def package_required(name: str) -> dict[str, Any]:
    return require_package(name)
