import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.code_parity import build_materialization_script, verify_code_parity
from tools.lib.config import RepoPaths
from tools.lib.repo_targets import resolve_repo_targets


def test_verify_code_parity_detects_stale_remote_commits(vaws_repo, monkeypatch):
    subprocess.run(["git", "tag", "0.18.0"], cwd=vaws_repo / "vllm", check=True)
    desired = resolve_repo_targets(
        RepoPaths(root=vaws_repo),
        vllm_upstream_tag="0.18.0",
        vllm_ascend_upstream_branch="main",
    )

    monkeypatch.setattr(
        "tools.lib.code_parity._collect_remote_git_state",
        lambda *_args, **_kwargs: {
            "workspace": {"commit": "0" * 40},
            "vllm": {"commit": desired.vllm.commit},
            "vllm-ascend": {"commit": desired.vllm_ascend.commit},
        },
    )

    result = verify_code_parity(RepoPaths(root=vaws_repo), "lab-a", desired)

    assert result.status == "needs_repair"
    assert result.mismatches == ["workspace"]


def test_build_materialization_script_uses_git_only(vaws_repo):
    subprocess.run(["git", "tag", "0.18.0"], cwd=vaws_repo / "vllm", check=True)
    desired = resolve_repo_targets(
        RepoPaths(root=vaws_repo),
        vllm_upstream_tag="0.18.0",
        vllm_ascend_upstream_branch="main",
    )

    script = build_materialization_script(desired, workspace_root="/vllm-workspace")

    assert "git clone" in script
    assert "submodule sync --recursive" in script
    assert "submodule update --init --recursive" in script
    assert "tar -cf -" not in script
    assert "scp " not in script
