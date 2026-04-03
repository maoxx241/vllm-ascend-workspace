from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIRST_CLASS_SKILLS = {
    "workspace-init": {
        "intent_groups": (
            ("first-time setup", "prepare this repo for development"),
            ("git setup", "first machine"),
            ("examples include, but are not limited to:",),
        ),
        "description_groups": (
            ("first-time",),
            ("recovery",),
        ),
    },
    "machine-management": {
        "intent_groups": (
            ("attach a machine", "verify whether a machine is ready"),
            ("remove a machine", "machine attach"),
            ("examples include, but are not limited to:",),
        ),
        "description_groups": (
            ("after setup",),
            ("ongoing",),
        ),
    },
    "serving": {
        "intent_groups": (
            ("start service", "status service"),
            ("list services", "stop service"),
            ("examples include, but are not limited to:",),
        ),
    },
    "benchmark": {
        "intent_groups": (
            ("run benchmark", "benchmark execution"),
            ("existing service", "service session"),
            ("examples include, but are not limited to:",),
        ),
    },
    "workspace-reset": {
        "intent_groups": (
            ("destructive teardown", "reset this workspace"),
            ("post-clone state", "explicit destructive teardown"),
            ("examples include, but are not limited to:",),
        ),
    },
}
REQUIRED_PUBLIC_SECTIONS = (
    "## Overview",
    "## When to Use",
    "## Quick Triage",
    "## Default Recipe",
    "## Stop Conditions",
    "## User-Visible Output Contract",
    "## Auth Boundary",
    "## Never Expose",
    "## Cross-Skill Boundary",
    "## Common Mistakes",
    "## Red Flags",
)
FORBIDDEN_DESCRIPTION_TERMS = (
    "tools/vaws.py",
    ".workspace.local",
    "reset --prepare",
    "reset --execute",
    "fleet add",
    "fleet verify",
    "session create",
    "session switch",
    "./sync",
    "./setup",
)
PUBLIC_BODY_FORBIDDEN_TERMS = (
    "tools/vaws.py ",
    "./sync",
    "./setup",
    "workspace-bootstrap",
    "workspace-git-profile",
    "workspace-fleet",
    "workspace-session-switch",
    "workspace-sync",
)
PUBLIC_SECTIONS_WITHOUT_INTERNALS = (
    "## Overview",
    "## When to Use",
    "## Quick Triage",
    "## Default Recipe",
    "## Stop Conditions",
    "## User-Visible Output Contract",
    "## Cross-Skill Boundary",
    "## Common Mistakes",
    "## Red Flags",
)
QUICK_TRIAGE_MARKERS = {
    "workspace-init": (
        "workspace.probe_config_validity",
        "workspace.probe_git_auth",
        "workspace.diagnose_workspace",
    ),
    "machine-management": (
        "machine.describe_server",
        "machine.probe_host_ssh",
        "runtime.probe_container_transport",
        "code_parity",
        "runtime_env",
    ),
    "serving": (
        "serving.list_sessions",
        "machine.probe_host_ssh",
        "runtime.probe_container_transport",
        "code_parity",
        "runtime_env",
    ),
    "benchmark": (
        "benchmark.describe_preset",
        "service_id",
        "serving.list_sessions",
        "code_parity",
        "runtime_env",
    ),
    "workspace-reset": (
        "servers",
        "auth",
        "repos",
        "known_hosts",
        "benchmark artifacts",
    ),
}
DEFAULT_RECIPE_MARKERS = {
    "workspace-init": (
        "workspace.probe_repo_topology",
        "workspace.describe_repo_targets",
        "machine.register_server",
        "runtime.probe_container_transport",
    ),
    "machine-management": (
        "machine.describe_server",
        "machine.probe_host_ssh",
        "runtime.probe_container_transport",
        "machine.bootstrap_host_ssh",
        "runtime.bootstrap_container_transport",
    ),
    "serving": (
        "serving.list_sessions",
        "serving.launch_service",
        "serving.probe_readiness",
        "serving.describe_session",
        "serving.stop_service",
    ),
    "benchmark": (
        "benchmark.describe_preset",
        "serving.list_sessions",
        "benchmark.run_probe",
        "benchmark.describe_run",
    ),
    "workspace-reset": (
        "reset.prepare_request",
        "reset.cleanup_remote_runtime",
        "reset.cleanup_overlay",
        "partial",
    ),
}
AUTH_BOUNDARY_MARKERS = {
    "workspace-init": (
        "github login",
        "server password",
        "optional first-machine",
        "needs_input",
    ),
    "machine-management": (
        "bare-metal",
        "password bootstrap",
        "github login",
        "container password",
    ),
    "serving": (
        "allowed: none",
        "forbidden",
        "auth prompt",
        "machine-management",
    ),
    "benchmark": (
        "allowed: none",
        "forbidden",
        "auth prompt",
        "explicit reusable service session",
    ),
    "workspace-reset": (
        "allowed: none",
        "forbidden",
        "auth prompt",
        "blocked",
    ),
}
CROSS_SKILL_BOUNDARY_MARKERS = {
    "workspace-init": (
        "machine-management",
        "benchmark",
        "workspace-reset",
    ),
    "machine-management": (
        "workspace-init",
        "serving",
        "benchmark",
        "workspace-reset",
    ),
    "serving": (
        "workspace-init",
        "machine-management",
        "benchmark",
        "workspace-reset",
    ),
    "benchmark": (
        "workspace-init",
        "machine-management",
        "serving",
        "workspace-reset",
    ),
    "workspace-reset": (
        "workspace-init",
        "machine-management",
        "benchmark",
    ),
}
STOP_CONDITION_MARKERS = {
    "workspace-init": (
        "missing git identity",
        "missing machine inventory",
        "partial first-machine readiness",
    ),
    "machine-management": (
        "do not silently bootstrap",
        "unexpected auth prompt",
        "invalid credentials",
    ),
    "serving": (
        "fingerprint mismatch",
        "readiness timeout",
        "machine-not-ready",
    ),
    "benchmark": (
        "service_id",
        "non-reusable",
        "stale code parity",
    ),
    "workspace-reset": (
        "missing authorization",
        "unreachable cleanup",
        "partial",
    ),
}
MINIMAL_READ_PATH_MARKERS = {
    "workspace-init": (
        "do not start at `.agents/discovery/readme.md`",
        "references/internal-routing.md",
        "do not read `tools/lib/*.py` until a named atomic tool fails",
    ),
    "machine-management": (
        "do not start at `.agents/discovery/readme.md`",
        "references/internal-routing.md",
        "runtime-bootstrap-triage.md",
    ),
}


