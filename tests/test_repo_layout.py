from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRY_DOCS = (
    "README.md",
    "AGENTS.md",
    "CLAUDE.md",
    ".cursorrules",
    ".agents/README.md",
)
PUBLIC_SKILLS = (
    "workspace-init",
    "machine-management",
    "benchmark",
    "workspace-reset",
)
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


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8").lower()


def test_public_repo_layout_exists():
    assert (ROOT / ".gitignore").exists()
    assert (ROOT / ".gitmodules").exists()
    assert (ROOT / "README.md").exists()
    assert (ROOT / "config" / "auth.example.yaml").exists()
    assert (ROOT / "config" / "repos.example.yaml").exists()
    assert (ROOT / "config" / "targets.example.yaml").exists()
    assert (ROOT / "AGENTS.md").exists()
    assert (ROOT / "CLAUDE.md").exists()
    assert (ROOT / ".cursorrules").exists()
    assert (ROOT / ".agents" / "README.md").exists()
    assert (ROOT / "pytest.ini").exists()
    assert (ROOT / "vllm").exists()
    assert (ROOT / "vllm-ascend").exists()


def test_process_docs_are_not_meant_for_main():
    ignore_text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".workspace.local/" in ignore_text
    assert ".DS_Store" in ignore_text
    assert "__pycache__/" in ignore_text
    assert "*.py[cod]" in ignore_text
    assert ".pytest_cache/" in ignore_text
    assert "docs/superpowers/" in ignore_text


def test_first_contact_docs_use_canonical_runtime_root_and_skill_first_surface():
    for path in ENTRY_DOCS:
        text = _text(path)
        assert "/vllm-workspace" in text
        assert "/workspace/vllm_workspace" not in text
        assert "planned" not in text
        assert "process docs" not in text
        assert "process documentation" not in text
        assert "tools/vaws.py" not in text
        assert "./setup" not in text
        assert "./sync" not in text


def test_first_contact_docs_route_only_to_public_skills():
    for path in ENTRY_DOCS:
        text = _text(path)
        for skill_name in PUBLIC_SKILLS:
            assert skill_name in text, f"{path} missing {skill_name}"
        for forbidden in LEGACY_PUBLIC_VOCAB:
            assert forbidden not in text, f"{path} leaked {forbidden}"


def test_first_contact_docs_keep_repo_context_without_legacy_workflow_map():
    readme = _text("README.md")
    agents = _text("AGENTS.md")
    agents_readme = _text(".agents/README.md")

    for text in (readme, agents, agents_readme):
        assert "vllm/" in text
        assert "vllm-ascend/" in text
        assert "recursive" in text
        assert "submodule update --init --recursive" in text

    for text in (readme, agents_readme):
        assert "upstream" in text
        assert "origin" in text
        assert ".workspace.local/repos.yaml" in text
        assert ".agents/skills/" in text


def test_profiling_analysis_remains_discoverable_during_public_surface_migration():
    profiling_skill = ROOT / ".agents" / "skills" / "profiling-analysis" / "SKILL.md"
    assert profiling_skill.exists()


def test_workspace_submodules_are_declared():
    gitmodules = (ROOT / ".gitmodules").read_text(encoding="utf-8")
    assert 'submodule "vllm"' in gitmodules
    assert "path = vllm" in gitmodules
    assert "https://github.com/vllm-project/vllm.git" in gitmodules
    assert 'submodule "vllm-ascend"' in gitmodules
    assert "path = vllm-ascend" in gitmodules
    assert "https://github.com/vllm-project/vllm-ascend.git" in gitmodules
    assert "git@github.com:" not in gitmodules


def test_public_repo_topology_example_stays_public_and_upstream_oriented():
    repos_example = (ROOT / "config" / "repos.example.yaml").read_text(
        encoding="utf-8"
    )
    assert "https://github.com/vllm-project/vllm.git" in repos_example
    assert "https://github.com/vllm-project/vllm-ascend.git" in repos_example
    assert "git@github.com:" not in repos_example
