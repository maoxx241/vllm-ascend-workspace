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