def _skill_text(skill_name: str) -> str:
    return (ROOT / ".agents" / "skills" / skill_name / "SKILL.md").read_text(
        encoding="utf-8"
    )


def _description_line(skill_name: str) -> str:
    for line in _skill_text(skill_name).splitlines():
        if line.startswith("description:"):
            return line.lower()
    raise AssertionError(f"missing description for {skill_name}")


def _section_body(skill_name: str, header: str) -> str:
    text = _skill_text(skill_name)
    marker = f"\n{header}\n"
    start = text.find(marker)
    assert start != -1, f"{skill_name} missing section {header}"
    start += len(marker)
    tail = text[start:]
    next_header = tail.find("\n## ")
    if next_header == -1:
        return tail.strip()
    return tail[:next_header].strip()


def test_first_class_workspace_skills_share_judgment_sections():
    for skill_name in FIRST_CLASS_SKILLS:
        text = _skill_text(skill_name)
        for section in REQUIRED_PUBLIC_SECTIONS:
            assert section in text, f"{skill_name} missing section: {section}"


def test_first_class_workspace_skill_descriptions_are_trigger_only():
    for skill_name in FIRST_CLASS_SKILLS:
        description = _description_line(skill_name)
        assert "description: use when" in description
        for forbidden in FORBIDDEN_DESCRIPTION_TERMS:
            assert forbidden not in description, (
                f"{skill_name} leaked {forbidden} into description"
            )


