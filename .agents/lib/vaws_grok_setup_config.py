"""Plan Grok's project Git creation callback; never change global preferences."""
from __future__ import annotations

from pathlib import Path
import shlex
import subprocess


MARKER = "# VAWS native Grok worktree setup v1"


def plan_grok_setup(files: dict[Path, str], notes: list, project: Path, root: Path) -> list[Path]:
    """Return executable files for the ordinary setup plan to install.

    A user-owned Git hook or shared hooksPath stays with its existing owner.
    Linked targets already share the callback installed from the source; their
    own client wiring must not retarget that shared hook to an ephemeral task.
    """
    def git(*args):
        return subprocess.run(["git", "-C", str(project), *args], capture_output=True,
                              text=True, timeout=5, check=False)

    common = git("rev-parse", "--path-format=absolute", "--git-common-dir")
    own = git("rev-parse", "--absolute-git-dir")
    if common.returncode or own.returncode:
        notes.append({"action": "preserved", "reason": "grok-worktree-setup-needs-git-repository"})
        return []
    common_path = Path(common.stdout.strip()).resolve()
    if common_path != Path(own.stdout.strip()).resolve():
        return []
    configured = git("config", "--get", "core.hooksPath")
    if configured.returncode not in (0, 1):
        raise RuntimeError(configured.stderr.strip() or "cannot inspect Git hooksPath")
    if configured.stdout.strip():
        notes.append({"action": "preserved", "reason": "grok-existing-git-hooks-path",
                      "detail": "Integrate the VAWS new-worktree callback with the existing Git hook owner."})
        return []
    path = common_path / "hooks/post-checkout"
    content = files.get(path)
    if path.is_symlink() or (path.exists() and not path.is_file()):
        notes.append({"path": str(path), "action": "preserved", "reason": "grok-existing-git-hook"})
        return []
    if content is None and path.exists():
        content = path.read_text(encoding="utf-8")
    if content is not None and MARKER not in content.splitlines():
        notes.append({"path": str(path), "action": "preserved", "reason": "grok-existing-git-hook"})
        return []
    command = "uv run --no-project python " + shlex.quote(str(root / ".agents/scripts/vaws_grok_worktree.py"))
    command += " --source " + shlex.quote(str(project.resolve()))
    files[path] = ("#!/bin/sh\n" + MARKER + '\ncase "$1" in\n'
                   + "  " + "0" * 40 + "|" + "0" * 64 + ") ;;\n  *) exit 0 ;;\nesac\n"
                   + "exec " + command + ' "$@"\n')
    notes.append({"action": "initialization-choice", "reason": "grok-native-worktree-preferences",
                  "detail": 'Grok user preferences need cli.worktree_type="git" and hints.new_session_worktree_mode/hints.fork_worktree_mode="always". These affect every Grok project; setup does not change them. Git mode starts from the selected commit and does not copy uncommitted changes.'})
    return [path]
