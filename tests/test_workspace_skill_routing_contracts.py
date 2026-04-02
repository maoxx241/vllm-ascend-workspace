from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIRST_CLASS_SKILLS = (
    "workspace-init",
    "machine-management",
    "benchmark",
    "workspace-reset",
)
REQUIRED_ROUTING_HEADERS = (
    "## Sanctioned Adapter Surface",
    "## Internal Delegation",
    "## Action Routing",
    "## Internal State Touched",
    "## Related Tests",
)


def _routing_text(skill_name: str) -> str:
    return (
        ROOT / ".agents" / "skills" / skill_name / "references" / "internal-routing.md"
    ).read_text(encoding="utf-8")


def _routing_section(skill_name: str, header: str) -> str:
    text = _routing_text(skill_name)
    marker = f"\n{header}\n"
    start = text.find(marker)
    assert start != -1, f"{skill_name} missing section {header}"
    start += len(marker)
    tail = text[start:]
    next_header = tail.find("\n## ")
    if next_header == -1:
        return tail.strip()
    return tail[:next_header].strip()


def test_internal_routing_files_declare_sanctioned_adapter_surface():
    for skill_name in FIRST_CLASS_SKILLS:
        text = _routing_text(skill_name)
        for header in REQUIRED_ROUTING_HEADERS:
            assert header in text, f"{skill_name} missing routing section {header}"
        lowered = text.lower()
        assert "tools/vaws.py" in lowered
        assert "ordinary execution surface" in lowered
        assert "do not route a normal agent directly to `tools/lib/*.py`" in lowered


def test_action_routing_stays_on_sanctioned_adapters():
    for skill_name in FIRST_CLASS_SKILLS:
        body = _routing_section(skill_name, "## Action Routing").lower()
        assert "sanctioned adapter" in body
        assert "tools/vaws.py" in body
        assert "tools/lib/" not in body
        assert "backend entrypoints" not in body


def test_internal_routing_files_use_canonical_cli_vocabulary_and_no_retired_modules():
    forbidden_terms = (
        "fleet add",
        "fleet verify",
        "fleet list",
        "reset --prepare",
        "reset --execute",
        "foundation",
        "git-profile",
        "tools/lib/foundation.py",
        "tools/lib/git_profile.py",
        "tools/lib/fleet.py",
        "tests/test_vaws_fleet.py",
        "tests/test_vaws_init_bootstrap.py",
    )
    for skill_name in FIRST_CLASS_SKILLS:
        text = _routing_text(skill_name).lower()
        for forbidden in forbidden_terms:
            assert forbidden not in text, (
                f"{skill_name} leaked retired routing term: {forbidden}"
            )


def test_routing_files_reference_canonical_adapters():
    expectations = {
        "workspace-init": ("tools/vaws.py init", "tools/vaws.py doctor"),
        "machine-management": ("tools/vaws.py machine add", "tools/vaws.py machine verify"),
        "benchmark": ("tools/vaws.py benchmark run", "tools/vaws.py internal acceptance run"),
        "workspace-reset": ("tools/vaws.py reset prepare", "tools/vaws.py reset execute"),
    }
    for skill_name, markers in expectations.items():
        text = _routing_text(skill_name).lower()
        for marker in markers:
            assert marker in text, f"{skill_name} missing routing marker: {marker}"
