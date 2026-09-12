"""Local first-use notice and one upstream preparation before a new CLI copy.

Only prepare_session does network I/O. Hooks and existing editing directories
never call it; ordinary task calls have no update work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import time


def copy_workspace_identity(source: Path, target: Path) -> None:
    """Copy a non-secret setup snapshot; never copy task identity or replace it."""
    from vaws_github import load_github_identity
    identity = load_github_identity(source)
    if identity:
        destination = target / ".vaws-local/github.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {key: identity[key] for key in ("schema", "login", "github_user_id", "forks") if key in identity}
        try:
            with destination.open("x", encoding="utf-8") as stream:
                json.dump(snapshot, stream, ensure_ascii=False)
                stream.write("\n")
        except FileExistsError:
            pass


def workspace_entry(root: Path, *, announce: bool = True) -> dict:
    root = root.resolve()
    state = root / ".vaws-local/updates"
    try:
        state.mkdir(parents=True, exist_ok=True)
        identity_path = root / ".vaws-local/github.json"
        if not identity_path.is_file():
            if not announce or os.environ.get("VAWS_RELEASE_LAUNCH") == "1":
                # Successful GUI hook stderr may be invisible. Do not consume
                # the first visible prompt merely because a hook fired.
                return {"state": "identity_pending"}
            notice = state / "onboarding-notice.json"
            try:
                with notice.open("x", encoding="utf-8") as stream:
                    json.dump({"offered_at": time.time()}, stream)
            except FileExistsError:
                return {"state": "identity_pending"}
            return {"state": "needs_github_user", "message":
                    "First use: provide your personal GitHub username to configure personal forks and upstream updates. "
                    "The Agent can run workspace_forks.py; a repo-init skill is not required. "
                    "Local work remains available."}
        identity = json.loads(identity_path.read_text(encoding="utf-8"))
        if (not isinstance(identity, dict) or identity.get("schema") != "vaws.github.v1"
                or not isinstance(identity.get("login"), str) or not identity["login"].strip()):
            return {"state": "identity_invalid", "message": "Saved GitHub identity has no login; rerun workspace_forks.py."}
        config_path = state / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.is_file() else {}
        if not isinstance(config, dict):
            return {"state": "configuration_invalid", "message": "Update configuration must be a JSON object."}
        if config.get("enabled") is False:
            return {"state": "disabled"}
        return {"state": "configured"}
    except Exception as exc:
        # This optional entry cannot prevent the user's native session. Retain
        # concrete diagnostics instead of converting an update failure into a
        # task/knowledge prerequisite.
        return {"state": "unavailable", "error": str(exc)}


def prepare_session(root: Path) -> dict:
    """One synchronous preparation, before a new editing directory is created.

    Uses the updater's ordinary Git lock. Failure keeps the available local
    version usable and leaves detailed updater evidence under .vaws-local.
    """
    result = workspace_entry(root)
    if result["state"] != "configured":
        return result
    try:
        from vaws_local_owner import windows_mounted_workspace, accessible_windows_path, managed_path
        if windows_mounted_workspace(root):
            # Shared NTFS Git state has one native Windows lock/process owner.
            from vaws_environment import windows_ready
            receipt = windows_ready(root)
            command = [accessible_windows_path(receipt["python"]), "-c",
                       "import json,sys;from pathlib import Path;root=Path(sys.argv[1]);"
                       "sys.path.insert(0,str(root/'.agents/lib'));"
                       "from vaws_workspace_entry import prepare_session;"
                       "print(json.dumps(prepare_session(root),ensure_ascii=False))",
                       managed_path(root, windows=True)]
            environment = dict(os.environ)
            for name in ("VAWS_ENV_RECEIPT", "VAWS_MANAGED_ENV_RECEIPT", "VAWS_CONTEXT_FILE",
                         "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                         "VAWS_RELEASE_LAUNCH", "VAWS_VENV_REEXEC", "VAWS_SKIP_VENV_REEXEC", "VIRTUAL_ENV",
                         "PYTHONHOME", "PYTHONPATH"):
                environment.pop(name, None)
            environment["WSLENV"] = ":".join(item for item in environment.get("WSLENV", "").split(":")
                                                if item.split("/", 1)[0] in environment)
            process = subprocess.run(command, cwd=root, env=environment, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, text=True, encoding="utf-8", check=True)
            return json.loads(process.stdout)
        from vaws_workspace_update import Deferred, WorkspaceUpdater, update_lock
        try:
            with update_lock(root):
                return WorkspaceUpdater(root).step(apply=True, activate=False, for_session=True)
        except Deferred as exc:
            return {"status": exc.status, "reason": exc.reason}
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        return {"status": "deferred", "reason": "session_update_pending", "error": str(exc)}
