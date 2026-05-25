#!/usr/bin/env python3
"""Remove a VAWS session's service, container, worktree, and leases."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
MM_SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"
for _p in (str(LIB_DIR), str(MM_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from _workflow_common import remove_container  # noqa: E402
from vaws_session_state import (  # noqa: E402
    load_session_lookup,
    mark_session_status,
    release_all_session_leases,
    session_record_for_execution,
    session_serving_state_path,
)

PROGRESS_SENTINEL = "__VAWS_SESSION_PROGRESS__="


def emit_progress(phase: str, message: str, **extra: Any) -> None:
    payload = {"phase": phase, "message": message}
    payload.update({key: value for key, value in extra.items() if value is not None})
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def run_git(args: list[str], *, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(ROOT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def stop_session(session_id: str, *, session_file: Path | None = None, force: bool) -> dict[str, Any]:
    script = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts" / "serve_stop.py"
    cmd = [sys.executable, str(script)]
    if session_file is not None:
        cmd.extend(["--session-file", str(session_file)])
    else:
        cmd.extend(["--session-id", session_id])
    if force:
        cmd.append("--force")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT), check=False)
    if not result.stdout.strip():
        return {"status": "unknown", "returncode": result.returncode, "stderr_tail": result.stderr[-500:]}
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {"status": "unknown", "stdout_tail": result.stdout[-500:]}
    payload["returncode"] = result.returncode
    return payload


def stop_result_allows_lease_release(result: dict[str, Any]) -> bool:
    return result.get("returncode") == 0 and result.get("status") in {"not_found", "stopped"}


def container_result_allows_lease_release(result: dict[str, Any] | None) -> bool:
    if not result:
        return False
    if result.get("success") is False:
        return False
    return result.get("status") not in {"blocked", "failed", "needs_input", "needs_repair"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--session-id")
    parser.add_argument("--session-file")
    parser.add_argument("--remove-container", action="store_true")
    parser.add_argument("--remove-worktree", action="store_true")
    parser.add_argument("--release-leases", action="store_true")
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if not args.session_id and not args.session_file:
            print_json(
                {
                    "status": "needs_input",
                    "error": "session_remove requires explicit --session-id or --session-file",
                    "missing": ["--session-id", "--session-file"],
                }
            )
            return 1
        lookup = load_session_lookup(
            session_id=args.session_id,
            session_file=args.session_file,
            repo_root=ROOT,
        )
        session = lookup.session
        sid = session["session_id"]
        results: dict[str, Any] = {}

        serving_state_path = session_serving_state_path(sid, lookup.state_repo_root)
        if args.remove_container and not serving_state_path.exists():
            results["stop"] = {
                "status": "not_found",
                "returncode": 0,
                "skipped": True,
                "session_id": sid,
                "message": "no session serving state recorded; container removal will stop any untracked process",
            }
        else:
            emit_progress("stop", "stopping session service", session_id=sid)
            results["stop"] = stop_session(sid, session_file=lookup.session_file, force=args.force)

        if args.remove_container:
            emit_progress("container", "removing session container", session_id=sid)
            results["container"] = remove_container(session_record_for_execution(session))

        if args.remove_worktree:
            worktree_root = Path(session["local"]["worktree_root"])
            emit_progress("worktree", "removing session worktree", path=str(worktree_root))
            cmd = ["worktree", "remove"]
            if args.force:
                cmd.extend(["--force", "--force"])
            cmd.append(str(worktree_root))
            proc = run_git(cmd)
            results["worktree"] = {
                "returncode": proc.returncode,
                "stdout_tail": proc.stdout[-500:],
                "stderr_tail": proc.stderr[-500:],
            }

        if args.release_leases:
            can_release = stop_result_allows_lease_release(results["stop"]) or container_result_allows_lease_release(
                results.get("container")
            )
            if not can_release:
                results["leases"] = {
                    "released": False,
                    "blocked": True,
                    "reason": (
                        "service stop did not succeed and the session container was not removed; "
                        "refusing to release leases that may still protect live resources"
                    ),
                }
                print_json({"status": "failed", "session_id": sid, "results": results})
                return 1
            emit_progress("lease", "releasing session leases", session_id=sid)
            release_all_session_leases(repo_root=lookup.state_repo_root, session_id=sid)
            results["leases"] = {"released": True}

        if args.remove_container or args.remove_worktree:
            remove_ok = True
            if args.remove_container:
                remove_ok = remove_ok and container_result_allows_lease_release(results.get("container"))
            if args.remove_worktree:
                remove_ok = remove_ok and results.get("worktree", {}).get("returncode") == 0
            next_status = "removed" if remove_ok else "needs_repair"
        else:
            next_status = "stopped" if stop_result_allows_lease_release(results["stop"]) else "needs_repair"
        updated = mark_session_status(
            repo_root=lookup.state_repo_root,
            session_id=sid,
            status=next_status,
        )
        print_json({"status": updated["status"], "session_id": sid, "results": results})
        return 0 if updated["status"] in {"removed", "stopped"} else 1
    except Exception as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
