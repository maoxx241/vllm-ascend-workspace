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
            ("ready environment", "service session"),
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
REQUIRED_SECTIONS = (
    "## Overview",
    "## When to Use",
    "## User-Visible Output Contract",
    "## Auth Boundary",
    "## Never Expose",
    "## Required Capabilities",
    "## Default Inference Rules",
    "## Cross-Skill Boundary",
    "## Failure Handling Notes",
    "## Failure Routing",
    "## Security Notes",
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
    "workspace-foundation",
    "workspace-git-profile",
    "workspace-fleet",
    "workspace-session-switch",
    "workspace-sync",
)
LEGACY_DISCOVERABLE_SKILLS = (
    "workspace-foundation",
    "workspace-git-profile",
    "workspace-fleet",
    "workspace-session-switch",
    "workspace-sync",
    "workspace-bootstrap",
)


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


def test_first_class_workspace_skills_share_canonical_sections():
    for skill_name in FIRST_CLASS_SKILLS:
        text = _skill_text(skill_name)
        assert "internal-routing.md" in text
        for section in REQUIRED_SECTIONS:
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


def test_machine_management_skill_mentions_machine_vocabulary():
    text = _skill_text("machine-management").lower()
    assert "machine add" in text
    assert "fleet add" not in text
    assert "fleet verify" not in text
    assert "machine ready does not imply service ready" in text


def test_when_to_use_sections_are_intent_led_and_non_exhaustive():
    for skill_name, expectations in FIRST_CLASS_SKILLS.items():
        when_to_use = _section_body(skill_name, "## When to Use").lower()
        for group in expectations["intent_groups"]:
            assert any(marker.lower() in when_to_use for marker in group), (
                f"{skill_name} missing intent signal group: {group}"
            )


def test_public_contract_sections_do_not_embed_internal_command_syntax_or_legacy_skills():
    public_sections = (
        "## Overview",
        "## When to Use",
        "## User-Visible Output Contract",
        "## Default Inference Rules",
        "## Cross-Skill Boundary",
        "## Failure Handling Notes",
        "## Security Notes",
        "## Common Mistakes",
        "## Red Flags",
    )
    for skill_name in FIRST_CLASS_SKILLS:
        for header in public_sections:
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
    expected_markers = {
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
            "temporary or explicit service session",
        ),
        "workspace-reset": (
            "allowed: none",
            "forbidden",
            "auth prompt",
            "blocked",
        ),
    }
    for skill_name, markers in expected_markers.items():
        body = _section_body(skill_name, "## Auth Boundary").lower()
        for marker in markers:
            assert marker in body, f"{skill_name} missing auth marker: {marker}"


def test_required_capabilities_sections_name_canonical_capability_leaves():
    expected_markers = {
        "workspace-init": (
            "git_auth",
            "repo_topology",
            "host_access",
            "container_access",
        ),
        "machine-management": (
            "host_access",
            "container_access",
            "code_parity",
            "runtime_env",
        ),
        "serving": (
            "host_access",
            "container_access",
            "code_parity",
            "runtime_env",
        ),
        "benchmark": (
            "git_auth",
            "repo_topology",
            "host_access",
            "container_access",
            "code_parity",
            "runtime_env",
            "service",
        ),
        "workspace-reset": (
            "known_hosts",
            "servers",
            "sessions",
            "targets",
        ),
    }
    for skill_name, markers in expected_markers.items():
        body = _section_body(skill_name, "## Required Capabilities").lower()
        for marker in markers:
            assert marker in body, (
                f"{skill_name} missing required capability marker: {marker}"
            )


def test_failure_routing_sections_redirect_to_canonical_skills():
    expected_markers = {
        "workspace-init": (
            "git_auth",
            "repo_topology",
            "machine-management",
        ),
        "machine-management": (
            "workspace-init",
            "host_access",
            "container_access",
        ),
        "serving": (
            "machine-management",
            "code_parity",
            "runtime_env",
        ),
        "benchmark": (
            "workspace-init",
            "machine-management",
            "serving",
            "runtime_env",
            "code_parity",
        ),
        "workspace-reset": (
            "workspace-reset",
            "partial",
            "machine-management",
        ),
    }
    for skill_name, markers in expected_markers.items():
        body = _section_body(skill_name, "## Failure Routing").lower()
        for marker in markers:
            assert marker in body, (
                f"{skill_name} missing failure routing marker: {marker}"
            )


def test_legacy_discoverable_skill_roots_are_absent():
    for skill_name in LEGACY_DISCOVERABLE_SKILLS:
        assert not (ROOT / ".agents" / "skills" / skill_name).exists()
