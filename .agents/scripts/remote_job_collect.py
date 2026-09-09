#!/usr/bin/env python3
"""Collect a remote-dev job directory via artifact-pull.

``vaws-remote-dev`` has no ``job_collect`` tool. This wrapper keeps the
command surface and pulls the job's remote directory when job-status
exposes it.
"""
from __future__ import annotations

import argparse
import json
import sys
from io import StringIO
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parents[1] / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=LIB_DIR.parent.parent)

from vaws_remote_dev import RemoteDevUnavailable, apply_consumer_environment, require_transport  # noqa: E402
from vaws_remote_target import add_target_args, print_json, selector_args  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect a remote job directory.", allow_abbrev=False)
    add_target_args(parser)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--local-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        require_transport()
        apply_consumer_environment()
        from remote_dev.cli import run_tool_main
    except (RemoteDevUnavailable, ImportError) as exc:
        print_json({"status": "failed", "error": str(exc)})
        return 2
    status_buf = StringIO()
    old_out = sys.stdout
    sys.stdout = status_buf
    try:
        rc = run_tool_main("job_status", [*selector_args(args), "--job-id", args.job_id])
    finally:
        sys.stdout = old_out
    try:
        payload = json.loads(status_buf.getvalue())
    except json.JSONDecodeError:
        print_json({
            "status": "failed",
            "error": (
                "vaws-remote-dev has no job_collect tool and job-status did not "
                "return JSON. Use remote_artifact_pull.py with an explicit --remote-path."
            ),
        })
        return 2
    result = payload.get("result") if isinstance(payload, dict) else None
    remote_path = None
    if isinstance(result, dict):
        extra = result.get("extra") if isinstance(result.get("extra"), dict) else {}
        refs = result.get("refs") if isinstance(result.get("refs"), dict) else {}
        remote_path = extra.get("remote_dir") or extra.get("job_dir") or refs.get("remote_dir")
    if not remote_path:
        print_json({
            "status": "failed",
            "error": (
                "vaws-remote-dev has no job_collect tool and job-status did not "
                "name a remote directory. Add a job_collect API to remote-dev, "
                "or pass the path to remote_artifact_pull.py --remote-path."
            ),
            "job_status": payload,
        })
        return 2
    local_dir = args.local_dir or (Path.cwd() / "artifacts" / "jobs" / args.job_id)
    return run_tool_main(
        "artifact_pull",
        [*selector_args(args), "--remote-path", str(remote_path), "--local-dir", str(local_dir)],
    )


if __name__ == "__main__":
    raise SystemExit(main())
