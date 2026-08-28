#!/usr/bin/env python3
"""Shared utilities for vllm-ascend-benchmark scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_session_state import load_session_lookup, session_benchmark_dir  # noqa: E402
from vaws_ssh import base_ssh_options  # noqa: E402
from vaws_validate import require_env_name  # noqa: E402

SERVING_SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
NIGHTLY_CONFIGS_DIR = (
    ROOT / "vllm-ascend" / "tests" / "e2e" / "nightly"
    / "single_node" / "models" / "configs"
)
PROGRESS_SENTINEL = "__VAWS_BENCHMARK_PROGRESS__="


# ---------------------------------------------------------------------------
# Progress / output helpers
# ---------------------------------------------------------------------------

def emit_progress(phase: str, message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"phase": phase, "message": message}
    payload.update({k: v for k, v in extra.items() if v is not None})
    sys.stderr.write(PROGRESS_SENTINEL + json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stderr.flush()


def print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def now_utc() -> str:
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def safe_token(value: str) -> str:
    token = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in value)
    return token.strip(".-") or "benchmark"


def benchmark_runs_dir(config: "BenchConfig") -> Path:
    lookup = load_session_lookup(
        session_id=config.session_id,
        session_file=config.session_file,
        repo_root=ROOT,
    )
    return session_benchmark_dir(lookup.session["session_id"], lookup.state_repo_root) / "runs"


def write_local_result(config: "BenchConfig", result: dict[str, Any]) -> Path:
    runs_dir = benchmark_runs_dir(config)
    runs_dir.mkdir(parents=True, exist_ok=True)
    target_token = safe_token(config.session_id or "benchmark")
    filename = (
        f"{now_utc().replace(':', '-')}_{target_token}_"
        f"{os.getpid()}_{uuid.uuid4().hex[:8]}.json"
    )
    result_path = runs_dir / filename
    result["result_path"] = str(result_path)
    result["run_dir"] = str(runs_dir)
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result_path


def _run_json_command_streaming(
    cmd: list[str],
    *,
    progress_markers: tuple[str, ...] = (),
) -> tuple[int, dict[str, Any] | None, str, str]:
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ROOT),
    )
    stderr_lines: list[str] = []

    def relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if not progress_markers or any(marker in line for marker in progress_markers):
                sys.stderr.write(line)
                sys.stderr.flush()

    thread = threading.Thread(target=relay_stderr, daemon=True)
    thread.start()
    assert proc.stdout is not None
    stdout = proc.stdout.read()
    returncode = proc.wait()
    thread.join(timeout=1)
    stderr = "".join(stderr_lines)
    payload = None
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            payload = None
    return returncode, payload, stdout, stderr


# ---------------------------------------------------------------------------
# Nightly YAML parsing (reference-only, not an execution template)
# ---------------------------------------------------------------------------

@dataclass
class NightlyReference:
    """Parsed reference from a nightly YAML config.

    Fields may be None when the YAML does not define them.
    """
    name: str = ""
    model: str = ""
    envs: dict[str, str] = field(default_factory=dict)
    server_cmd: list[str] = field(default_factory=list)
    bench_config: dict[str, Any] = field(default_factory=dict)
    baseline: float | None = None
    threshold: float | None = None


def _try_yaml_import():
    try:
        import yaml  # noqa: F811
        return yaml
    except ImportError:
        return None


def parse_nightly_yaml(yaml_name: str) -> NightlyReference | None:
    """Parse a nightly config YAML as a reference source.

    Returns the first test case's config. Returns None if the file or
    required library is unavailable.
    """
    yaml_mod = _try_yaml_import()
    if yaml_mod is None:
        emit_progress("nightly", "PyYAML not available, skipping nightly reference")
        return None

    yaml_path = NIGHTLY_CONFIGS_DIR / yaml_name
    if not yaml_path.suffix:
        yaml_path = yaml_path.with_suffix(".yaml")
    if not yaml_path.exists():
        emit_progress("nightly", f"nightly config not found: {yaml_path.name}")
        return None

    with open(yaml_path, "r", encoding="utf-8") as f:
        data = yaml_mod.safe_load(f)

    cases = data.get("test_cases")
    if not cases:
        return None

    case = cases[0]
    ref = NightlyReference(name=case.get("name", yaml_name))
    ref.model = case.get("model", "")

    raw_envs = case.get("envs", {})
    ref.envs = {k: str(v) for k, v in raw_envs.items() if k != "SERVER_PORT"}

    cmd_parts = list(case.get("server_cmd", []))
    cmd_parts.extend(case.get("server_cmd_extra", []))
    ref.server_cmd = [str(s) for s in cmd_parts]

    benchmarks = case.get("benchmarks", {})
    perf = benchmarks.get("perf", {})
    if perf:
        ref.bench_config = {
            k: v for k, v in perf.items()
            if k not in ("case_type", "baseline", "threshold")
        }
        ref.baseline = perf.get("baseline")
        ref.threshold = perf.get("threshold")

    return ref


# ---------------------------------------------------------------------------
# Configuration assembly
# ---------------------------------------------------------------------------

@dataclass
class BenchConfig:
    """Assembled benchmark configuration ready for execution."""
    session_id: str | None = None
    session_file: str | None = None
    model: str = ""
    tp: int | None = None
    dp: int | None = None
    port: int | None = None
    serve_args: list[str] = field(default_factory=list)
    bench_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    skip_parity: bool = False
    nightly_ref: NightlyReference | None = None

    def to_serve_start_args(self) -> list[str]:
        """Build CLI args for serve_start.py."""
        args = ["--model", self.model]
        if self.session_file:
            args.extend(["--session-file", self.session_file])
        else:
            args.extend(["--session-id", self.session_id or ""])
        if self.tp is not None:
            args.extend(["--tp", str(self.tp)])
        if self.dp is not None:
            args.extend(["--dp", str(self.dp)])
        if self.port is not None:
            args.extend(["--port", str(self.port)])
        for k, v in self.env.items():
            args.extend(["--extra-env", f"{k}={v}"])
        if self.skip_parity:
            args.append("--skip-parity")
        if self.serve_args:
            args.append("--")
            args.extend(self.serve_args)
        return args

    def to_bench_serve_args(
        self, base_url: str, served_model_name: str,
    ) -> list[str]:
        """Build CLI args for `vllm bench serve`."""
        from urllib.parse import urlparse
        parsed = urlparse(base_url)
        host = parsed.hostname or "localhost"
        port = str(parsed.port or 8000)

        # Backend/endpoint are overridable via bench_args so completion-style
        # models (e.g. DSV4 with --backend openai / /v1/completions) work; the
        # chat default stays for the common case.
        has_backend = any(a == "--backend" or a.startswith("--backend=") for a in self.bench_args)
        has_endpoint = any(a == "--endpoint" or a.startswith("--endpoint=") for a in self.bench_args)

        args = ["vllm", "bench", "serve"]
        if not has_backend:
            args.extend(["--backend", "openai-chat"])
        if not has_endpoint:
            args.extend(["--endpoint", "/v1/chat/completions"])
        args.extend([
            "--host", host,
            "--port", port,
            "--model", served_model_name,
            "--tokenizer", self.model,
            "--save-result",
        ])
        has_num_prompts = any(a.startswith("--num-prompts") for a in self.bench_args)
        has_concurrency = any(a.startswith("--max-concurrency") for a in self.bench_args)

        if not has_num_prompts:
            args.extend(["--num-prompts", "64"])
        if not has_concurrency:
            args.extend(["--max-concurrency", "16"])

        args.extend(self.bench_args)
        return args

    def summary_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"model": self.model}
        if self.session_id:
            d["session_id"] = self.session_id
        if self.session_file:
            d["session_file"] = self.session_file
        if self.tp is not None:
            d["tp"] = self.tp
        if self.serve_args:
            d["serve_args"] = self.serve_args
        if self.bench_args:
            d["bench_args"] = self.bench_args
        if self.env:
            d["env"] = self.env
        return d


def assemble_config(
    *,
    session_id: str | None = None,
    session_file: str | None = None,
    model: str,
    tp: int | None = None,
    dp: int | None = None,
    port: int | None = None,
    serve_args: list[str] | None = None,
    bench_args: list[str] | None = None,
    extra_env: list[str] | None = None,
    refer_nightly: str | None = None,
    skip_parity: bool = False,
) -> BenchConfig:
    """Assemble a BenchConfig with user > nightly priority.

    Benchmarks are session-only. The session is resolved once here (including
    worktree-binding auto-resolution) and pinned into the config so every
    downstream subprocess targets the same session explicitly.
    """
    lookup = load_session_lookup(
        session_id=session_id,
        session_file=session_file,
        repo_root=ROOT,
    )
    nightly_ref: NightlyReference | None = None
    if refer_nightly:
        nightly_ref = parse_nightly_yaml(refer_nightly)

    cfg = BenchConfig(
        session_id=lookup.session["session_id"],
        session_file=str(lookup.session_file),
        model=model,
        skip_parity=skip_parity,
        nightly_ref=nightly_ref,
    )

    # --- TP ---
    if tp is not None:
        cfg.tp = tp
    elif nightly_ref and "--tensor-parallel-size" in nightly_ref.server_cmd:
        idx = nightly_ref.server_cmd.index("--tensor-parallel-size")
        if idx + 1 < len(nightly_ref.server_cmd):
            cfg.tp = int(nightly_ref.server_cmd[idx + 1])

    # --- DP ---
    if dp is not None:
        cfg.dp = dp
    elif nightly_ref and "--data-parallel-size" in nightly_ref.server_cmd:
        idx = nightly_ref.server_cmd.index("--data-parallel-size")
        if idx + 1 < len(nightly_ref.server_cmd):
            cfg.dp = int(nightly_ref.server_cmd[idx + 1])

    # --- Port ---
    cfg.port = port

    # --- Serve args: user provided overrides nightly ---
    if serve_args:
        cfg.serve_args = list(serve_args)
    elif nightly_ref:
        filtered = []
        skip_next = False
        for i, arg in enumerate(nightly_ref.server_cmd):
            if skip_next:
                skip_next = False
                continue
            if arg in ("--tensor-parallel-size", "--port"):
                skip_next = True
                continue
            filtered.append(arg)
        cfg.serve_args = filtered

    # --- Bench args: user provided overrides nightly ---
    if bench_args:
        cfg.bench_args = list(bench_args)
    elif nightly_ref and nightly_ref.bench_config:
        bc = nightly_ref.bench_config
        assembled: list[str] = []
        if "num_prompts" in bc:
            assembled.extend(["--num-prompts", str(bc["num_prompts"])])
        if "max_out_len" in bc:
            assembled.extend(["--output-len", str(bc["max_out_len"])])
        if "batch_size" in bc:
            assembled.extend(["--max-concurrency", str(bc["batch_size"])])
        cfg.bench_args = assembled

    # --- Env: merge nightly base + user overrides ---
    env: dict[str, str] = {}
    if nightly_ref:
        for key, value in nightly_ref.envs.items():
            env[require_env_name(key)] = value
    if extra_env:
        for item in extra_env:
            if "=" not in item:
                raise ValueError(f"bad --extra-env {item!r}, expected KEY=VALUE")
            k, v = item.split("=", 1)
            env[require_env_name(k)] = v
    cfg.env = env

    return cfg


# ---------------------------------------------------------------------------
# Serving skill wrappers
# ---------------------------------------------------------------------------

def call_serve_start(config: BenchConfig) -> dict[str, Any]:
    """Call serve_start.py and return its JSON output."""
    script = str(SERVING_SCRIPTS / "serve_start.py")
    cmd = [sys.executable, script] + config.to_serve_start_args()

    emit_progress("serve_start", f"starting service: {config.model}")
    returncode, data, stdout, stderr = _run_json_command_streaming(
        cmd,
        progress_markers=("__VAWS_SERVING_PROGRESS__=", "__VAWS_PARITY_PROGRESS__="),
    )

    if not stdout.strip():
        raise RuntimeError(
            f"serve_start.py produced no output (rc={returncode}):\n"
            f"{stderr[:2000]}"
        )
    if data is None:
        raise RuntimeError(
            f"serve_start.py output is not JSON (rc={returncode}):\n"
            f"stdout: {stdout[:1000]}\nstderr: {stderr[:1000]}"
        )
    return data


def call_serve_stop(config: BenchConfig, force: bool = False) -> dict[str, Any]:
    """Call serve_stop.py and return its JSON output."""
    script = str(SERVING_SCRIPTS / "serve_stop.py")
    cmd = [sys.executable, script]
    if config.session_file:
        cmd.extend(["--session-file", config.session_file])
    else:
        cmd.extend(["--session-id", config.session_id or ""])
    if force:
        cmd.append("--force")

    emit_progress("serve_stop", "stopping service")
    _returncode, data, stdout, _stderr = _run_json_command_streaming(cmd)
    if not stdout.strip():
        return {"status": "unknown", "message": "no output from serve_stop"}
    if data is None:
        return {"status": "unknown", "message": stdout[:500]}
    return data


# ---------------------------------------------------------------------------
# Remote benchmark execution
# ---------------------------------------------------------------------------

def _get_ssh_endpoint(
    *,
    session_id: str | None = None,
    session_file: str | None = None,
) -> tuple[str, int]:
    """Resolve the session container SSH host and port."""
    lookup = load_session_lookup(
        session_id=session_id,
        session_file=session_file,
        repo_root=ROOT,
    )
    remote = lookup.session["remote"]
    container = remote["container"]
    return remote["host"], int(container["ssh_port"])


def ssh_run_script(
    container_ip: str,
    container_port: int,
    script: str,
    *,
    timeout: int = 300,
) -> subprocess.CompletedProcess:
    """Run an arbitrary bash script inside the session container over SSH."""
    import shlex

    ssh_cmd = [
        "ssh",
        *base_ssh_options(),
        "-p", str(container_port),
        f"root@{container_ip}",
        "bash", "-c", shlex.quote(script),
    ]
    return subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=timeout)


def _git_ref_fetch_checkout(repo_dir: str, ref: str, branch: str) -> str:
    """Build a bash snippet that fetches (if needed) and checks out ``ref``.

    Supports ``pr:NNNN`` / ``#NNNN`` (GitHub PR head) and plain commit/branch
    refs. Source-only alignment: it resets and checks out, it never rebuilds
    custom ops (that stays the caller's / parity's decision).
    """
    import re as _re
    import shlex as _shlex

    lines = [
        f"cd {_shlex.quote(repo_dir)}",
        "git reset --hard >/dev/null 2>&1 || true",
    ]
    pr_match = _re.fullmatch(r"(?:pr:|#)?(\d+)", ref.strip())
    if pr_match:
        pr = pr_match.group(1)
        local_ref = f"refs/remotes/origin/pr-{pr}-head"
        lines.append(
            f"git fetch origin {_shlex.quote(f'pull/{pr}/head:{local_ref}')}"
        )
        checkout_ref = local_ref
    else:
        # Try a fetch so remote-only commits resolve, but tolerate offline.
        lines.append("git fetch origin --quiet || true")
        checkout_ref = ref
    lines.append(f"git rev-parse --verify {_shlex.quote(checkout_ref)} >/dev/null")
    lines.append(
        f"git checkout -B {_shlex.quote(branch)} {_shlex.quote(checkout_ref)} >/dev/null 2>&1"
    )
    lines.append("git rev-parse HEAD")
    return "\n".join(lines)


def remote_align_source(
    container_ip: str,
    container_port: int,
    *,
    vllm_ascend_ref: str | None = None,
    vllm_ref: str | None = None,
    vllm_ascend_dir: str = "/vllm-workspace/vllm-ascend",
    vllm_dir: str = "/vllm-workspace/vllm",
    timeout: int = 360,
) -> dict[str, Any]:
    """Align in-container vllm / vllm-ascend checkouts to given git refs.

    Source-only (no recompile), matching the common "对齐版本配套但不重编算子"
    workflow. Returns the resolved HEAD of each repo it touched.
    """
    blocks: list[str] = ["set -euo pipefail"]
    if vllm_ref:
        blocks.append('printf "vllm_head="')
        blocks.append(_git_ref_fetch_checkout(vllm_dir, vllm_ref, "vaws-bench-vllm"))
    if vllm_ascend_ref:
        blocks.append('printf "vllm_ascend_head="')
        blocks.append(
            _git_ref_fetch_checkout(vllm_ascend_dir, vllm_ascend_ref, "vaws-bench-vllm-ascend")
        )
    if len(blocks) == 1:
        return {"status": "ok", "message": "no refs requested"}
    proc = ssh_run_script(container_ip, container_port, "\n".join(blocks), timeout=timeout)
    heads: dict[str, str] = {}
    for line in (proc.stdout or "").splitlines():
        for key in ("vllm_head=", "vllm_ascend_head="):
            if line.startswith(key):
                heads[key.rstrip("=")] = line.split("=", 1)[1].strip()
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "heads": heads,
        "vllm_ascend_ref": vllm_ascend_ref,
        "vllm_ref": vllm_ref,
        "stdout_tail": (proc.stdout or "")[-800:],
        "stderr_tail": (proc.stderr or "")[-800:],
    }


def _ascend_env_preamble() -> str:
    """Shell preamble that sources the Ascend CANN environment."""
    return (
        "set -e; "
        "if [ -f /etc/profile.d/vaws-ascend-env.sh ]; then"
        "  set +u; source /etc/profile.d/vaws-ascend-env.sh; set -u;"
        " fi; "
        'export LD_LIBRARY_PATH='
        '"/usr/local/Ascend/driver/lib64/driver'
        ':/usr/local/Ascend/driver/lib64'
        '${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"; '
    )


def run_bench_on_remote(
    config: BenchConfig,
    base_url: str,
    served_model_name: str,
    container_ip: str,
    container_port: int,
) -> dict[str, Any]:
    """Run vllm bench serve on the remote container via SSH."""
    import shlex

    bench_cmd_parts = config.to_bench_serve_args(base_url, served_model_name)
    target_token = safe_token(config.session_id or "benchmark")
    result_filename = (
        f"result_bench_{target_token}_{now_utc().replace(':', '-')}_"
        f"{os.getpid()}_{uuid.uuid4().hex[:8]}.json"
    )
    bench_cmd_parts.extend(["--result-filename", result_filename])

    bench_cmd = " ".join(shlex.quote(str(s)) for s in bench_cmd_parts)

    remote_script = (
        _ascend_env_preamble()
        + f"cd /tmp && {bench_cmd} 2>&1 && cat /tmp/{result_filename}"
    )

    ssh_cmd = [
        "ssh",
        *base_ssh_options(),
        "-p", str(container_port),
        f"root@{container_ip}",
        "bash", "-c", shlex.quote(remote_script),
    ]

    emit_progress("bench_run", f"running vllm bench serve on {target_token}")
    proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=1200)

    if proc.returncode != 0:
        raise RuntimeError(
            f"vllm bench serve failed (rc={proc.returncode}):\n"
            f"stdout: {proc.stdout[-2000:]}\n"
            f"stderr: {proc.stderr[-2000:]}"
        )

    stdout = proc.stdout
    json_start = stdout.rfind("\n{")
    if json_start == -1:
        json_start = 0 if stdout.startswith("{") else -1
    else:
        json_start += 1

    if json_start == -1:
        raise RuntimeError(
            f"cannot find JSON result in bench output:\n{stdout[-2000:]}"
        )

    try:
        result_data = json.loads(stdout[json_start:])
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"cannot parse bench result JSON: {e}\n{stdout[json_start:json_start+500]}"
        )

    return result_data


# ---------------------------------------------------------------------------
# Metrics extraction
# ---------------------------------------------------------------------------

def extract_metrics(raw_result: dict[str, Any]) -> dict[str, Any]:
    """Extract key metrics from vllm bench serve result JSON."""
    metrics: dict[str, Any] = {}

    for key in ("output_throughput", "mean_tpot_ms", "mean_ttft_ms",
                "median_tpot_ms", "median_ttft_ms", "acceptance_rate",
                "spec_decode_acceptance_rate", "p99_tpot_ms", "p99_ttft_ms",
                "total_input", "total_output", "request_throughput",
                "mean_e2el_ms", "median_e2el_ms"):
        if key in raw_result:
            val = raw_result[key]
            if isinstance(val, str):
                try:
                    val = float(val)
                except ValueError:
                    pass
            metrics[key] = val

    return metrics


# ---------------------------------------------------------------------------
# Safe stale-process cleanup
# ---------------------------------------------------------------------------

# Regex (POSIX ERE) for the vLLM runtime child process *comm* names that are
# safe to reap. Deliberately narrow: only vLLM's own worker/engine helper
# processes. NEVER match sshd, bash, the session shell, or PID 1.
_VLLM_STALE_COMM_RE = r"^(VLLM::EngineCor|VLLM::Worker|VLLMWorker|VLLM::Core)"


def safe_stale_cleanup(
    container_ip: str,
    container_port: int,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Kill orphaned vLLM EngineCore/Worker processes inside a session container.

    This is the SAFE replacement for the ad-hoc cleanup that once SIGTERM'd a
    session's dedicated sshd (``Exiting on signal 15``), dropping the container
    SSH port and forcing a rebuild. Hard safety rules enforced remotely:

      * skip PID 1 (container init);
      * only match vLLM runtime child *comm* names (``VLLM::EngineCore`` /
        ``VLLM::Worker`` / ``VLLMWorker``) -- never sshd/bash/session shells;
      * additionally exclude any pid whose full cmdline mentions ``sshd`` or
        ``vaws`` (dedicated session sshd, remote-dev helpers);
      * kill only explicit pids (never a process group / negative pid), so a
        signal can never fan out to the session sshd.

    Returns a summary with the pids matched and (unless ``dry_run``) reaped.
    """
    import shlex

    action = "echo DRY_RUN_SKIP_KILL" if dry_run else "kill_pids"
    remote_script = r'''
set +e
match_re='%s'
mapfile -t pids < <(ps -eo pid=,comm=,args= | awk -v re="$match_re" '
  $1 == 1 {next}
  {
    comm=$2
    if (comm ~ re) {
      # exclude anything that is (or wraps) sshd / vaws helpers
      if ($0 ~ /sshd/ || $0 ~ /vaws/) next
      print $1
    }
  }
')
printf "matched_pids=%%s\n" "${pids[*]:-}"
kill_pids() {
  [ ${#pids[@]} -eq 0 ] && return 0
  kill -TERM "${pids[@]}" 2>/dev/null || true
  sleep 3
  # re-check and SIGKILL survivors (still only explicit pids)
  survivors=()
  for p in "${pids[@]}"; do
    if kill -0 "$p" 2>/dev/null; then survivors+=("$p"); fi
  done
  if [ ${#survivors[@]} -gt 0 ]; then
    kill -KILL "${survivors[@]}" 2>/dev/null || true
  fi
}
%s
sleep 1
printf "remaining=%%s\n" "$(ps -eo pid=,comm= | awk -v re="$match_re" '$1!=1 && $2 ~ re {print $1}' | tr "\n" " ")"
exit 0
''' % (_VLLM_STALE_COMM_RE, action)

    ssh_cmd = [
        "ssh",
        *base_ssh_options(),
        "-p", str(container_port),
        f"root@{container_ip}",
        "bash", "-c", shlex.quote(remote_script),
    ]
    proc = subprocess.run(ssh_cmd, capture_output=True, text=True, timeout=120)
    out = proc.stdout or ""
    matched = ""
    remaining = ""
    for line in out.splitlines():
        if line.startswith("matched_pids="):
            matched = line.split("=", 1)[1].strip()
        elif line.startswith("remaining="):
            remaining = line.split("=", 1)[1].strip()
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "dry_run": dry_run,
        "matched_pids": matched.split() if matched else [],
        "remaining_pids": remaining.split() if remaining else [],
        "returncode": proc.returncode,
        "stderr_tail": (proc.stderr or "")[-400:],
    }
