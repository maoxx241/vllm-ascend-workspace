#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_BRANCH = "vaws-top"
DEFAULT_URL = "http://127.0.0.1:8789/api/health"


class MonitorError(RuntimeError):
    pass


def progress(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def run(
    command: list[str],
    *,
    cwd: Path = REPO_ROOT,
    check: bool = True,
    relay: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if relay:
        if result.stdout:
            print(result.stdout.rstrip(), file=sys.stderr)
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "command failed").strip()[-4000:]
        raise MonitorError(f"{' '.join(command)}: {detail}")
    return result


def default_worktree() -> Path:
    return Path.home() / "vaws-worktrees" / REPO_ROOT.name / "npu-fleet-monitor"


def parse_worktrees(payload: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in [*payload.splitlines(), ""]:
        if not line:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def discover_worktree(branch: str) -> Path | None:
    result = run(["git", "worktree", "list", "--porcelain"])
    expected = f"refs/heads/{branch}"
    for record in parse_worktrees(result.stdout):
        if record.get("branch") == expected and record.get("worktree"):
            return Path(record["worktree"]).resolve()
    return None


def ref_exists(ref: str) -> bool:
    return run(["git", "show-ref", "--verify", ref], check=False).returncode == 0


def ensure_local_branch(branch: str) -> None:
    local_ref = f"refs/heads/{branch}"
    if ref_exists(local_ref):
        return

    remotes = run(["git", "remote"]).stdout.split()
    ordered = [name for name in ("origin", "upstream") if name in remotes]
    ordered.extend(name for name in remotes if name not in ordered)
    for remote in ordered:
        remote_ref = f"refs/remotes/{remote}/{branch}"
        if not ref_exists(remote_ref):
            progress(f"Fetching monitor branch {remote}/{branch}")
            fetched = run(
                ["git", "fetch", remote, f"refs/heads/{branch}:{remote_ref}"],
                check=False,
                relay=True,
            )
            if fetched.returncode != 0:
                continue
        if ref_exists(remote_ref):
            run(["git", "branch", "--track", branch, f"{remote}/{branch}"])
            return
    raise MonitorError(f"monitor branch {branch} was not found on any configured remote")


def resolve_worktree(branch: str, requested: Path | None, *, create: bool) -> Path:
    existing = discover_worktree(branch)
    if existing:
        if requested and requested.resolve() != existing:
            raise MonitorError(f"branch {branch} is already checked out at {existing}")
        return existing

    if create:
        ensure_local_branch(branch)
    target = (requested or default_worktree()).expanduser().resolve()
    if not create:
        raise MonitorError(f"no worktree is attached to {branch}")
    run(["git", "show-ref", "--verify", f"refs/heads/{branch}"])
    if target.exists() and any(target.iterdir()):
        raise MonitorError(f"target exists and is not an empty monitor worktree: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    progress(f"Creating monitor worktree at {target}")
    run(["git", "worktree", "add", str(target), branch], relay=True)
    return target


def validate_project(worktree: Path, branch: str) -> str:
    required = ("package.json", "scripts/install-user-service.sh", "deploy/npu-fleet-monitor.service")
    missing = [name for name in required if not (worktree / name).is_file()]
    if missing:
        raise MonitorError(f"monitor branch is missing required files: {', '.join(missing)}")
    actual = run(["git", "branch", "--show-current"], cwd=worktree).stdout.strip()
    if actual != branch:
        raise MonitorError(f"worktree branch is {actual or 'detached'}, expected {branch}")
    dirty = run(["git", "status", "--porcelain"], cwd=worktree).stdout.strip()
    if dirty:
        raise MonitorError("monitor worktree has source changes; preserve or commit them before deployment")
    return run(["git", "rev-parse", "HEAD"], cwd=worktree).stdout.strip()


def build_if_needed(worktree: Path, commit: str) -> bool:
    marker = worktree / "data" / ".deployed-commit"
    current = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    ready = (worktree / "node_modules").is_dir() and (worktree / "dist/client").is_dir()
    if current == commit and ready:
        progress("Locked build already matches the selected commit")
        return False

    version = run(["node", "--version"]).stdout.strip().lstrip("v")
    try:
        major = int(version.split(".", 1)[0])
    except ValueError as exc:
        raise MonitorError(f"cannot parse Node.js version: {version}") from exc
    if major < 22:
        raise MonitorError(f"Node.js 22+ is required, found {version}")

    progress("Installing locked frontend dependencies")
    run(["npm", "ci"], cwd=worktree, relay=True)
    progress("Running backend tests")
    run(["npm", "run", "test:backend"], cwd=worktree, relay=True)
    progress("Building the production dashboard")
    run(["npm", "run", "build"], cwd=worktree, relay=True)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(commit + "\n", encoding="utf-8")
    return True


def systemd_properties() -> dict[str, str]:
    result = run(
        ["systemctl", "--user", "show", "npu-fleet-monitor.service", "-p", "ActiveState", "-p", "SubState", "-p", "UnitFileState"],
        check=False,
    )
    values: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if result.returncode != 0 and not values:
        values["error"] = (result.stderr or "systemctl query failed").strip()[-1000:]
    return values


def health(wait_seconds: float = 0) -> tuple[bool, dict[str, Any] | None, str | None]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + wait_seconds
    error = "health endpoint unavailable"
    while True:
        try:
            with opener.open(DEFAULT_URL, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            return payload.get("status") == "ok", payload, None
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            error = str(exc)
        if time.monotonic() >= deadline:
            return False, None, error
        time.sleep(0.5)


def install_and_restart(worktree: Path) -> None:
    progress("Installing and enabling the user service")
    run([str(worktree / "scripts/install-user-service.sh")], cwd=worktree, relay=True)
    run(["systemctl", "--user", "restart", "npu-fleet-monitor.service"])


def payload_for(action: str, branch: str, worktree: Path | None, commit: str | None, built: bool | None) -> dict[str, Any]:
    ok, health_payload, health_error = health()
    return {
        "ok": ok,
        "action": action,
        "branch": branch,
        "commit": commit,
        "worktree": str(worktree) if worktree else None,
        "service": systemd_properties(),
        "url": "http://127.0.0.1:8788",
        "health": health_payload,
        "health_error": health_error,
        "built": built,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy and operate the local NPU fleet monitor")
    parser.add_argument("action", choices=("ensure", "status", "restart", "stop"))
    parser.add_argument("--branch", default=DEFAULT_BRANCH)
    parser.add_argument("--worktree", type=Path)
    args = parser.parse_args()

    try:
        worktree = resolve_worktree(args.branch, args.worktree, create=args.action == "ensure")
        commit = validate_project(worktree, args.branch)
        if args.action == "ensure":
            built = build_if_needed(worktree, commit)
            install_and_restart(worktree)
            ok, health_payload, health_error = health(wait_seconds=30)
            result = {
                "ok": ok,
                "action": args.action,
                "branch": args.branch,
                "commit": commit,
                "worktree": str(worktree),
                "service": systemd_properties(),
                "url": "http://127.0.0.1:8788",
                "health": health_payload,
                "health_error": health_error,
                "built": built,
            }
        elif args.action == "restart":
            run(["systemctl", "--user", "restart", "npu-fleet-monitor.service"])
            ok, health_payload, health_error = health(wait_seconds=30)
            result = payload_for(args.action, args.branch, worktree, commit, None)
            result.update({"ok": ok, "health": health_payload, "health_error": health_error})
        elif args.action == "stop":
            run(["systemctl", "--user", "stop", "npu-fleet-monitor.service"])
            result = payload_for(args.action, args.branch, worktree, commit, None)
            result["ok"] = result["service"].get("ActiveState") == "inactive"
        else:
            result = payload_for(args.action, args.branch, worktree, commit, None)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["ok"] else 1
    except (MonitorError, OSError) as exc:
        print(json.dumps({"ok": False, "action": args.action, "error": str(exc)}, ensure_ascii=False, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
