import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools.lib.repo_targets import resolve_repo_targets


def test_resolve_repo_targets_respects_explicit_upstream_pins(vaws_repo):
    subprocess.run(["git", "tag", "0.18.0"], cwd=vaws_repo / "vllm", check=True)

    targets = resolve_repo_targets(
        RepoPaths(root=vaws_repo),
        vllm_upstream_tag="0.18.0",
        vllm_ascend_upstream_branch="main",
    )

    assert targets.workspace.repo_name == "workspace"
    assert targets.vllm.remote_name == "upstream"
    assert targets.vllm.ref_name == "refs/tags/0.18.0"
    assert len(targets.vllm.commit) == 40
    assert targets.vllm_ascend.remote_name == "upstream"
    assert targets.vllm_ascend.ref_name == "refs/remotes/upstream/main"


def test_resolve_repo_targets_rejects_mutable_main_branch(vaws_repo):
    subprocess.run(["git", "checkout", "main"], cwd=vaws_repo, check=True)

    with pytest.raises(RuntimeError, match="feature branch"):
        resolve_repo_targets(
            RepoPaths(root=vaws_repo),
            vllm_upstream_tag="0.18.0",
            vllm_ascend_upstream_branch="main",
            require_feature_branch=True,
        )
