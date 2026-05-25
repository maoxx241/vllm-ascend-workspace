#!/usr/bin/env python3
"""Shared utilities for ascend-profiling-analysis scripts.

Responsibilities kept minimal on purpose:
  - resolve a machine (alias or IP) to an SSH endpoint via inventory
  - run remote bash commands and stream stdout/stderr back
  - tar-sync the framework subtree (``scripts/ascend_profile/``) to the
    remote work dir
  - read / validate the collection skill's manifest
  - manage local run directories under ``.vaws-local/profiling-analysis/runs/``
  - emit progress as ``__VAWS_PROFILE_ANALYSIS_PROGRESS__=<json>`` on stderr

This script intentionally does NOT contain any profiling analysis logic. The
real pipeline lives next to it under ``scripts/ascend_profile/`` and is run
remotely.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
MM_SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"

for _p in (str(LIB_DIR), str(MM_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import inventory as inventory_store  # noqa: E402
from vaws_session_state import load_session_lookup, session_record_for_execution  # noqa: E402

ANALYSIS_STATE_DIR = ROOT / ".vaws-local" / "profiling-analysis" / "runs"
PROGRESS_SENTINEL = "__VAWS_PROFILE_ANALYSIS_PROGRESS__="

DEFAULT_REMOTE_WORK_DIR = "/tmp/ascend_profile_framework"
# The analysis framework lives next to this file as a sibling package; it is
# tar-synced to the remote work dir's ``ascend_profile/`` subpath and invoked
# as ``python3 -m ascend_profile.<stage>`` from that work dir.
FRAMEWORK_LOCAL_DIR = Path(__file__).resolve().parent / "ascend_profile"
FRAMEWORK_REMOTE_SUBPATH = "ascend_profile"
FRAMEWORK_PYTHON_MODULE = "ascend_profile"

# Back-compat aliases (will be removed once all call sites use the new names).
TOOLS_LOCAL_DIR = FRAMEWORK_LOCAL_DIR
TOOLS_REMOTE_SUBPATH = FRAMEWORK_REMOTE_SUBPATH

REQUIRED_SINGLE_ARTIFACTS = (
    "manifest.json",
    "segment_manifest.json",
    "diagnosis_findings.json",
    "report/report.md",
    "report/report.xlsx",
    "report/report.html",
)

# Stage-aware artifact validation: the minimum set of files that must exist
# in the remote output dir once a given stage has finished. Used by the
# wrapper so that ``--only-stage normalize`` doesn't get rejected for not
# producing ``report/report.md``.
#
# The keys match ``ascend_profile.analyze.STAGE_ORDER``; each value is the
# *cumulative* set assumed to be present after that stage runs (so checking
# the end-stage set is enough).
REQUIRED_ARTIFACTS_BY_END_STAGE = {
    "normalize": (
        "manifest.json",
        "normalize_manifest.json",
        "normalized_event_index.csv",
    ),
    "segment": (
        "manifest.json",
        "normalize_manifest.json",
        "segment_manifest.json",
        "step_segments.json",
        "layer_segments.json",
    ),
    "classify": (
        "manifest.json",
        "segment_manifest.json",
        "classify_manifest.json",
        "block_segments.json",
        "class_signatures.json",
    ),
    "summarize": (
        "manifest.json",
        "classify_manifest.json",
        "summary_manifest.json",
        "rank_summary.csv",
        "step_summary.csv",
    ),
    "cross_rank": (
        "manifest.json",
        "summary_manifest.json",
        "cross_rank_manifest.json",
        "cross_rank_alignment.csv",
    ),
    "diagnostics": (
        "manifest.json",
        "summary_manifest.json",
        "diagnosis_findings.json",
    ),
    "report": REQUIRED_SINGLE_ARTIFACTS,
}

# Artifacts that are cheap to pull back to the user's workstation. Big ones
# (normalized_event_index.csv, evidence/bubble_windows.jsonl) are intentionally
# excluded -- agents that need them should ssh in and grep, not download.
LIGHTWEIGHT_PULL_PATHS = (
    "manifest.json",
    "normalize_manifest.json",
    "segment_manifest.json",
    "classify_manifest.json",
    "summary_manifest.json",
    "cross_rank_manifest.json",
    "diagnosis_findings.json",
    "evidence_index.csv",
    "raw_kernel_index.csv",
    "rank_summary.csv",
    "step_summary.csv",
    "step_anatomy.csv",
    "step_class_summary.csv",
    "layer_summary.csv",
    "layer_class_summary.csv",
    "block_summary.csv",
    "block_class_summary.csv",
    "operator_summary.csv",
    "operator_class_summary.csv",
    "hccl_op_summary.csv",
    "hccl_class_summary.csv",
    "wait_anchor_ops.csv",
    "aicpu_summary.csv",
    "cross_rank_alignment.csv",
    "cross_rank_alignment.json",
    "step_segments.json",
    "layer_segments.json",
    "block_segments.json",
    "class_signatures.json",
    "structure_evidence_graph.json",
    "report/manifest.json",
    "report/report.md",
    "report/report.xlsx",
    "report/report.html",
)


# ---------------------------------------------------------------------------
# SSH endpoint
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SshEndpoint:
    host: str
    port: int
    user: str = "root"

    def destination(self) -> str:
        return f"{self.user}@{self.host}"


def resolve_machine(identifier: str) -> dict[str, Any]:
    read_path = inventory_store.read_inventory_path(
        inventory_store.preferred_inventory_path(inventory_store.DEFAULT_PATH)
    )
    inv = inventory_store.load_inventory(read_path)
    for m in inv.get("machines", []):
        alias = m.get("alias", "")
        host = m.get("host", {})
        host_ip = host.get("ip", "") if isinstance(host, dict) else host
        if alias == identifier or host_ip == identifier:
            return m
    raise ValueError(
        f"machine '{identifier}' not found in inventory; run machine-management skill first"
    )


def endpoint_from_machine(machine: dict[str, Any]) -> SshEndpoint:
    host_info = machine.get("host", {})
    container_info = machine.get("container", {})
    if isinstance(host_info, dict):
        ip = host_info.get("ip", "")
        host_port = int(host_info.get("port", 22))
        user = host_info.get("user", "root")
    else:
        ip = host_info
        host_port = 22
        user = "root"
    ssh_port = int(container_info.get("ssh_port", host_port))
    return SshEndpoint(host=ip, port=ssh_port, user=user)


def get_machine_alias(machine: dict[str, Any]) -> str:
    host = machine.get("host", {})
    if isinstance(host, dict):
        host_ip = host.get("ip", "unknown")
    else:
        host_ip = host or "unknown"
    return machine.get("alias", host_ip)


def resolve_execution_target(
    machine: str | None,
    *,
    session_id: str | None = None,
    session_file: str | Path | None = None,
) -> dict[str, Any]:
    if session_id or session_file:
        lookup = load_session_lookup(
            session_id=session_id,
            session_file=session_file,
            repo_root=ROOT,
        )
        record = session_record_for_execution(lookup.session)
        return {
            "mode": "session",
            "record": record,
            "alias": get_machine_alias(record),
            "endpoint": endpoint_from_machine(record),
            "session_id": lookup.session["session_id"],
            "session_file": str(lookup.session_file),
            "session": lookup.session,
        }
    if not machine:
        raise ValueError("--machine is required unless --session-id or --session-file is used")
    record = resolve_machine(machine)
    return {
        "mode": "legacy",
        "record": record,
        "alias": get_machine_alias(record),
        "endpoint": endpoint_from_machine(record),
        "session_id": None,
        "session_file": None,
        "session": None,
    }


# ---------------------------------------------------------------------------
# Progress / output
# ---------------------------------------------------------------------------

def progress(phase: str, message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"phase": phase, "message": message}
    payload.update({k: v for k, v in extra.items() if v is not None})
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def print_json(data: dict[str, Any]) -> None:
    json.dump(data, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Remote command execution
# ---------------------------------------------------------------------------

def _ssh_base_cmd(endpoint: SshEndpoint) -> list[str]:
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=10",
        "-o", "LogLevel=ERROR",
        "-p", str(endpoint.port),
        endpoint.destination(),
    ]


def ssh_exec(
    endpoint: SshEndpoint,
    script: str,
    *,
    check: bool = True,
    timeout: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a one-shot bash snippet on the remote host."""
    cmd = [*_ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(script)]
    result = subprocess.run(
        cmd, capture_output=True, text=True, check=False, timeout=timeout
    )
    if check and result.returncode != 0:
        tail_err = result.stderr.strip().splitlines()[-50:]
        raise RuntimeError(
            "remote command failed (rc={rc})\n"
            "  cmd: {cmd}\n"
            "  stderr tail:\n{err}".format(
                rc=result.returncode,
                cmd=script.strip().splitlines()[0][:200],
                err="\n".join(tail_err),
            )
        )
    return result


