"""Nonblocking first-use notice and release watcher wiring for native clients.

This entry does no network I/O or installation. Existing GUI session hooks only
start preparation; the CLI launcher can copy an already prepared release into a
new editing directory. Ordinary tasks remain available.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time


def copy_workspace_identity(source: Path, target: Path, *, disable_updates: bool = False) -> None:
    """Copy a non-secret setup snapshot; never copy task identity or replace it."""
    from vaws_github import atomic_json, load_github_identity
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
    if disable_updates:
        # A prepared checkout shares the originating repository's watcher.
        path = target / ".vaws-local/updates/config.json"
        configuration = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(configuration, dict):
            raise ValueError("prepared update configuration must be a JSON object")
        atomic_json(path, {**configuration, "enabled": False})


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
                    "First use: provide your personal GitHub username to configure personal forks and release updates. "
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
        result = {"state": "configured"}
        # The watcher's common-Git OS lock is the authority. This local TTL only
        # avoids spawning duplicate contenders on rapid hook events.
        launch = state / "last-launch.json"
        try:
            previous = json.loads(launch.read_text(encoding="utf-8"))
            age = time.time() - float(previous["at"])
            if 0 <= age < 60:
                return result
        except (OSError, ValueError, TypeError, KeyError):
            pass
        command = [getattr(sys, "_base_executable", sys.executable), str(root / ".agents/scripts/workspace_update.py"),
                   "--root", str(root), "watch"]
        from vaws_local_owner import windows_mounted_workspace, accessible_windows_path, managed_path
        if windows_mounted_workspace(root):
            # NTFS accessed by WSL and native Windows must have one lock owner.
            # Launch native Python; its watcher takes the Windows OS lock.
            from vaws_environment import windows_ready
            receipt = windows_ready(root)
            command = [accessible_windows_path(receipt["python"]),
                       managed_path(root / ".agents/scripts/workspace_update.py", windows=True),
                       "--root", managed_path(root, windows=True), "watch"]
        options = {"start_new_session": True} if os.name != "nt" else {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP}
        environment = dict(os.environ)
        for name in ("VAWS_ENV_RECEIPT", "VAWS_MANAGED_ENV_RECEIPT", "VAWS_CONTEXT_FILE",
                     "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT", "CODEX_THREAD_ID", "CODEX_SESSION_ID",
                     "VAWS_RELEASE_LAUNCH", "VAWS_VENV_REEXEC", "VAWS_SKIP_VENV_REEXEC", "VIRTUAL_ENV",
                     "PYTHONHOME", "PYTHONPATH"):
            environment.pop(name, None)
        if windows_mounted_workspace(root):
            # WSLENV otherwise forwards stale parent pins even when Python's
            # subprocess env has removed their Linux values.
            forwarded = environment.get("WSLENV", "").split(":")
            environment["WSLENV"] = ":".join(item for item in forwarded
                                              if item.split("/", 1)[0] in environment)
        with (state / "watch.log").open("ab") as stream:
            process = subprocess.Popen(command, cwd=root, env=environment,
                                       stdin=subprocess.DEVNULL, stdout=stream, stderr=stream,
                                       close_fds=True, **options)
        launch.write_text(json.dumps({"at": time.time(), "pid": process.pid}), encoding="utf-8")
        result["watcher"] = "requested"
        return result
    except Exception as exc:
        # This optional entry cannot prevent the user's native session. Retain
        # concrete diagnostics instead of converting an update failure into a
        # task/knowledge prerequisite.
        return {"state": "unavailable", "error": str(exc)}


def report_workspace_entry(root: Path, *, announce: bool = True) -> None:
    if os.name == "nt" and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    result = workspace_entry(root, announce=announce)
    if result.get("state") not in {"configured", "identity_pending", "disabled"}:
        print(json.dumps({"workspace_updates": result}, ensure_ascii=False), file=sys.stderr, flush=True)
