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
    USABLE_STATES,
    VAWS_TOP_NAME,
    acknowledged_drift,
    all_pins,
    inspect,
    read_hook_degradations,
    resolve,
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
    "remote_endpoints": ("remote-dev",),
    "resolver_registration": ("remote-dev",),
    "task_pool": ("vaws-coordinator",),
    "host_npu_authority": ("vaws-coordinator",),
    "fleet_observation": (VAWS_TOP_NAME,),
    "shared_knowledge": (),
    "conformance_kit": ("vaws-knowledge",),
}


def _usable(state: str) -> bool:
    return state in USABLE_STATES


def _service_api_incompatible(info: Mapping[str, Any]) -> dict[str, Any] | None:
    api = info.get("service_api") or {}
    if api.get("state") != "incompatible":
        return None
    accepted = api.get("accepted") or {}
    lo, hi = accepted.get("min"), accepted.get("max")
    return {
        "layer": "dependency",
        "detail": (
            f"{info.get('name')} service API is incompatible: "
            f"supports {api.get('supports')} vs accepted {lo}..{hi}"
        ),
        "effect": "dependent capabilities are degraded/unavailable; execution is not blocked",
        "remedy": (
            f"bump the pin or update the checkout to a build whose `supports` includes {lo}..{hi}"
        ),
        "expected_source_repo": None,
        "expected_source_ref": None,
    }