def ssh_stream(
    endpoint: SshEndpoint,
    script: str,
    *,
    forward_prefix: str = "[remote] ",
    timeout: int | None = None,
) -> int:
    """Run a remote command, streaming stdout/stderr to local stderr.

    Returns the remote exit code. Useful for long-running ``analyze.py`` runs
    where users want to see stage progress live.

    Silent-hang handling: ``timeout`` is enforced two ways at once. First, the
    remote command is wrapped in ``timeout --preserve-status <s>s bash -c …``
    so an unresponsive remote process is killed at the source even if it
    stops producing output. Second, the local reader uses ``select.select``
    with a small slice so wall-clock timeouts are honoured immediately even
    when stdout pipes through a slow buffer.
    """
    import select

    remote_payload = script
    if timeout is not None and timeout > 0:
        # Add a small grace margin (5 s) so the remote-side ``timeout`` fires
        # first and exits with a useful message before the local killer takes
        # over. We still keep ``--preserve-status`` to surface the wrapped
        # command's real exit code on success.
        margin = max(int(timeout) - 5, 1)
        remote_payload = (
            f"timeout --preserve-status {margin}s bash -lc "
            f"{shlex.quote(script)}"
        )

    cmd = [*_ssh_base_cmd(endpoint), "bash", "-c", shlex.quote(remote_payload)]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    started = time.time()
    deadline = started + timeout if timeout is not None else None
    try:
        while True:
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    proc.kill()
                    raise TimeoutError(
                        f"remote command exceeded {timeout}s (no output for the wall-clock window)"
                    )
                # Slice the select wait so we react to deadline promptly.
                wait = min(remaining, 5.0)
            else:
                wait = 5.0
            ready, _, _ = select.select([fd], [], [], wait)
            if ready:
                line = proc.stdout.readline()
                if not line:
                    break
                sys.stderr.write(
                    forward_prefix + line if not line.startswith(forward_prefix) else line
                )
                sys.stderr.flush()
            else:
                # No data this slice; loop and re-check deadline. If the
                # process has already exited we'd see eof on next readline.
                if proc.poll() is not None:
                    # Drain anything still buffered.
                    remainder = proc.stdout.read()
                    if remainder:
                        sys.stderr.write(forward_prefix + remainder)
                        sys.stderr.flush()
                    break
        return proc.wait()
    finally:
        if proc.poll() is None:
            proc.terminate()


