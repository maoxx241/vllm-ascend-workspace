from pathlib import Path


def test_public_repo_layout_exists():
    repo = Path(__file__).resolve().parents[1]
    assert (repo / ".gitignore").exists()
    assert (repo / "README.md").exists()
    assert (repo / "config" / "auth.example.yaml").exists()
    assert (repo / "config" / "repos.example.yaml").exists()
    assert (repo / "config" / "targets.example.yaml").exists()
    assert (repo / "AGENTS.md").exists()
    assert (repo / ".cursorrules").exists()
    assert (repo / "pytest.ini").exists()


def test_process_docs_are_not_meant_for_main():
    repo = Path(__file__).resolve().parents[1]
    ignore_text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".workspace.local/" in ignore_text
    assert "__pycache__/" in ignore_text
    assert "*.py[cod]" in ignore_text
    assert ".pytest_cache/" in ignore_text
    assert "docs/superpowers/" in ignore_text


def test_public_guidance_uses_current_entrypoints_and_canonical_root():
    repo = Path(__file__).resolve().parents[1]
    readme = (repo / "README.md").read_text(encoding="utf-8").lower()
    agents = (repo / "AGENTS.md").read_text(encoding="utf-8").lower()
    cursorrules = (repo / ".cursorrules").read_text(encoding="utf-8").lower()

    for text in (readme, agents, cursorrules):
        assert "/vllm-workspace" in text
        assert "tools/vaws.py" in text
        assert "./setup" in text
        assert "./sync" in text
        assert "/workspace/vllm_workspace" not in text

    assert "planned entrypoints" not in readme
    assert "planned implementation entrypoint" not in cursorrules
    assert "planned compatibility entrypoints" not in cursorrules
    assert "legacy canonical path references" in readme
    assert "legacy canonical path references" not in agents
    assert "legacy canonical path references" not in cursorrules


def test_workspace_local_skill_skeletons_exist_and_stay_public():
    repo = Path(__file__).resolve().parents[1]
    agents_root = repo / ".agents"
    assert (agents_root / "README.md").exists()

    skill_names = (
        "workspace-bootstrap",
        "workspace-session-switch",
        "workspace-sync",
        "profiling-analysis",
    )
    for skill_name in skill_names:
        skill_path = agents_root / "skills" / skill_name / "SKILL.md"
        assert skill_path.exists()
        text = skill_path.read_text(encoding="utf-8").lower()
        assert "workspace-local" in text
        assert "guidance" in text
        assert "global skill installer" not in text
        assert "/workspace/vllm_workspace" not in text
