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
    assert (repo / "CLAUDE.md").exists()
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
    agents_readme = (repo / ".agents" / "README.md").read_text(
        encoding="utf-8"
    ).lower()
    cursorrules = (repo / ".cursorrules").read_text(encoding="utf-8").lower()

    for text in (readme, agents, agents_readme, cursorrules):
        assert "/vllm-workspace" in text
        assert "tools/vaws.py" in text
        assert "/workspace/vllm_workspace" not in text

    for text in (readme, agents, agents_readme, cursorrules):
        assert "planned" not in text
        assert "process docs" not in text
        assert "process documentation" not in text

    for text in (readme, agents_readme):
        assert "workspace-init" in text
        assert "workspace-foundation" in text
        assert "workspace-git-profile" in text
        assert "workspace-bootstrap" not in text

    assert ".agents/skills/" in readme
    assert ".agents/skills/" in agents_readme
    assert ".agents/skills/" in cursorrules
    assert "vllm/" in readme
    assert "vllm-ascend/" in readme
    assert "submodule" in readme
    assert "vllm/" in agents
    assert "vllm-ascend/" in agents
    assert "fleet" in readme
    assert "fleet" in agents


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


def test_public_docs_explain_upstream_defaults_and_staged_init_routing():
    repo = Path(__file__).resolve().parents[1]
    readme = (repo / "README.md").read_text(encoding="utf-8").lower()
    init_skill = (repo / ".agents" / "skills" / "workspace-init" / "SKILL.md").read_text(
        encoding="utf-8"
    ).lower()
    bootstrap_skill = (
        repo / ".agents" / "skills" / "workspace-bootstrap" / "SKILL.md"
    ).read_text(encoding="utf-8").lower()
    agents_readme = (repo / ".agents" / "README.md").read_text(
        encoding="utf-8"
    ).lower()

    for text in (readme, agents_readme):
        assert "upstream" in text
        assert "origin" in text
        assert ".workspace.local/repos.yaml" in text
        assert "natural language" in text or "conversational" in text
        assert "workspace-init" in text
        assert "workspace-foundation" in text
        assert "workspace-git-profile" in text
        assert "workspace-bootstrap" not in text

    assert "staged" in init_skill
    assert "workspace-foundation" in init_skill
    assert "workspace-git-profile" in init_skill
    assert "compatibility alias" in bootstrap_skill
    assert "workspace-init" in bootstrap_skill

    for forbidden in (
        "reset --prepare",
        "reset --execute",
        "fabricate authorization",
    ):
        assert forbidden not in bootstrap_skill


def test_agents_md_routes_first_class_workspace_skills():
    repo = Path(__file__).resolve().parents[1]
    agents = (repo / "AGENTS.md").read_text(encoding="utf-8").lower()
    for skill_name in (
        "workspace-init",
        "workspace-foundation",
        "workspace-git-profile",
        "workspace-fleet",
        "workspace-reset",
        "workspace-session-switch",
        "workspace-sync",
    ):
        assert skill_name in agents

    assert "workspace-bootstrap" not in agents
    assert "reset --prepare" not in agents
    assert "reset --execute" not in agents
    assert "must not skip prepare" not in agents
    assert "fabricate authorization" not in agents
    assert "init --bootstrap" not in agents
    assert "fleet add" not in agents

def test_first_class_workspace_skills_and_internal_routing_refs_exist():
    repo = Path(__file__).resolve().parents[1]
    for skill_name in (
        "workspace-init",
        "workspace-foundation",
        "workspace-git-profile",
        "workspace-fleet",
        "workspace-reset",
        "workspace-session-switch",
        "workspace-sync",
    ):
        skill_root = repo / ".agents" / "skills" / skill_name
        assert (skill_root / "SKILL.md").exists()
        assert (skill_root / "references" / "internal-routing.md").exists()

    bootstrap_root = repo / ".agents" / "skills" / "workspace-bootstrap"
    assert (bootstrap_root / "SKILL.md").exists()
    assert (bootstrap_root / "references" / "internal-routing.md").exists()


def test_internal_routing_refs_contain_command_mapping_and_related_tests():
    repo = Path(__file__).resolve().parents[1]
    for skill_name in (
        "workspace-init",
        "workspace-foundation",
        "workspace-git-profile",
        "workspace-bootstrap",
        "workspace-fleet",
        "workspace-reset",
        "workspace-session-switch",
        "workspace-sync",
    ):
        text = (
            repo / ".agents" / "skills" / skill_name / "references" / "internal-routing.md"
        ).read_text(encoding="utf-8").lower()
        assert "## command mapping" in text
        assert "## related tests" in text
        assert "tools/vaws.py" in text or "./sync" in text or "./setup" in text


def test_first_class_workspace_skills_share_contract_shape():
    repo = Path(__file__).resolve().parents[1]
    required_headers = (
        "## overview",
        "## when to use",
        "## user-visible output contract",
        "## never expose",
        "## default inference rules",
        "## cross-skill boundary",
        "## failure handling notes",
        "## security notes",
        "## common mistakes",
        "## red flags",
    )
    for skill_name in (
        "workspace-init",
        "workspace-foundation",
        "workspace-git-profile",
        "workspace-fleet",
        "workspace-reset",
        "workspace-session-switch",
        "workspace-sync",
    ):
        text = (
            repo / ".agents" / "skills" / skill_name / "SKILL.md"
        ).read_text(encoding="utf-8").lower()
        assert "description: use when" in text
        assert "examples include, but are not limited to:" in text
        assert "internal-routing.md" in text
        for header in required_headers:
            assert header in text


def test_public_workspace_skill_contracts_do_not_require_exact_cli_syntax():
    repo = Path(__file__).resolve().parents[1]
    for skill_name in (
        "workspace-init",
        "workspace-foundation",
        "workspace-git-profile",
        "workspace-fleet",
        "workspace-reset",
        "workspace-session-switch",
        "workspace-sync",
    ):
        text = (
            repo / ".agents" / "skills" / skill_name / "SKILL.md"
        ).read_text(encoding="utf-8").lower()
        assert "tools/vaws.py " not in text
        assert "./sync" not in text
        assert "./setup" not in text
