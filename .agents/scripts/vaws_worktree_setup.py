#!/usr/bin/env python3
"""Prepare a worktree supplied by a native client's creation lifecycle.

Codex and Cursor run this before the new Agent starts. They own the directory
and native session; the normal session hook then attaches it to VAWS. This is
not a command an Agent needs to call, and never runs from SessionStart/resume.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))

from vaws_environment import EnvironmentError, PIN_ENV, MANAGED_PIN_ENV, native_ready, saved_ready, select_environment, _inputs
from vaws_workspace_entry import copy_workspace_identity, prepare_session
from vaws_workspace_update import (Deferred, SUBMODULES, clean_checkout, common_dir,
                                   git, gitlinks, initialized, prepared_source, run)


def native_paths(client: str, environment: dict, cwd: Path) -> tuple[Path, Path]:
    source_key = "CODEX_SOURCE_TREE_PATH" if client == "codex" else "ROOT_WORKTREE_PATH"
    if not environment.get(source_key):
        raise ValueError(f"{source_key} is missing; this entry belongs to native worktree creation")
    if client == "codex" and not environment.get("CODEX_WORKTREE_PATH"):
        raise ValueError("CODEX_WORKTREE_PATH is missing; the native client must supply its new worktree")
    source = Path(environment[source_key]).expanduser().resolve()
    target = Path(environment.get("CODEX_WORKTREE_PATH", cwd) if client == "codex" else cwd).resolve()
    if source == target or target != cwd.resolve():
        raise ValueError("native setup must run inside its new worktree, separate from the source")
    shared = common_dir(source).resolve()
    if shared != common_dir(target).resolve():
        raise ValueError("native source and worktree do not belong to the same Git repository")
    target_gitdir = Path(git(target, "rev-parse", "--absolute-git-dir")).resolve()
    if target_gitdir == shared or Path(git(target, "rev-parse", "--show-toplevel")).resolve() != target:
        raise ValueError("native setup target must be the root of a linked worktree, not the main checkout")
    return source, target


def unpinned_environment() -> dict:
    environment = dict(os.environ)
    for name in (PIN_ENV, MANAGED_PIN_ENV, "VAWS_CONTEXT_FILE", "VAWS_PARENT_CONTEXT", "VAWS_ATTACH_CONTEXT",
                 "VAWS_RELEASE_LAUNCH", "VAWS_VENV_REEXEC", "VAWS_SKIP_VENV_REEXEC", "VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"):
        environment.pop(name, None)
    return environment


def ready_for_target(source: Path, target: Path, environment: dict) -> dict:
    """Reuse matching source inputs, otherwise prepare only the required packages."""
    try:
        receipt = native_ready(source)
        if receipt["input_id"] == _inputs(target)[3]:
            return receipt
    except EnvironmentError:
        pass
    try:
        return native_ready(target)
    except EnvironmentError:
        result = run([getattr(sys, "_base_executable", sys.executable),
                      str(target / ".agents/scripts/vaws_deps.py"), "sync", "--packages-only", "--locked"],
                     cwd=target, env=environment, timeout=1800)
        return json.loads(result.stdout)["receipt"]


def configure_target(client: str, target: Path, receipt: dict, environment: dict) -> None:
    # The selected revision owns its wiring. No shared editing directory or
    # already-running client's hook/MCP commands are rewritten.
    arguments = [receipt["python"], str(target / ".agents/scripts/vaws_client_setup.py"),
                 "--client", client, "--project", str(target), "--apply"]
    if client == "kimi":
        # One installed Kimi lifecycle adapter routes each native event to its
        # selected environment; a new worktree must not append global hooks.
        arguments += ["--kimi-config", str(target / ".vaws-local/kimi-hooks.toml")]
    run(arguments, cwd=target, env={**environment, PIN_ENV: receipt["receipt"]}, timeout=120)


def active_submodules(target: Path, revision: str) -> dict[str, str]:
    active = {path: sha for path, sha in gitlinks(target, revision).items() if initialized(target, path)}
    for path, sha in active.items():
        if path not in SUBMODULES:
            raise Deferred("submodule_layout_changed", path)
        clean_checkout(target / path, branch=None, expected={sha})
        if any(initialized(target / path, child) for child in gitlinks(target / path, "HEAD")):
            raise Deferred("nested_submodule_initialized", path)
    return active


def advance_worktree(target: Path, prepared: Path, original: str,
                     branch: str | None, active: dict[str, str]) -> dict[str, str]:
    """Fast-forward one new checkout and its already-initialized components."""
    clean_checkout(target, branch=branch, expected={original})
    if active_submodules(target, original) != active:
        raise Deferred("submodule_state_changed")
    revision = git(prepared, "rev-parse", "HEAD")
    links = gitlinks(target, revision)
    for path in active:
        if path not in links:
            raise Deferred("submodule_layout_changed", path)
        module, sha = target / path, links[path]
        if run(["git", "cat-file", "-e", sha + "^{commit}"], cwd=module, check=False).returncode:
            # Preparation cached these objects. Import from that local checkout;
            # never initialize a missing module or contact its network remote.
            if not initialized(prepared, path):
                raise Deferred("submodule_object_unavailable", path)
            git(module, "fetch", "--no-tags", str(prepared / path), sha)
    git(target, "merge", "--ff-only", revision)
    for path in active:
        git(target / path, "checkout", "--detach", links[path])
    return {path: links[path] for path in active}


def prepare_worktree(client: str, source: Path, target: Path) -> dict:
    from vaws_local_owner import windows_mounted_workspace
    if windows_mounted_workspace(target):
        raise ValueError("run native worktree setup with the Windows owner for this mounted workspace")
    environment = unpinned_environment()
    # A repeated native setup or handoff reuses the existing selection even if
    # the user has since edited the lock. It never triggers another update.
    selection = target / ".vaws-local/environment-selection" / f"{sys.platform}.json"
    if selection.is_file():
        receipt = saved_ready(target)
        # Dependency sync can save a selection before wiring completes. Repair
        # that bounded step without changing the saved version or its inputs.
        configure_target(client, target, receipt, environment)
        return {"status": "reused", "workspace": str(target), "environment": receipt["key"]}

    original = git(target, "rev-parse", "HEAD")
    branch = git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False) or None
    result = {"status": "kept", "reason": "explicit_source"}
    prepared = None
    submodules = {}
    # Native clients may create a task from an explicitly selected older or
    # business commit. Preserve that choice, as well as copied local edits.
    try:
        clean_checkout(target, branch=branch)
        active = active_submodules(target, original)
        if original == git(source, "rev-parse", "HEAD"):
            result = prepare_session(source)
            prepared = prepared_source(source)
        if prepared is not None:
            # Only this newly supplied worktree moves. Recheck after potentially
            # slow preparation so an intervening edit stays with its author.
            submodules = advance_worktree(target, prepared, original, branch, active)
    except Deferred as exc:
        result = {"status": "kept", "reason": exc.reason}
        prepared = None
    identity = {}
    try:
        copy_workspace_identity(source, target)
    except (OSError, ValueError, RuntimeError) as exc:
        identity = {"status": "unavailable", "error": str(exc)}
    receipt = ready_for_target(prepared or source, target, environment)
    configure_target(client, target, receipt, environment)
    select_environment(target, receipt)
    return {"status": "ready", "workspace": str(target), "head": git(target, "rev-parse", "HEAD"),
            "environment": receipt["key"], "update": result, "submodules": submodules,
            **({"identity": identity} if identity else {})}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("codex", "cursor"), required=True)
    args = parser.parse_args(argv)
    # Native setup starts before session pins exist; inherited parent pins
    # must not choose this new directory's dependencies.
    os.environ.pop(PIN_ENV, None)
    os.environ.pop(MANAGED_PIN_ENV, None)
    try:
        source, target = native_paths(args.client, os.environ, Path.cwd())
        result = prepare_worktree(args.client, source, target)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as exc:
        result = {"status": "failed", "phase": "native_worktree_setup", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=False), file=sys.stderr, flush=True)
        return 1
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
