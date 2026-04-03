from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY_FILES = (
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    "README.md",
    ".agents/README.md",
)
PUBLIC_SKILLS = (
    "workspace-init",
    "machine-management",
    "serving",
    "benchmark",
    "workspace-reset",
)
DISCOVERY_ROUTE = ".agents/discovery/README.md"
FORBIDDEN_TERMS = (
    "tools/vaws.py",
    "./setup",
    "./sync",
    ".workspace.local/",
    ".workspace.local/repos.yaml",
    "internal-routing.md",
    "workspace-foundation",
    "workspace-git-profile",
    "workspace-fleet",
    "workspace-session-switch",
    "workspace-sync",
    "workspace-bootstrap",
    "--help",
)
SKILL_FIRST_MARKERS = (
    "read the matched public `skill.md` first",
    "only when the matched skill or its linked reference does not identify the next tool",
)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8").lower()


def test_first_contact_surface_does_not_require_cli_or_overlay_internals():
    for path in ENTRY_FILES:
        text = _text(path)
        for forbidden in FORBIDDEN_TERMS:
            assert forbidden not in text, f"{path} leaked {forbidden}"


def test_entry_surface_routes_a_fresh_agent_to_skill_first_then_discovery():
    for path in ENTRY_FILES:
        text = _text(path)
        assert DISCOVERY_ROUTE.lower() in text
        for skill_name in PUBLIC_SKILLS:
            assert skill_name in text, f"{path} missing {skill_name}"
        for marker in SKILL_FIRST_MARKERS:
            assert marker in text, f"{path} missing skill-first marker: {marker}"
