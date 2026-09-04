#!/usr/bin/env python3
"""Shared utilities for ascend-profiling-analysis scripts.

Responsibilities kept minimal on purpose:
  - resolve the session target (explicit --session-id/--session-file or the
    bound session of the cwd worktree) to an SSH endpoint
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
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"

if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_local_state import allocate_run_dir  # noqa: E402
from vaws_remote_toolbox import (  # noqa: E402
    SshEndpoint,
    container_endpoint_from_record,
    emit_progress as _lib_emit_progress,
    print_json as _lib_print_json,
    ssh_exec,
)
from vaws_session_state import (  # noqa: E402
    SessionStateError,
    load_session_lookup,
    session_record_for_execution,
)
from vaws_ssh import base_ssh_options  # noqa: E402

ANALYSIS_STATE_DIR = ROOT / ".vaws-local" / "profiling-analysis" / "runs"
PROGRESS_SENTINEL = "__VAWS_PROFILE_ANALYSIS_PROGRESS__="

DEFAULT_REMOTE_WORK_DIR = "/tmp/ascend_profile_framework"
SSH_CONNECT_TIMEOUT_SECONDS = 15
# The analysis framework lives next to this file as a sibling package; it is
# tar-synced to the remote work dir's ``ascend_profile/`` subpath and invoked
# as ``python3 -m ascend_profile.<stage>`` from that work dir.
FRAMEWORK_LOCAL_DIR = Path(__file__).resolve().parent / "ascend_profile"
FRAMEWORK_REMOTE_SUBPATH = "ascend_profile"
FRAMEWORK_PYTHON_MODULE = "ascend_profile"

REQUIRED_SINGLE_ARTIFACTS = (
    "manifest.json",
    "segment_manifest.json",
    "diagnosis_findings.json",
    "report/report.md",
    "report/report.xlsx",
    "report/report.html",
)

# Fast mode (profile_analyze --mode fast) runs the remote analyze with
# --skip-xlsx --skip-host-trace --report-mode summary: report.xlsx is never
# written (the HTML stub still is), and analysis_summary.json becomes the
# primary machine-readable output, so it is required instead.
REQUIRED_SINGLE_ARTIFACTS_FAST = (
    "manifest.json",
    "segment_manifest.json",
    "diagnosis_findings.json",
    "report/report.md",
    "report/analysis_summary.json",
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
    "rank_summary.csv",
    "step_summary.csv",
    "step_anatomy.csv",
    "step_class_summary.csv",
    "layer_class_summary.csv",
    "block_class_summary.csv",
    "operator_class_summary.csv",
    "operator_efficiency_summary.csv",
    "model_insights.json",
    "model_context_summary.csv",
    "model_inferred_config.csv",
    "model_feature_summary.csv",
    "model_layer_type_summary.csv",
    "model_candidate_summary.csv",
    "model_config_overview.csv",
    "model_parameter_estimate.csv",
    "model_kv_cache_estimate.csv",
    "model_config_feature_summary.csv",
    "hardware_insights.json",
    "hardware_summary.csv",
    "hardware_theoretical_peaks.csv",
    "hccl_op_summary.csv",
    "hccl_class_summary.csv",
    "wait_anchor_ops.csv",
    "aicpu_summary.csv",
    "report/manifest.json",
    "report/report.md",
    "report/report.xlsx",
    "report/report.html",
    "report/analysis_summary.json",
    # html_report_v2's lazy-loaded data; without it the pulled report.html
    # is a dead shell outside the remote host.
    "report/assets",
    # Per-row giants (block_summary.csv, layer_summary.csv, operator_summary.csv,
    # evidence_index.csv, cross_rank_alignment.*, *_segments.json,
    # class_signatures.json, structure_evidence_graph.json, raw_kernel_index.csv)
    # stay on the remote; use --keep-remote-output to mirror everything.
)

# Fast-mode pull list (profile_analyze --mode fast): only the agent-facing
# compact artifacts come back -- report.md + analysis_summary.json, every
# *_manifest.json, diagnosis_findings.json, and the class-level summary CSVs.
# The bulky per-row tables (evidence_index.csv, cross_rank_alignment.*,
# operator_summary.csv, step_anatomy.csv, layer/block_summary.csv, ...) stay
# on the remote; agents that need them should ssh in and grep.
FAST_PULL_PATHS = (
    "manifest.json",
    "normalize_manifest.json",
    "segment_manifest.json",
    "classify_manifest.json",
    "summary_manifest.json",
    "cross_rank_manifest.json",
    "diagnosis_findings.json",
    "rank_summary.csv",
    "step_summary.csv",
    "step_class_summary.csv",
    "layer_class_summary.csv",
    "block_class_summary.csv",
    "operator_class_summary.csv",
    "hccl_class_summary.csv",
    "report/manifest.json",
    "report/report.md",
    "report/analysis_summary.json",
)


# ---------------------------------------------------------------------------
# SSH endpoint (SshEndpoint itself is imported from vaws_remote_toolbox)
# ---------------------------------------------------------------------------

def get_machine_alias(machine: dict[str, Any]) -> str:
    host = machine.get("host", {})
    if isinstance(host, dict):
        host_ip = host.get("ip", "unknown")
    else:
        host_ip = host or "unknown"
    return machine.get("alias", host_ip)


def resolve_execution_target(
    *,
    session_id: str | None = None,
    session_file: str | Path | None = None,
) -> dict[str, Any]:
    """Resolve the session execution target (session-only).

    With no explicit id/file the session is auto-resolved from the nearest
    worktree binding (cwd upward).
    """
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
        "endpoint": container_endpoint_from_record(record),
        "session_id": lookup.session["session_id"],
        "session_file": str(lookup.session_file),
        "session": lookup.session,
    }


# ---------------------------------------------------------------------------
# Progress / output (thin wrappers over the lib primitives; they keep this
# skill's sentinel prefix and historical function names)
# ---------------------------------------------------------------------------

def progress(phase: str, message: str, **extra: Any) -> None:
    _lib_emit_progress(phase, message, sentinel=PROGRESS_SENTINEL, **extra)


def print_json(data: dict[str, Any]) -> None:
    _lib_print_json(data)


# ---------------------------------------------------------------------------
# Remote command execution
#
# ``ssh_exec`` is imported from vaws_remote_toolbox (bounded connect phase,
# wall-clock timeout mapped to rc=255). ``_ssh_base_cmd`` stays local because
# ``ssh_stream`` adds ServerAlive keepalive options for long-running streams
# (and the timeout regression tests patch it).
# ---------------------------------------------------------------------------

def _ssh_base_cmd(endpoint: SshEndpoint) -> list[str]:
    # mux=False: this builder serves ssh_stream's hour-scale sessions, and a
    # muxed channel can outlive the remote side without noticing (observed:
    # remote analyze completed, mux master alive, session client hung until
    # the local timeout). Same failure class as the collection tunnel.
    return [
        "ssh",
        *base_ssh_options(connect_timeout=SSH_CONNECT_TIMEOUT_SECONDS, mux=False),
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=10",
        "-p", str(endpoint.port),
        endpoint.destination(),
    ]


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
        *base_ssh_options(),
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
    - Otherwise a fresh ``<state-dir>/<utc-timestamp>_<tag>/`` directory is
      allocated (collision-safe) under ``.vaws-local/profiling-analysis/runs/``.
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

    return allocate_run_dir(ANALYSIS_STATE_DIR, tag)


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


def remote_python_with_module(
    endpoint: SshEndpoint,
    module: str,
    *,
    required: bool = False,
) -> str:
    """Find a python3 on the remote host that can import ``module``.

    Defaults match ascend-memory-profiling for consistency. Optional probes
    fall back to plain ``python3``; required probes fail closed so a missing
    analysis dependency is reported before framework sync or execution.
    """
    candidates = [
        "/usr/local/python3.11.14/bin/python3",
        "/usr/local/python3.10/bin/python3",
        "python3",
    ]
    for cand in candidates:
        try:
            check = ssh_exec(
                endpoint,
                f"{cand} -c 'import {module}' 2>/dev/null && echo OK || true",
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            if required:
                raise RuntimeError(
                    f"remote python probe timed out after {exc.timeout}s while "
                    f"checking required module {module!r} with candidate {cand!r}; "
                    "check SSH connectivity before starting analysis"
                ) from exc
            continue
        if "OK" in check.stdout:
            return cand
    if required:
        raise RuntimeError(
            f"no supported remote Python can import required module {module!r}; "
            "prepare the runtime with the profiling-analysis requirements "
            "before starting analysis"
        )
    # Optional callers retain the historical fallback to plain python3.
    return "python3"


def quote_remote(path: str) -> str:
    return shlex.quote(path)


# ---------------------------------------------------------------------------
# Wrapper orchestration (shared by profile_analyze.py / profile_sweep.py)
#
# Failure contract: every failure prints one {"status": "failed", ...} JSON
# object on stdout and returns a phase-scoped exit code:
#   2 = pre-remote setup (manifest_validation / resolve / dependency_preflight
#       / setup)
#   3 = parity_sync (framework tar-sync)
#   4 = remote execution (remote_analyze / remote_sweep)
#   5 = validation (artifact_validation / summary_pull)
#   6 = artifact_pull
# ---------------------------------------------------------------------------

WRAPPER_PHASE_EXIT_CODES = {
    "manifest_validation": 2,
    "resolve": 2,
    "dependency_preflight": 2,
    "setup": 2,
    "parity_sync": 3,
    "remote_analyze": 4,
    "remote_sweep": 4,
    "artifact_validation": 5,
    "summary_pull": 5,
    "artifact_pull": 6,
}


def fail_return(phase: str, error: Any, **extra: Any) -> int:
    """Print the wrapper failure JSON for ``phase`` and return its exit code.

    Extra fields whose value is None are dropped so each wrapper keeps its
    historical payload shape.
    """
    payload: dict[str, Any] = {"status": "failed", "phase": phase, "error": str(error)}
    payload.update({k: v for k, v in extra.items() if v is not None})
    print_json(payload)
    return WRAPPER_PHASE_EXIT_CODES[phase]


def resolve_wrapper_target(
    *,
    session_id: str | None = None,
    session_file: str | Path | None = None,
) -> tuple[dict[str, Any] | None, int | None]:
    """Resolve the session target; on failure emit phase=resolve (exit 2)."""
    try:
        return resolve_execution_target(session_id=session_id, session_file=session_file), None
    except (ValueError, SessionStateError) as exc:
        return None, fail_return("resolve", exc)


def require_remote_python(
    endpoint: SshEndpoint,
    *,
    alias: str,
    session_id: str | None,
    module: str = "yaml",
) -> tuple[str | None, int | None]:
    """Preflight a remote python that can import ``module`` (exit 2 on failure)."""
    try:
        return remote_python_with_module(endpoint, module, required=True), None
    except RuntimeError as exc:
        return None, fail_return(
            "dependency_preflight", exc, machine=alias, session_id=session_id
        )


def prepare_run_dir(
    tag: str,
    *,
    explicit_dir: str | None = None,
    overwrite: bool = False,
    alias: str,
    session_id: str | None = None,
) -> tuple[Path | None, int | None]:
    """Create the local run dir (exit 2 on failure) and log the setup line."""
    try:
        run_dir = ensure_run_dir(tag, explicit_dir=explicit_dir, overwrite=overwrite)
    except FileExistsError as exc:
        return None, fail_return("setup", exc, machine=alias, session_id=session_id)
    progress("setup", "local run dir created", path=str(run_dir))
    return run_dir, None


def sync_framework(
    endpoint: SshEndpoint, remote_work_dir: str, remote_output_dir: str
) -> str:
    """Create the remote dirs and tar-sync ``scripts/ascend_profile/``.

    Returns the remote framework dir. Raises RuntimeError / FileNotFoundError;
    callers convert with ``fail_return("parity_sync", ...)`` (exit 3).
    """
    remote_framework_dir = f"{remote_work_dir}/{FRAMEWORK_REMOTE_SUBPATH}"
    ssh_exec(
        endpoint,
        f"mkdir -p {quote_remote(remote_framework_dir)} "
        f"{quote_remote(remote_output_dir)}",
        check=True,
        timeout=60,
    )
    sync_to_remote(endpoint, FRAMEWORK_LOCAL_DIR, remote_framework_dir)
    return remote_framework_dir


def stream_remote_command(
    endpoint: SshEndpoint,
    cmd: str,
    *,
    forward_prefix: str,
    timeout: int | None,
    fail_phase: str,
    **fail_extra: Any,
) -> tuple[int | None, int | None]:
    """Run ``ssh_stream`` with the wall-clock budget.

    Returns ``(rc, None)`` on completion; a wall-clock TimeoutError prints the
    ``fail_phase`` failure JSON (exit 4) and returns ``(None, code)``.
    """
    try:
        return ssh_stream(endpoint, cmd, forward_prefix=forward_prefix, timeout=timeout), None
    except TimeoutError as exc:
        return None, fail_return(fail_phase, exc, **fail_extra)


def pull_artifacts(
    endpoint: SshEndpoint,
    remote_output_dir: str,
    run_dir: Path,
    *,
    keep_remote_output: bool,
    include_paths: Iterable[str],
) -> None:
    """Pull artifacts back to the local run dir.

    Raises RuntimeError; callers convert with ``fail_return("artifact_pull",
    ...)`` (exit 6).
    """
    if keep_remote_output:
        sync_from_remote(endpoint, remote_output_dir, run_dir)
    else:
        sync_from_remote(
            endpoint, remote_output_dir, run_dir, include_paths=include_paths
        )