def _apply_service_api(
    capabilities: dict[str, Any],
    pins: Mapping[str, Mapping[str, Any]],
    deps: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    warnings: list[str] = []
    for dep_name, info in deps.items():
        api = info.get("service_api") or {}
        pin = pins.get(dep_name) or {}
        if api.get("state") == "undeclared":
            detail = api.get("detail") or "service-api.json is missing or predates the contract"
            warnings.append(f"{dep_name} service API is undeclared: {detail}")
        incompatible = _service_api_incompatible(info)
        if incompatible is None:
            continue
        incompatible["expected_source_repo"] = pin.get("repository")
        incompatible["expected_source_ref"] = pin.get("commit") or pin.get("ref")
        for cap_name, depends in CAPABILITY_DEPS.items():
            if dep_name not in depends:
                continue
            cap = capabilities[cap_name]
            cap["available"] = False
            cap["degraded"] = True
            cap["degradation"].append(incompatible)
    return warnings


def _dep_degradation(
    pin: Mapping[str, Any],
    info: Mapping[str, Any],
    *,
    layer: str = "dependency",
    effect: str,
) -> dict[str, Any]:
    return {
        "layer": layer,
        "detail": (
            f"{info.get('name')} checkout is {info.get('state')} at {info.get('path')}"
            + (f": {'; '.join(info.get('problems') or ())}" if info.get("problems") else "")
        ),
        "effect": effect,
        "remedy": str(pin.get("bootstrap") or ""),
        "expected_source_repo": pin.get("repository"),
        "expected_source_ref": pin.get("commit") or pin.get("ref"),
    }


def _shared_degradation(repo_root: Path) -> dict[str, Any] | None:
    """Reuse the knowledge client's shared-layer degradation entry verbatim."""
    capability = _probe_shared(default_paths(repo_root)["shared_dir"])
    if capability["status"] == SHARED_AVAILABLE:
        return None
    # Same fields as vaws_knowledge_client.query_knowledge shared-layer missing[].
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
    pins = all_pins()
    deps = {name: inspect(name, env, repo_root=repo_root) for name in pins}
    capabilities: dict[str, Any] = {}

    remote = deps["remote-dev"]
    remote_pin = pins["remote-dev"]
    remote_ok = _usable(remote["state"])
    remote_deg: list[dict[str, Any]] = []
    if remote["state"] != "ready":
        remote_deg.append(
            _dep_degradation(
                remote_pin,
                remote,
                effect="remote companion tools cannot start; host+port recipes that go through the launcher fail closed",
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
                remote_pin,
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
    coord_pin = pins["vaws-coordinator"]
    coord_ok = _usable(coord["state"])
    coord_deg: list[dict[str, Any]] = []
    if coord["state"] != "ready":
        coord_deg.append(
            _dep_degradation(
                coord_pin,
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

    host_module = Path(coord["path"]) / "host/vaws_npu_coordination.py" if coord.get("path") else None
    host_ok = coord_ok and bool(host_module and host_module.is_file())
    host_deg: list[dict[str, Any]] = []
    if not host_ok:
        host_deg.append(
            _dep_degradation(
                coord_pin,
                coord,
                effect="host NPU queue protocol cannot be loaded from the coordinator checkout",
            )
        )
    elif coord["state"] != "ready":
        host_deg.append(
            _dep_degradation(
                coord_pin,
                coord,
                effect="host NPU queue protocol cannot be loaded from the coordinator checkout",
            )
        )
    capabilities["host_npu_authority"] = _capability(
        available=host_ok,
        degraded=bool(host_deg),
        depends_on=CAPABILITY_DEPS["host_npu_authority"],
        degradation=host_deg,
    )

    top = deps[VAWS_TOP_NAME]
    top_pin = pins[VAWS_TOP_NAME]
    top_ok = _usable(top["state"])
    top_deg: list[dict[str, Any]] = []
    if top["state"] != "ready":
        top_deg.append(
            _dep_degradation(
                top_pin,
                top,
                effect="npu-fleet-monitor cannot locate the standalone fleet dashboard checkout",
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
    kit_pin = pins["vaws-knowledge"]
    kit_ok = _usable(kit["state"])
    kit_deg: list[dict[str, Any]] = []
    if kit["state"] != "ready":
        kit_deg.append(
            _dep_degradation(
                kit_pin,
                kit,
                effect="the vaws-knowledge conformance kit is not available to knowledge client tests",
            )
        )
    capabilities["conformance_kit"] = _capability(
        available=kit_ok,
        degraded=bool(kit_deg),
        depends_on=CAPABILITY_DEPS["conformance_kit"],
        degradation=kit_deg,
    )

    warnings = _apply_service_api(capabilities, pins, deps)
    flat: list[dict[str, Any]] = []
    for name in CAPABILITY_ORDER:
        flat.extend(capabilities[name]["degradation"])
    drift = acknowledged_drift(env)
    return {
        "deps": deps,
        "capabilities": capabilities,
        "degraded": any(capabilities[name]["degraded"] for name in CAPABILITY_ORDER),
        "degradation": flat,
        "warnings": warnings,
        "acknowledged_drift": drift,
        "recent_hook_degradations": read_hook_degradations(repo_root=repo_root),
    }


def _bootstrap_actions(report: Mapping[str, Any]) -> list[dict[str, str | None]]:
    seen: set[str] = set()
    actions: list[dict[str, str | None]] = []
    for entry in report.get("degradation") or []:
        remedy = str(entry.get("remedy") or "").strip()
        if not remedy or remedy in seen:
            continue
        seen.add(remedy)
        actions.append(
            {
                "description": str(entry.get("effect") or entry.get("detail") or "bootstrap a missing dependency"),
                "command": remedy,
                "ref": None,
            }
        )
    if not actions:
        actions.append(
            {
                "description": "no bootstrap required",
                "command": None,
                "ref": None,
            }
        )
    return actions


def _parts_for(report: Mapping[str, Any]) -> list[dict[str, Any]]:
    parts = [
        make_part(
            unit="dependency_pins",
            unit_kind="check",
            outcome="success",
            summary="tracked dependency pin files validate against dependency-v1",
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
        env_keys=["HOME", "CI", "VAWS_DEPS_ALLOW_OFF_PIN", "VAWS_REMOTE_DEV_ROOT",
                  "VAWS_COORDINATOR_ROOT", "VAWS_TOP_ROOT", "VAWS_KNOWLEDGE_KIT_ROOT"],
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
            summary=f"dependency pin is invalid: {pin_error}"[:400],
            attempt=attempt,
            environment=environment,
            evidence=make_evidence(),
            next_step=make_next_step(
                actions=[
                    {
                        "description": "fix the tracked pin file named in the failure",
                        "command": None,
                        "ref": None,
                    }
                ]
            ),
            failure=make_failure(
                layer="caller",
                reason_code="bad_arguments",
                message=str(pin_error),
                attribution_basis=["load_pin() rejected a tracked .agents/deps pin against dependency-v1"],
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
                "vaws_deps doctor inspected pinned checkouts and local knowledge cache on this laptop",
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
        next_step=make_next_step(actions=_bootstrap_actions(report)),
        failure=failure,
        parts=parts,
        extensions={"capability_report": report},
    )
    validate_envelope(envelope)
    return envelope


def dumps_doctor(envelope: Mapping[str, Any]) -> str:
    return dumps(dict(envelope))


def checkout_usable(name: str, env: Mapping[str, str] | None = None) -> Path | None:
    return resolve(name, required=False, env=env)
