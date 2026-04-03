from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ADAPTER_FILES = ("AGENTS.md", "CLAUDE.md", ".cursorrules")
PUBLIC_SKILL_ROUTES = (
    ".agents/skills/workspace-init/skill.md",
    ".agents/skills/machine-management/skill.md",
    ".agents/skills/serving/skill.md",
    ".agents/skills/benchmark/skill.md",
    ".agents/skills/workspace-reset/skill.md",
)
DISCOVERY_ROUTE = ".agents/discovery/readme.md"
LEGACY_PUBLIC_VOCAB = (
    "workspace-foundation",
    "workspace-git-profile",
    "workspace-fleet",
    "workspace-session-switch",
    "workspace-sync",
    "workspace-bootstrap",
    "fleet",
    "session",
    "sync",
)
SKILL_FIRST_MARKERS = (
    "read the matched public `skill.md` first",
    "only when the matched skill or its linked reference does not identify the next tool",
)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8").lower()


def test_first_class_adapter_files_exist():
    assert (ROOT / "AGENTS.md").exists()
    assert (ROOT / "CLAUDE.md").exists()
    assert (ROOT / ".cursorrules").exists()


def test_adapters_route_to_public_skills_then_discovery():
    for path in ADAPTER_FILES:
        text = _text(path)
        for route in PUBLIC_SKILL_ROUTES:
            assert route in text, f"{path} missing route {route}"
        assert DISCOVERY_ROUTE in text, f"{path} missing discovery route"
        for marker in SKILL_FIRST_MARKERS:
            assert marker in text, f"{path} missing skill-first marker: {marker}"
        for forbidden in LEGACY_PUBLIC_VOCAB:
            assert forbidden not in text, f"{path} leaked {forbidden}"


def test_adapters_do_not_center_cli_or_internal_procedure_tokens():
    forbidden_terms = (
        "internal-routing.md",
        "tools/vaws.py",
        "./setup",
        "./sync",
        ".workspace.local/",
        ".workspace.local/repos.yaml",
        "init --bootstrap",
        "fleet add",
        "fleet verify",
        "reset --prepare",
        "reset --execute",
        "session create",
        "session switch",
        "acceptance run",
        "benchmark run",
    )
    for path in ADAPTER_FILES:
        text = _text(path)
        for term in forbidden_terms:
            assert term not in text, f"{path} leaked {term}"
