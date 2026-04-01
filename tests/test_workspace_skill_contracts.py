from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIRST_CLASS_SKILLS = {
    "workspace-bootstrap": {
        "intent_groups": (
            ("first usable workspace baseline", "initialize this workspace for the first time"),
            ("first development server", "development server attached"),
            ("examples include, but are not limited to:",),
        ),
    },
    "workspace-fleet": {
        "intent_groups": (
            ("additional managed server", "additional server"),
            ("post-bootstrap", "after baseline"),
            ("examples include, but are not limited to:",),
        ),
    },
    "workspace-reset": {
        "intent_groups": (
            ("reset or deinitialized", "reset this workspace"),
            ("near post-clone state", "post-clone state"),
            ("examples include, but are not limited to:",),
        ),
    },
    "workspace-session-switch": {
        "intent_groups": (
            ("create a new feature session", "new feature session"),
            ("switch the active session", "active session"),
            ("examples include, but are not limited to:",),
        ),
    },
    "workspace-sync": {
        "intent_groups": (
            ("sync repository or session state", "repository or session state"),
            ("check current sync status", "current sync status"),
            ("examples include, but are not limited to:",),
        ),
    },
}
REQUIRED_SECTIONS = (
    "## Overview",
    "## When to Use",
    "## User-Visible Output Contract",
    "## Never Expose",
    "## Default Inference Rules",
    "## Cross-Skill Boundary",
    "## Failure Handling Notes",
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
)
PUBLIC_BODY_FORBIDDEN_TERMS = (
    "tools/vaws.py ",
    "./sync",
    "./setup",
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
        for section in REQUIRED_SECTIONS:
            assert section in text, f"{skill_name} missing section: {section}"


def test_first_class_workspace_skill_descriptions_are_trigger_only():
    for skill_name in FIRST_CLASS_SKILLS:
        description = _description_line(skill_name)
        assert "description: use when" in description
        for forbidden in FORBIDDEN_DESCRIPTION_TERMS:
            assert forbidden not in description, f"{skill_name} leaked {forbidden} into description"


def test_when_to_use_sections_are_intent_led_and_non_exhaustive():
    for skill_name, expectations in FIRST_CLASS_SKILLS.items():
        when_to_use = _section_body(skill_name, "## When to Use").lower()
        for group in expectations["intent_groups"]:
            assert any(marker.lower() in when_to_use for marker in group), (
                f"{skill_name} missing intent signal group: {group}"
            )


def test_public_contract_sections_do_not_embed_internal_command_syntax():
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
                assert forbidden not in body, f"{skill_name} leaked {forbidden} into {header}"


def test_never_expose_sections_contain_concrete_hidden_items():
    for skill_name in FIRST_CLASS_SKILLS:
        body = _section_body(skill_name, "## Never Expose").lower()
        assert "-" in body
        assert "raw" in body or "internal" in body or "overlay" in body or "secret" in body
