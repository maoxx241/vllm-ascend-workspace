from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {
    "workspace-init": {
        "intent_groups": (
            ("prepare this repo for development", "first-time setup"),
            ("git setup", "first machine"),
        ),
        "required_triage_markers": (
            "workspace.probe_config_validity",
            "workspace.probe_git_auth",
        ),
        "required_recipe_markers": (
            "workspace.probe_repo_topology",
            "workspace.describe_repo_targets",
            "machine.register_server",
        ),
        "required_stop_markers": (
            "missing git identity",
            "missing machine inventory",
        ),
        "pressure_only_variants": (
            "把这个仓库准备成可开发状态，Git 还没配，机器也还没有。",
            "先把仓库基础环境准备好，再看要不要接第一台机器。",
        ),
    },
    "machine-management": {
        "intent_groups": (
            ("attach a machine", "verify whether a machine is ready"),
            ("remove a machine", "machine attach"),
        ),
        "required_triage_markers": (
            "machine.describe_server",
            "machine.probe_host_ssh",
        ),
        "required_recipe_markers": (
            "machine.probe_host_ssh",
            "runtime.probe_container_transport",
        ),
        "required_stop_markers": (
            "do not silently bootstrap",
            "unexpected auth prompt",
        ),
        "pressure_only_variants": (
            "这台机器之前 attach 过，现在帮我确认还能不能用；如果只是 verify 失败，不要顺手重新 bootstrap。",
            "先 verify 这台机器，不要把 repair 和 bootstrap 混进来。",
        ),
    },
    "serving": {
        "intent_groups": (
            ("start service", "status service"),
            ("list services", "stop service"),
        ),
        "required_triage_markers": (
            "serving.list_sessions",
            "machine.probe_host_ssh",
            "runtime.probe_container_transport",
        ),
        "required_recipe_markers": (
            "serving.describe_session",
            "serving.launch_service",
            "serving.probe_readiness",
        ),
        "required_stop_markers": (
            "fingerprint mismatch",
            "readiness timeout",
        ),
        "pressure_only_variants": (
            "先把服务起起来，如果已有完全匹配的 session 就复用。",
            "先看看能不能复用现成 service，不行再起新的。",
        ),
    },
    "benchmark": {
        "intent_groups": (
            ("run benchmark", "benchmark execution"),
            ("service session", "existing service"),
        ),
        "required_triage_markers": (
            "service_id",
            "benchmark.describe_preset",
            "serving.list_sessions",
        ),
        "required_recipe_markers": (
            "benchmark.describe_preset",
            "benchmark.run_probe",
            "serving.describe_session",
        ),
        "required_stop_markers": (
            "service_id",
            "non-reusable",
            "stale code parity",
        ),
        "pressure_only_variants": (
            "先看 preset，再确认要复用哪个已有 service 跑 benchmark。",
            "先列出可复用的 service，再决定 benchmark 怎么跑。",
        ),
    },
    "workspace-reset": {
        "intent_groups": (
            ("destructive teardown", "reset this workspace"),
            ("post-clone state", "explicit destructive teardown"),
        ),
        "required_triage_markers": (
            "servers",
            "benchmark artifacts",
            "reset.prepare_request",
        ),
        "required_recipe_markers": (
            "reset.prepare_request",
            "reset.cleanup_remote_runtime",
            "reset.cleanup_overlay",
        ),
        "required_stop_markers": (
            "missing authorization",
            "unreachable cleanup",
        ),
        "pressure_only_variants": (
            "把这个 workspace 清掉，但先告诉我会删什么，而且删不干净要明确说出来。",
            "先给我 destructive preview，再决定要不要真的 reset。",
        ),
    },
}
EXTRA_PRESSURE_MARKERS = {
    "workspace-init": (
        "do not start at `.agents/discovery/readme.md`",
        "do not read `tools/lib/*.py` until a named atomic tool fails",
    ),
    "machine-management": (
        "do not start at `.agents/discovery/readme.md`",
        "runtime-bootstrap-triage.md",
        "do not read `tools/lib/*.py` until a named atomic tool fails",
    ),
}


def _skill_text(skill_name: str) -> str:
    return (ROOT / ".agents" / "skills" / skill_name / "SKILL.md").read_text(
        encoding="utf-8"
    ).lower()


def _section_body(skill_name: str, header: str) -> str:
    text = _skill_text(skill_name)
    marker = f"\n{header.lower()}\n"
    start = text.find(marker)
    assert start != -1, f"{skill_name} missing section {header}"
    start += len(marker)
    tail = text[start:]
    next_header = tail.find("\n## ")
    if next_header == -1:
        return tail.strip()
    return tail[:next_header].strip()


def test_pressure_scenarios_require_usable_guidance_and_no_phrase_copying():
    for skill_name, scenario in SCENARIOS.items():
        text = _skill_text(skill_name)
        triage = _section_body(skill_name, "## Quick Triage")
        recipe = _section_body(skill_name, "## Default Recipe")
        stops = _section_body(skill_name, "## Stop Conditions")

        assert "examples include, but are not limited to:" in text
        for group in scenario["intent_groups"]:
            assert any(marker.lower() in text for marker in group), (
                f"{skill_name} missing intent signal group: {group}"
            )
        for marker in scenario["required_triage_markers"]:
            assert marker.lower() in triage, (
                f"{skill_name} missing triage guidance marker: {marker}"
            )
        for marker in scenario["required_recipe_markers"]:
            assert marker.lower() in recipe, (
                f"{skill_name} missing recipe guidance marker: {marker}"
            )
        for marker in scenario["required_stop_markers"]:
            assert marker.lower() in stops, (
                f"{skill_name} missing stop-condition marker: {marker}"
            )
        for phrase in scenario["pressure_only_variants"]:
            assert phrase.lower() not in text, (
                f"{skill_name} copied pressure-only variant {phrase!r} into the contract body"
            )


def test_pressure_scenarios_guard_against_premature_discovery_and_source_fanout():
    for skill_name, markers in EXTRA_PRESSURE_MARKERS.items():
        text = _skill_text(skill_name)
        for marker in markers:
            assert marker in text, f"{skill_name} missing pressure guard: {marker}"
