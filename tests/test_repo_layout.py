from pathlib import Path


def test_public_repo_layout_exists():
    repo = Path(__file__).resolve().parents[1]
    assert (repo / ".gitignore").exists()
    assert (repo / ".gitmodules").exists()
    assert (repo / "README.md").exists()
    assert (repo / "config" / "auth.example.yaml").exists()
    assert (repo / "config" / "repos.example.yaml").exists()
    assert (repo / "config" / "targets.example.yaml").exists()
    assert (repo / "AGENTS.md").exists()
    assert (repo / ".cursorrules").exists()
    assert (repo / "pytest.ini").exists()
    assert (repo / "vllm").exists()
    assert (repo / "vllm-ascend").exists()


def test_process_docs_are_not_meant_for_main():
    repo = Path(__file__).resolve().parents[1]
    ignore_text = (repo / ".gitignore").read_text(encoding="utf-8")
    assert ".workspace.local/" in ignore_text
    assert ".DS_Store" in ignore_text
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

    for text in (readme, agents, cursorrules):
        assert "planned" not in text
        assert "process docs" not in text
        assert "process documentation" not in text

    assert ".agents/skills/" in readme
    assert ".agents/skills/" in agents
    assert ".agents/skills/" in cursorrules
    assert "vllm/" in readme
    assert "vllm-ascend/" in readme
    assert "submodule" in readme
    assert "vllm/" in agents
    assert "vllm-ascend/" in agents
    assert "submodule" in agents


def test_workspace_submodules_are_declared():
    repo = Path(__file__).resolve().parents[1]
    gitmodules = (repo / ".gitmodules").read_text(encoding="utf-8")
    assert 'submodule "vllm"' in gitmodules
    assert "path = vllm" in gitmodules
    assert "https://github.com/vllm-project/vllm.git" in gitmodules
    assert 'submodule "vllm-ascend"' in gitmodules
    assert "path = vllm-ascend" in gitmodules
    assert "https://github.com/vllm-project/vllm-ascend.git" in gitmodules
    assert "git@github.com:" not in gitmodules


def test_public_docs_explain_recursive_submodule_bootstrap():
    repo = Path(__file__).resolve().parents[1]
    readme = (repo / "README.md").read_text(encoding="utf-8").lower()
    agents = (repo / "AGENTS.md").read_text(encoding="utf-8").lower()

    for text in (readme, agents):
        assert "recursive" in text
        assert "submodule update --init --recursive" in text


def test_public_repo_topology_example_stays_public_and_upstream_oriented():
    repo = Path(__file__).resolve().parents[1]
    repos_example = (repo / "config" / "repos.example.yaml").read_text(
        encoding="utf-8"
    )
    assert "https://github.com/vllm-project/vllm.git" in repos_example
    assert "https://github.com/vllm-project/vllm-ascend.git" in repos_example
    assert "git@github.com:" not in repos_example


def test_public_docs_explain_upstream_defaults_and_local_origin_bootstrap():
    repo = Path(__file__).resolve().parents[1]
    readme = (repo / "README.md").read_text(encoding="utf-8").lower()
    agents = (repo / "AGENTS.md").read_text(encoding="utf-8").lower()
    agents_readme = (repo / ".agents" / "README.md").read_text(
        encoding="utf-8"
    ).lower()

    for text in (readme, agents, agents_readme):
        assert "upstream" in text
        assert "origin" in text
        assert ".workspace.local/repos.yaml" in text
        assert "natural language" in text or "conversational" in text


def test_workspace_local_skill_skeletons_exist_and_stay_public():
    repo = Path(__file__).resolve().parents[1]
    agents_root = repo / ".agents"
    agents_readme = (agents_root / "README.md").read_text(encoding="utf-8").lower()
    assert "/vllm-workspace" in agents_readme
    assert "tools/vaws.py" in agents_readme
    assert "./setup" in agents_readme
    assert "./sync" in agents_readme
    assert ".workspace.local/" in agents_readme
    assert ".workspace.local/sessions/<session>/manifest.yaml" in agents_readme
    assert "/vllm-workspace/.vaws/sessions/<session>/manifest.yaml" in agents_readme
    assert "/workspace/vllm_workspace" not in agents_readme

    expected_skill_content = {
        "workspace-bootstrap": (
            "/vllm-workspace",
            "natural language",
            "server",
            "vllm-ascend",
            "optional",
            "tools/vaws.py init --bootstrap",
        ),
        "workspace-session-switch": (
            ".workspace.local/state.json",
            ".workspace.local/sessions/<session>/manifest.yaml",
            "/vllm-workspace/.vaws/sessions/<session>/manifest.yaml",
            "tools/vaws.py session create",
            "tools/vaws.py session switch",
        ),
        "workspace-sync": (
            "tools/vaws.py sync",
            "./sync",
            "origin/main",
        ),
        "profiling-analysis": (
            "/vllm-workspace",
            "active feature session",
        ),
    }
    for skill_name, required_snippets in expected_skill_content.items():
        skill_path = agents_root / "skills" / skill_name / "SKILL.md"
        assert skill_path.exists()
        text = skill_path.read_text(encoding="utf-8").lower()
        assert "workspace-local" in text
        assert "global skill installer" not in text
        assert "/workspace/vllm_workspace" not in text
        for snippet in required_snippets:
            assert snippet in text
