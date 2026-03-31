import subprocess
import sys
from pathlib import Path
from typing import Dict, Optional

import pytest


def _run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )


def _init_git_repo(path: Path, remote_url: str) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _run(["git", "init", "-b", "main"], cwd=path)
    _run(["git", "config", "user.name", "Test User"], cwd=path)
    _run(["git", "config", "user.email", "test@example.com"], cwd=path)
    (path / "README.md").write_text(f"# {path.name}\n", encoding="utf-8")
    _run(["git", "add", "README.md"], cwd=path)
    _run(["git", "commit", "-m", "init"], cwd=path)
    _run(["git", "remote", "add", "origin", remote_url], cwd=path)
    _run(["git", "remote", "add", "upstream", remote_url], cwd=path)
    _run(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], cwd=path)
    _run(["git", "update-ref", "refs/remotes/origin/release", "HEAD"], cwd=path)
    _run(["git", "update-ref", "refs/remotes/upstream/main", "HEAD"], cwd=path)
    _run(["git", "update-ref", "refs/remotes/upstream/release", "HEAD"], cwd=path)


@pytest.fixture
def vaws_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".gitmodules").write_text(
        "\n".join(
            [
                '[submodule "vllm"]',
                "\tpath = vllm",
                "\turl = https://github.com/vllm-project/vllm.git",
                "\tbranch = main",
                '[submodule "vllm-ascend"]',
                "\tpath = vllm-ascend",
                "\turl = https://github.com/vllm-project/vllm-ascend.git",
                "\tbranch = main",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _init_git_repo(repo / "vllm", "https://github.com/vllm-project/vllm.git")
    _init_git_repo(
        repo / "vllm-ascend",
        "https://github.com/vllm-project/vllm-ascend.git",
    )
    (repo / "vllm-ascend" / ".gitmodules").write_text(
        "\n".join(
            [
                '[submodule "csrc/third_party/catlass"]',
                "\tpath = csrc/third_party/catlass",
                "\turl = https://github.com/example/catlass.git",
                "\tbranch = main",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _init_git_repo(
        repo / "vllm-ascend" / "csrc" / "third_party" / "catlass",
        "https://github.com/example/catlass.git",
    )
    return repo


def run_vaws(
    repo: Path,
    *args: str,
    env: Optional[Dict[str, str]] = None,
) -> subprocess.CompletedProcess[str]:
    script = Path(__file__).resolve().parents[1] / "tools" / "vaws.py"
    return subprocess.run(
        [sys.executable, str(script), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
