from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8").lower()


def test_first_class_adapter_files_exist():
    assert (ROOT / "AGENTS.md").exists()
    assert (ROOT / "CLAUDE.md").exists()
    assert (ROOT / ".cursorrules").exists()


def test_adapters_route_to_all_first_class_workspace_skills():
    expected_routes = (
        ".agents/skills/workspace-init/skill.md",
        ".agents/skills/workspace-foundation/skill.md",
        ".agents/skills/workspace-git-profile/skill.md",
        ".agents/skills/workspace-fleet/skill.md",
        ".agents/skills/workspace-reset/skill.md",
        ".agents/skills/workspace-session-switch/skill.md",
        ".agents/skills/workspace-sync/skill.md",
    )
    for path in ("AGENTS.md", "CLAUDE.md", ".cursorrules"):
        text = _text(path)
        for route in expected_routes:
            assert route in text


def test_adapters_do_not_link_to_internal_routing_files_or_procedure_tokens():
    forbidden_terms = (
        "internal-routing.md",
        "workspace-bootstrap",
        "bootstrap",
        "init --bootstrap",
        "fleet add",
        "fleet verify",
        "reset --prepare",
        "reset --execute",
        "session create",
        "session switch",
    )
    for path in ("AGENTS.md", "CLAUDE.md", ".cursorrules"):
        text = _text(path)
        for term in forbidden_terms:
            assert term not in text