def test_workspace_init_and_machine_management_descriptions_encode_trigger_split():
    for skill_name in ("workspace-init", "machine-management"):
        description = _description_line(skill_name)
        for group in FIRST_CLASS_SKILLS[skill_name]["description_groups"]:
            assert any(marker.lower() in description for marker in group), (
                f"{skill_name} missing description signal group: {group}"
            )


def test_machine_management_skill_mentions_verify_first_machine_vocabulary():
    text = _skill_text("machine-management").lower()
    assert "machine add" in text
    assert "fleet add" not in text
    assert "fleet verify" not in text
    assert "machine ready does not imply service ready" in text
    assert "do not silently bootstrap" in text


def test_benchmark_skill_rejects_temporary_service_public_vocab():
    text = _skill_text("benchmark").lower()
    assert "temporary service" not in text
    assert "weights_path" not in text
    assert "service_id" in text


def test_when_to_use_sections_are_intent_led_and_non_exhaustive():
    for skill_name, expectations in FIRST_CLASS_SKILLS.items():
        when_to_use = _section_body(skill_name, "## When to Use").lower()
        for group in expectations["intent_groups"]:
            assert any(marker.lower() in when_to_use for marker in group), (
                f"{skill_name} missing intent signal group: {group}"
            )


def test_public_contract_sections_do_not_embed_internal_command_syntax_or_routing_refs():
    for skill_name in FIRST_CLASS_SKILLS:
        for header in PUBLIC_SECTIONS_WITHOUT_INTERNALS:
            body = _section_body(skill_name, header).lower()
            for forbidden in PUBLIC_BODY_FORBIDDEN_TERMS:
                assert forbidden not in body, (
                    f"{skill_name} leaked {forbidden} into {header}"
                )


def test_never_expose_sections_contain_concrete_hidden_items():
    for skill_name in FIRST_CLASS_SKILLS:
        body = _section_body(skill_name, "## Never Expose").lower()
        assert "-" in body
        assert (
            "raw" in body
            or "internal" in body
            or "overlay" in body
            or "secret" in body
        )


def test_auth_boundary_sections_encode_allowed_and_forbidden_prompts():
    for skill_name, markers in AUTH_BOUNDARY_MARKERS.items():
        body = _section_body(skill_name, "## Auth Boundary").lower()
        for marker in markers:
            assert marker in body, f"{skill_name} missing auth marker: {marker}"


def test_quick_triage_sections_name_prerequisite_leaves():
    for skill_name, markers in QUICK_TRIAGE_MARKERS.items():
        body = _section_body(skill_name, "## Quick Triage").lower()
        for marker in markers:
            assert marker in body, (
                f"{skill_name} missing quick triage marker: {marker}"
            )


def test_default_recipe_sections_name_actionable_ladders():
    for skill_name, markers in DEFAULT_RECIPE_MARKERS.items():
        body = _section_body(skill_name, "## Default Recipe").lower()
        for marker in markers:
            assert marker in body, (
                f"{skill_name} missing default recipe marker: {marker}"
            )


def test_cross_skill_boundary_and_stop_conditions_encode_safe_handoffs():
    for skill_name, markers in CROSS_SKILL_BOUNDARY_MARKERS.items():
        boundary = _section_body(skill_name, "## Cross-Skill Boundary").lower()
        for marker in markers:
            assert marker in boundary, (
                f"{skill_name} missing cross-skill boundary marker: {marker}"
            )

    for skill_name, markers in STOP_CONDITION_MARKERS.items():
        stops = _section_body(skill_name, "## Stop Conditions").lower()
        for marker in markers:
            assert marker in stops, (
                f"{skill_name} missing stop-condition marker: {marker}"
            )


def test_public_skill_bodies_define_minimal_read_paths_and_discovery_fallback():
    for skill_name, markers in MINIMAL_READ_PATH_MARKERS.items():
        text = _skill_text(skill_name).lower()
        for marker in markers:
            assert marker.lower() in text, f"{skill_name} missing marker: {marker}"