# ---------------------------------------------------------------------------
# tar-over-ssh sync helpers (rsync is not always installed in Ascend containers)
# ---------------------------------------------------------------------------

def _ssh_pipe_cmd(endpoint: SshEndpoint, remote_cmd: str) -> list[str]:
    """SSH command that runs a remote shell snippet, suitable for tar piping."""
    return [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        "-p", str(endpoint.port),
        endpoint.destination(),
        remote_cmd,
    ]


def sync_to_remote(
    endpoint: SshEndpoint,
    local_path: Path,
    remote_path: str,
    *,
    extra_excludes: Iterable[str] = ("__pycache__", "*.pyc"),
) -> None:
    """Mirror ``local_path/`` into ``remote_path/`` using ``tar | ssh tar -x``.

    Implements --delete by clearing ``remote_path`` first, then unpacking the
    tarball. Lightweight on purpose: callers pick the smallest subtree they
    need (typically ``scripts/ascend_profile/``).
    """
    if not local_path.exists():
        raise FileNotFoundError(f"local path does not exist: {local_path}")
    if not local_path.is_dir():
        raise NotADirectoryError(f"sync source must be a directory: {local_path}")

    progress("parity", "tar local -> remote", src=str(local_path), dst=remote_path)

    # Wipe + recreate the remote directory (mimics rsync --delete).
    ssh_exec(
        endpoint,
        f"rm -rf {shlex.quote(remote_path)} && mkdir -p {shlex.quote(remote_path)}",
        check=True,
        timeout=120,
    )

    tar_args = ["tar", "-cz"]
    for pattern in extra_excludes:
        tar_args.extend(["--exclude", pattern])
    tar_args.extend(["-C", str(local_path), "."])
    remote_unpack = f"tar -xz -C {shlex.quote(remote_path)}"

    tar_proc = subprocess.Popen(tar_args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    ssh_proc = subprocess.Popen(
        _ssh_pipe_cmd(endpoint, remote_unpack),
        stdin=tar_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if tar_proc.stdout is not None:
        tar_proc.stdout.close()  # let ssh_proc receive EOF when tar exits
    ssh_out, ssh_err = ssh_proc.communicate()
    tar_err = tar_proc.stderr.read() if tar_proc.stderr else b""
    tar_proc.wait()
    if tar_proc.returncode != 0:
        raise RuntimeError(
            "local tar failed (rc={rc}): {err}".format(
                rc=tar_proc.returncode, err=tar_err.decode("utf-8", "replace")[:1000]
            )
        )
    if ssh_proc.returncode != 0:
        raise RuntimeError(
            "remote tar -x failed (rc={rc}): {err}".format(
                rc=ssh_proc.returncode, err=ssh_err.decode("utf-8", "replace")[:1000]
            )
        )


def sync_from_remote(
    endpoint: SshEndpoint,
    remote_path: str,
    local_path: Path,
    *,
    include_paths: Iterable[str] | None = None,
) -> None:
    """Mirror ``remote_path/`` into ``local_path/`` using ``ssh tar -c | tar -x``.

    When ``include_paths`` is provided, only those relative paths are tarred
    on the remote side. Missing paths are silently skipped (some sweep roots
    are produced even when an analyze stage degrades, and we don't want to
    fail the whole pull because of one missing optional file).
    """
    local_path.mkdir(parents=True, exist_ok=True)
    progress("artifact_pull", "tar remote -> local", src=remote_path, dst=str(local_path))

    if include_paths is None:
        # Pull the whole directory.
        remote_pack = f"cd {shlex.quote(remote_path)} && tar -cz ."
    else:
        # Build a remote bash snippet that tars only the existing requested
        # paths. Paths that do not exist remotely are skipped with a warning
        # to stderr (which we forward via ssh stderr).
        existing = " ".join(shlex.quote(p) for p in include_paths)
        remote_pack = (
            f"cd {shlex.quote(remote_path)} && "
            f"present=(); for p in {existing}; do "
            f"  if [ -e \"$p\" ]; then present+=(\"$p\"); else "
            f"    echo \"skip missing: $p\" 1>&2; fi; "
            f"done; "
            f"if [ ${{#present[@]}} -eq 0 ]; then exit 0; fi; "
            f"tar -cz \"${{present[@]}}\""
        )

    ssh_proc = subprocess.Popen(
        _ssh_pipe_cmd(endpoint, remote_pack),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    tar_proc = subprocess.Popen(
        ["tar", "-xz", "-C", str(local_path)],
        stdin=ssh_proc.stdout,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if ssh_proc.stdout is not None:
        ssh_proc.stdout.close()
    tar_out, tar_err = tar_proc.communicate()
    ssh_err = ssh_proc.stderr.read() if ssh_proc.stderr else b""
    ssh_proc.wait()
    if ssh_proc.returncode != 0:
        raise RuntimeError(
            "remote tar -c failed (rc={rc}): {err}".format(
                rc=ssh_proc.returncode, err=ssh_err.decode("utf-8", "replace")[:1000]
            )
        )
    # tar -x can exit 0 with empty stdin (no requested paths existed); only
    # bail out on a real non-zero local tar exit.
    if tar_proc.returncode not in (0,):
        raise RuntimeError(
            "local tar -x failed (rc={rc}): {err}".format(
                rc=tar_proc.returncode, err=tar_err.decode("utf-8", "replace")[:1000]
            )
        )


# ---------------------------------------------------------------------------
# Run dir / manifest helpers
# ---------------------------------------------------------------------------

def ensure_run_dir(
    tag: str = "",
    *,
    explicit_dir: str | None = None,
    overwrite: bool = False,
) -> Path:
    """Return the local run directory to write pulled artifacts into.

    - When ``explicit_dir`` is given, it is used verbatim.  If the path
      already exists and is non-empty, ``FileExistsError`` is raised unless
      ``overwrite=True``.
    - Otherwise a fresh ``<state-dir>/<timestamp>_<tag>/`` directory is
      created under ``.vaws-local/profiling-analysis/runs/``.
    """
    if explicit_dir:
        d = Path(explicit_dir).expanduser().resolve()
        if d.exists():
            if d.is_file():
                raise FileExistsError(
                    f"--local-output-dir points at an existing file: {d}"
                )
            if any(d.iterdir()) and not overwrite:
                raise FileExistsError(
                    f"--local-output-dir is not empty: {d}; "
                    "pass --overwrite to use it anyway"
                )
        d.mkdir(parents=True, exist_ok=True)
        return d

    ts = time.strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{tag}" if tag else ts
    d = ANALYSIS_STATE_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_collection_manifest(manifest_path: Path) -> dict[str, Any]:
    """Read and shallow-validate a manifest produced by ascend-profiling-collection.

    The manifest contract is: {schema_version, analysis_status, remote_profile_root, ...}.
    We require ``analysis_status == "ok"`` and ``remote_profile_root`` to be a
    non-empty string. Anything else is a hard fail; this skill never tries to
    repair an incomplete collection.
    """
    if not manifest_path.is_file():
        raise FileNotFoundError(f"manifest not found: {manifest_path}")
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise RuntimeError(f"manifest is not valid JSON: {manifest_path} ({e})") from e

    status = data.get("analysis_status")
    if status != "ok":
        raise RuntimeError(
            "collection manifest is not analyzable: "
            f"analysis_status={status!r} at {manifest_path}; "
            "fix the collection run before invoking analysis"
        )

    remote_root = data.get("remote_profile_root")
    if not isinstance(remote_root, str) or not remote_root.strip():
        raise RuntimeError(
            f"manifest missing remote_profile_root: {manifest_path}"
        )
    return data


def remote_python_with_module(endpoint: SshEndpoint, module: str) -> str:
    """Find a python3 on the remote host that can import ``module``.

    Defaults match ascend-memory-profiling for consistency. Falls back to
    plain ``python3`` so that hosts without a CANN-specific interpreter still
    work for static analysis of a kernel_details.csv (no torch_npu needed).
    """
    candidates = [
        "/usr/local/python3.11.14/bin/python3",
        "/usr/local/python3.10/bin/python3",
        "python3",
    ]
    for cand in candidates:
        check = ssh_exec(
            endpoint,
            f"{cand} -c 'import {module}' 2>/dev/null && echo OK || true",
            check=False,
            timeout=30,
        )
        if "OK" in check.stdout:
            return cand
    # Final fallback: ascend-profile-analysis only needs stdlib (no torch_npu),
    # so plain python3 is acceptable even if the import probe failed.
    return "python3"


def quote_remote(path: str) -> str:
    return shlex.quote(path)
