import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.branch_context import require_feature_branch
from tools.lib.config import RepoPaths


def test_require_feature_branch_rejects_protected_branch(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "workspace": {
                    "protected_branches": ["main", "release"],
                }
            }
        ),
        encoding="utf-8",
    )

    try:
        require_feature_branch(RepoPaths(root=vaws_repo))
    except RuntimeError as exc:
        assert "protected branch" in str(exc)
    else:
        raise AssertionError("expected protected-branch rejection")


def test_require_feature_branch_accepts_feature_branch(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    (overlay / "repos.yaml").write_text(
        yaml.safe_dump(
            {
                "workspace": {
                    "protected_branches": ["main", "release"],
                }
            }
        ),
        encoding="utf-8",
    )
    subprocess.run(
        ["git", "checkout", "-b", "feature/demo"],
        cwd=vaws_repo,
        text=True,
        capture_output=True,
        check=True,
    )

    require_feature_branch(RepoPaths(root=vaws_repo))
