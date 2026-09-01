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
PRESETS_DIR = ROOT / ".agents" / "skills" / "vllm-ascend-benchmark" / "presets"
PROGRESS_SENTINEL = "__VAWS_BENCHMARK_PROGRESS__="

# Mirrors serve_start.py's DEFAULT_HEALTH_TIMEOUT; used to bound the
# serve_start subprocess when the config does not pin an explicit timeout.
_SERVE_START_DEFAULT_HEALTH_TIMEOUT = 300
# Subprocess budget beyond the readiness wait: covers parity sync, remote
# process spawn and the final state/JSON write after the service is ready.
_SERVE_START_TIMEOUT_MARGIN = 300


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
    timeout: float | None = None,
) -> tuple[int, dict[str, Any] | None, str, str]:
    """Run a JSON-emitting subprocess, relaying matching stderr lines live.

    When ``timeout`` (seconds) is set, a watchdog kills the child once it
    elapses; the call then returns returncode 124 with a timeout note appended
    to stderr and no parsed payload (partial stdout is not trusted).
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=str(ROOT),
    )
    stderr_lines: list[str] = []
    timed_out = False

    def relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            if not progress_markers or any(marker in line for marker in progress_markers):
                sys.stderr.write(line)
                sys.stderr.flush()

    def kill_on_timeout() -> None:
        nonlocal timed_out
        timed_out = True
        proc.kill()

    thread = threading.Thread(target=relay_stderr, daemon=True)
    thread.start()
    watchdog = threading.Timer(timeout, kill_on_timeout) if timeout is not None else None
    if watchdog is not None:
        watchdog.daemon = True
        watchdog.start()
    assert proc.stdout is not None
    stdout = proc.stdout.read()
    returncode = proc.wait()
    if watchdog is not None:
        watchdog.cancel()
    thread.join(timeout=1)
    stderr = "".join(stderr_lines)
    if timed_out:
        returncode = 124
        stderr += (
            f"\ncommand timed out after {timeout}s and was killed: "
            f"{' '.join(str(c) for c in cmd[:3])} ...\n"
        )
        return returncode, None, stdout, stderr
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
# Benchmark presets
# ---------------------------------------------------------------------------

def load_preset(name: str) -> dict[str, Any]:
    """Load a named benchmark preset from the skill's ``presets/`` directory.

    ``name`` is a bare preset name (the ``.json`` suffix is optional); path
    traversal is rejected. Raises ``ValueError`` for unknown presets or
    malformed preset files.
    """
    stem = name[:-5] if name.endswith(".json") else name
    if not stem or "/" in stem or "\\" in stem or ".." in stem:
        raise ValueError(f"invalid preset name {name!r}: use a bare preset name")
    path = PRESETS_DIR / f"{stem}.json"
    if not path.is_file():
        available = (
            sorted(p.stem for p in PRESETS_DIR.glob("*.json"))
            if PRESETS_DIR.is_dir()
            else []
        )
        raise ValueError(
            f"unknown preset {name!r}; available presets: "
            + (", ".join(available) if available else "(none)")
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"preset {name!r} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError(f"preset {name!r} must contain a JSON object")
    return data


def _preset_list(data: dict[str, Any], key: str) -> list[str] | None:
    """Read a preset key as a list of strings, or None when absent."""
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError(f"preset key {key!r} must be an array of strings")
    return [str(item) for item in value]


def _preset_env(data: dict[str, Any], key: str) -> dict[str, str]:
    """Read a preset key as an env dict with validated variable names."""
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"preset key {key!r} must be an object of KEY: VALUE strings")
    return {require_env_name(str(k)): str(v) for k, v in value.items()}


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
    served_model_name: str = ""
    devices: str | None = None
    health_timeout: int | None = None
    serve_args: list[str] = field(default_factory=list)
    bench_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    bench_env: dict[str, str] = field(default_factory=dict)
    skip_parity: bool = False
    nightly_ref: NightlyReference | None = None
    preset_name: str | None = None
    preset: dict[str, Any] | None = None

    def to_serve_start_args(self) -> list[str]:
        """Build CLI args for serve_start.py."""
        args = ["--model", self.model]
        if self.session_file:
            args.extend(["--session-file", self.session_file])
        else:
            args.extend(["--session-id", self.session_id or ""])
        if self.served_model_name:
            args.extend(["--served-model-name", self.served_model_name])
        if self.tp is not None:
            args.extend(["--tp", str(self.tp)])
        if self.dp is not None:
            args.extend(["--dp", str(self.dp)])
        if self.port is not None:
            args.extend(["--port", str(self.port)])
        if self.devices:
            args.extend(["--devices", self.devices])
        if self.health_timeout is not None:
            args.extend(["--health-timeout", str(self.health_timeout)])
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
        if self.preset_name:
            d["preset"] = self.preset_name
        if self.tp is not None:
            d["tp"] = self.tp
        if self.served_model_name:
            d["served_model_name"] = self.served_model_name
        if self.devices:
            d["devices"] = self.devices
        if self.health_timeout is not None:
            d["health_timeout"] = self.health_timeout
        if self.serve_args:
            d["serve_args"] = self.serve_args
        if self.bench_args:
            d["bench_args"] = self.bench_args
        if self.env:
            d["env"] = self.env
        if self.bench_env:
            d["bench_env"] = self.bench_env
        return d


def assemble_config(
    *,
    session_id: str | None = None,
    session_file: str | None = None,
    model: str,
    tp: int | None = None,
    dp: int | None = None,
    port: int | None = None,
    served_model_name: str | None = None,
    devices: str | None = None,
    health_timeout: int | None = None,
    serve_args: list[str] | None = None,
    bench_args: list[str] | None = None,
    extra_env: list[str] | None = None,
    bench_env: list[str] | None = None,
    refer_nightly: str | None = None,
    preset: str | None = None,
    skip_parity: bool = False,
) -> BenchConfig:
    """Assemble a BenchConfig with CLI > preset > nightly > default priority.

    Benchmarks are session-only. The session is resolved once here (including
    worktree-binding auto-resolution) and pinned into the config so every
    downstream subprocess targets the same session explicitly.

    ``preset`` names a JSON file under the skill's ``presets/`` directory.
    Recognized preset keys: ``tp``, ``dp``, ``port``, ``devices``,
    ``served_model_name``, ``health_timeout``, ``vllm_ref``, ``runs``,
    ``warmup_runs``, ``env`` (object), ``bench_env`` (object), ``serve_args``
    (array), ``bench_args`` (array), ``fixed_request_dataset`` (object) and
    ``bench_request_counts`` (array of int). The resolved preset dict is kept
    on ``cfg.preset`` so callers can read non-config keys (``vllm_ref``,
    ``fixed_request_dataset``, ``bench_request_counts``, ``runs``,
    ``warmup_runs``) from it.
    """
    lookup = load_session_lookup(
        session_id=session_id,
        session_file=session_file,
        repo_root=ROOT,
    )
    nightly_ref: NightlyReference | None = None
    if refer_nightly:
        nightly_ref = parse_nightly_yaml(refer_nightly)

    preset_dict: dict[str, Any] | None = None
    if preset:
        preset_dict = load_preset(preset)

    cfg = BenchConfig(
        session_id=lookup.session["session_id"],
        session_file=str(lookup.session_file),
        model=model,
        skip_parity=skip_parity,
        nightly_ref=nightly_ref,
        preset_name=preset,
        preset=preset_dict,
    )

    # --- TP ---
    if tp is not None:
        cfg.tp = tp
    elif preset_dict and preset_dict.get("tp") is not None:
        cfg.tp = int(preset_dict["tp"])
    elif nightly_ref and "--tensor-parallel-size" in nightly_ref.server_cmd:
        idx = nightly_ref.server_cmd.index("--tensor-parallel-size")
        if idx + 1 < len(nightly_ref.server_cmd):
            cfg.tp = int(nightly_ref.server_cmd[idx + 1])

    # --- DP ---
    if dp is not None:
        cfg.dp = dp
    elif preset_dict and preset_dict.get("dp") is not None:
        cfg.dp = int(preset_dict["dp"])
    elif nightly_ref and "--data-parallel-size" in nightly_ref.server_cmd:
        idx = nightly_ref.server_cmd.index("--data-parallel-size")
        if idx + 1 < len(nightly_ref.server_cmd):
            cfg.dp = int(nightly_ref.server_cmd[idx + 1])

    # --- Port ---
    if port is not None:
        cfg.port = port
    elif preset_dict and preset_dict.get("port") is not None:
        cfg.port = int(preset_dict["port"])

    # --- Served model name ---
    if served_model_name:
        cfg.served_model_name = served_model_name
    elif preset_dict and preset_dict.get("served_model_name"):
        cfg.served_model_name = str(preset_dict["served_model_name"])

    # --- Devices ---
    if devices:
        cfg.devices = devices
    elif preset_dict and preset_dict.get("devices"):
        cfg.devices = str(preset_dict["devices"])

    # --- Health timeout ---
    if health_timeout is not None:
        cfg.health_timeout = health_timeout
    elif preset_dict and preset_dict.get("health_timeout") is not None:
        cfg.health_timeout = int(preset_dict["health_timeout"])

    # --- Serve args: user provided overrides preset, preset overrides nightly ---
    if serve_args:
        cfg.serve_args = list(serve_args)
    elif preset_dict and _preset_list(preset_dict, "serve_args") is not None:
        cfg.serve_args = _preset_list(preset_dict, "serve_args") or []
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

    # --- Bench args: user provided overrides preset, preset overrides nightly ---
    if bench_args:
        cfg.bench_args = list(bench_args)
    elif preset_dict and _preset_list(preset_dict, "bench_args") is not None:
        cfg.bench_args = _preset_list(preset_dict, "bench_args") or []
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

    # --- Service env: nightly base, preset overrides, user wins ---
    env: dict[str, str] = {}
    if nightly_ref:
        for key, value in nightly_ref.envs.items():
            env[require_env_name(key)] = value
    if preset_dict:
        env.update(_preset_env(preset_dict, "env"))
    if extra_env:
        for item in extra_env:
            if "=" not in item:
                raise ValueError(f"bad --extra-env {item!r}, expected KEY=VALUE")
            k, v = item.split("=", 1)
            env[require_env_name(k)] = v
    cfg.env = env

    # --- Bench-side env: preset base, user wins ---
    benv: dict[str, str] = {}
    if preset_dict:
        benv.update(_preset_env(preset_dict, "bench_env"))
    if bench_env:
        for item in bench_env:
            if "=" not in item:
                raise ValueError(f"bad --bench-env {item!r}, expected KEY=VALUE")
            k, v = item.split("=", 1)
            benv[require_env_name(k)] = v
    cfg.bench_env = benv

    return cfg


# ---------------------------------------------------------------------------
# Serving skill wrappers
# ---------------------------------------------------------------------------

def call_serve_start(config: BenchConfig) -> dict[str, Any]:
    """Call serve_start.py and return its JSON output.

    The subprocess is bounded by the effective health timeout (config value,
    else serve_start.py's default) plus ``_SERVE_START_TIMEOUT_MARGIN`` for
    parity sync, remote spawn and the final state write; on timeout the
    raised error carries the watchdog note from ``_run_json_command_streaming``.
    """
    script = str(SERVING_SCRIPTS / "serve_start.py")
    cmd = [sys.executable, script] + config.to_serve_start_args()

    health_timeout = (
        config.health_timeout
        if config.health_timeout is not None
        else _SERVE_START_DEFAULT_HEALTH_TIMEOUT
    )
    emit_progress("serve_start", f"starting service: {config.model}")
    returncode, data, stdout, stderr = _run_json_command_streaming(
        cmd,
        progress_markers=("__VAWS_SERVING_PROGRESS__=", "__VAWS_PARITY_PROGRESS__="),
        timeout=health_timeout + _SERVE_START_TIMEOUT_MARGIN,
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

    # Bench-side env exports (e.g. PYTHONPATH/VLLM_VERSION) run after the
    # Ascend preamble so they win over anything the preamble sets.
    env_exports = "".join(
        f"export {require_env_name(k)}={shlex.quote(v)}; "
        for k, v in config.bench_env.items()
    )

    remote_script = (
        _ascend_env_preamble()
        + env_exports
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
# Remote inspection / patching / dataset / probe helpers
# ---------------------------------------------------------------------------

def remote_native_input_digest(
    container_ip: str,
    container_port: int,
    repo_dir: str = "/vllm-workspace/vllm-ascend",
) -> dict[str, Any]:
    """Fingerprint the in-container vllm-ascend native-build inputs.

    Source alignment never rebuilds custom ops, so when csrc/cmake/
    requirements change between states the compiled artifacts are stale.
    The digest covers ``csrc``, ``cmake``, ``CMakeLists.txt``,
    ``pyproject.toml`` and the requirements files — both tracked files and
    untracked-but-not-ignored ones (``git ls-files --others
    --exclude-standard``), so an uncommitted new source file still changes
    the fingerprint while ignored build artifacts (``build/`` etc.) stay
    excluded. Missing paths are tolerated: the digest is the sha256 of an
    empty file list only when there are genuinely no native inputs.
    """
    import shlex

    native_paths = "csrc cmake CMakeLists.txt pyproject.toml requirements.txt requirements"
    remote_script = "\n".join([
        "set -uo pipefail",
        f"cd {shlex.quote(repo_dir)} || exit 1",
        f"digest=$({{ git ls-files -z -- {native_paths};"
        f" git ls-files -z --others --exclude-standard -- {native_paths}; }}"
        " 2>/dev/null | sort -z"
        " | xargs -0 -r sha256sum 2>/dev/null | sha256sum | awk '{print $1}')",
        "head=$(git rev-parse HEAD 2>/dev/null || true)",
        'printf "digest=%s\\nhead=%s\\n" "${digest:-}" "${head:-}"',
    ])
    proc = ssh_run_script(container_ip, container_port, remote_script, timeout=120)
    digest = ""
    head = ""
    for line in (proc.stdout or "").splitlines():
        if line.startswith("digest="):
            digest = line.split("=", 1)[1].strip()
        elif line.startswith("head="):
            head = line.split("=", 1)[1].strip()
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "digest": digest,
        "head": head,
        "returncode": proc.returncode,
        "stderr_tail": (proc.stderr or "")[-400:],
    }


def apply_remote_patch(
    container_ip: str,
    container_port: int,
    patch_file: Path,
    repo_dir: str = "/vllm-workspace/vllm-ascend",
) -> dict[str, Any]:
    """git-apply a local patch file inside the remote repo checkout.

    The patch is base64-transferred through the SSH channel, applied with
    ``git apply``, and the resulting ``git status --short`` is returned for
    traceability.
    """
    import base64
    import shlex

    patch_b64 = base64.b64encode(Path(patch_file).read_bytes()).decode("ascii")
    remote_script = (
        "set -euo pipefail\n"
        "python3 - <<'PY'\n"
        "import base64\n"
        f"data = {patch_b64!r}\n"
        "open('/tmp/vaws_bench_remote_patch.diff', 'wb').write(base64.b64decode(data))\n"
        "PY\n"
        f"cd {shlex.quote(repo_dir)}\n"
        "git apply /tmp/vaws_bench_remote_patch.diff\n"
        "git status --short\n"
    )
    proc = ssh_run_script(container_ip, container_port, remote_script, timeout=120)
    return {
        "status": "ok" if proc.returncode == 0 else "failed",
        "returncode": proc.returncode,
        "repo_dir": repo_dir,
        "patch_file": str(patch_file),
        "stdout_tail": (proc.stdout or "")[-2000:],
        "stderr_tail": (proc.stderr or "")[-2000:],
    }


_FIXED_DATASET_REMOTE_PY = r'''
import json
import os
from pathlib import Path

from vllm.tokenizers import get_tokenizer

model = os.environ["VAWS_FIXED_MODEL"]
tokenizer_mode = os.environ["VAWS_FIXED_TOKENIZER_MODE"]
target_len = int(os.environ["VAWS_FIXED_INPUT_LEN"])
output_len = int(os.environ["VAWS_FIXED_OUTPUT_LEN"])
num_rows = int(os.environ["VAWS_FIXED_NUM_ROWS"])
dataset_path = Path(os.environ["VAWS_FIXED_DATASET_PATH"])
prompt = os.environ.get("VAWS_FIXED_PROMPT") or ""

tokenizer = get_tokenizer(model, tokenizer_mode=tokenizer_mode)

def token_len(text: str) -> int:
    return len(tokenizer(text).input_ids)

if prompt:
    actual_len = token_len(prompt)
    if actual_len != target_len:
        raise SystemExit(
            f"fixed prompt token length mismatch: got {actual_len}, expected {target_len}"
        )
else:
    prompt = ""
    pieces = [
        " token",
        " data",
        " request",
        " benchmark",
        " inference",
        " model",
        " a",
        "x",
    ]
    for piece in pieces:
        for repeat in range(1, target_len * 4 + 256):
            candidate = piece * repeat
            if token_len(candidate) == target_len:
                prompt = candidate
                break
        if prompt:
            break

    if not prompt:
        vocab_size = getattr(tokenizer, "vocab_size", None) or len(tokenizer)
        for token_id in range(100, min(int(vocab_size), 50000)):
            piece = tokenizer.decode([token_id], skip_special_tokens=True)
            if not piece or token_len(piece) != 1:
                continue
            candidate = piece * target_len
            if token_len(candidate) == target_len:
                prompt = candidate
                break

    if not prompt:
        raise SystemExit(f"failed to construct {target_len}-token fixed prompt")

actual_len = token_len(prompt)
rows = [
    json.dumps(
        {"prompt": prompt, "output_tokens": output_len},
        ensure_ascii=False,
    )
    for _ in range(num_rows)
]
dataset_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
print(json.dumps({
    "status": "ok",
    "dataset_path": str(dataset_path),
    "num_rows": num_rows,
    "prompt_token_len": actual_len,
    "output_len": output_len,
    "prompt_sha256": __import__("hashlib").sha256(prompt.encode("utf-8")).hexdigest(),
}, ensure_ascii=False))
'''.strip()


def prepare_fixed_request_dataset(
    container_ip: str,
    container_port: int,
    *,
    model: str,
    tokenizer_mode: str,
    input_len: int,
    output_len: int,
    path: str,
    num_rows: int,
    prompt: str | None = None,
    env_preamble: str = "",
) -> dict[str, Any]:
    """Generate a custom JSONL dataset of identical fixed-length requests.

    Runs on the remote container (it needs the model tokenizer). Every row is
    ``{"prompt": ..., "output_tokens": output_len}`` and the prompt is built
    to tokenize to exactly ``input_len`` tokens. Hard failures (token-count
    mismatch, no constructible prompt) raise ``RuntimeError``.

    ``env_preamble`` carries extra caller-supplied shell exports (e.g.
    PYTHONPATH from the resolved bench_env) appended after the Ascend CANN
    preamble so the vllm tokenizer imports from the aligned checkout.
    """
    import shlex

    exports = [
        f"export VAWS_FIXED_MODEL={shlex.quote(model)}",
        f"export VAWS_FIXED_TOKENIZER_MODE={shlex.quote(tokenizer_mode)}",
        f"export VAWS_FIXED_INPUT_LEN={int(input_len)}",
        f"export VAWS_FIXED_OUTPUT_LEN={int(output_len)}",
        f"export VAWS_FIXED_NUM_ROWS={int(num_rows)}",
        f"export VAWS_FIXED_DATASET_PATH={shlex.quote(path)}",
    ]
    if prompt:
        exports.append(f"export VAWS_FIXED_PROMPT={shlex.quote(prompt)}")

    remote_script = (
        _ascend_env_preamble()
        + (env_preamble or "")
        + "; ".join(exports)
        + "; python3 - <<'PY'\n"
        + _FIXED_DATASET_REMOTE_PY
        + "\nPY\n"
    )
    proc = ssh_run_script(container_ip, container_port, remote_script, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(
            "fixed request dataset preparation failed "
            f"(rc={proc.returncode}):\n"
            f"stdout: {(proc.stdout or '')[-2000:]}\n"
            f"stderr: {(proc.stderr or '')[-2000:]}"
        )
    payload = _last_json_line(proc.stdout or "")
    if payload is None:
        raise RuntimeError(
            "fixed request dataset preparation returned no JSON:\n"
            f"{(proc.stdout or '')[-2000:]}"
        )
    return payload


_ACCURACY_PROBE_REMOTE_PY = r'''
import hashlib
import json
import os
import urllib.error
import urllib.request

payload = json.loads(os.environ["VAWS_BENCH_ACCURACY_PAYLOAD"])
url = os.environ["VAWS_BENCH_ACCURACY_URL"]
data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    url,
    data=data,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=300) as resp:
        body = resp.read().decode("utf-8")
except urllib.error.HTTPError as exc:
    body = exc.read().decode("utf-8", errors="replace")
    print(json.dumps({"status": "failed", "http_status": exc.code, "body": body[:2000]}, ensure_ascii=False))
    raise SystemExit(0)

parsed = json.loads(body)
choice = (parsed.get("choices") or [{}])[0]
message = choice.get("message") or {}
text = choice.get("text") or message.get("content") or ""
out = {
    "status": "ok",
    "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    "text": text,
    "finish_reason": choice.get("finish_reason"),
    "usage": parsed.get("usage"),
}
print(json.dumps(out, ensure_ascii=False))
'''.strip()


def run_accuracy_probe(
    container_ip: str,
    container_port: int,
    *,
    port: int,
    model: str,
    prompt: str,
    max_tokens: int,
) -> dict[str, Any]:
    """POST one deterministic completion request and hash the returned text.

    Temperature is pinned to 0 so the text is comparable across states. An
    HTTP error from the service is reported as ``{"status": "failed",
    "http_status": ..., "body": ...}`` without raising; transport-level
    failures (SSH/remote python) still raise ``RuntimeError``.
    """
    import hashlib
    import shlex

    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": int(max_tokens),
        "temperature": 0.0,
        "stream": False,
    }
    remote_script = (
        f"export VAWS_BENCH_ACCURACY_URL={shlex.quote(f'http://127.0.0.1:{port}/v1/completions')}\n"
        f"export VAWS_BENCH_ACCURACY_PAYLOAD={shlex.quote(json.dumps(payload, ensure_ascii=False))}\n"
        "python3 - <<'PY'\n"
        + _ACCURACY_PROBE_REMOTE_PY
        + "\nPY\n"
    )
    proc = ssh_run_script(container_ip, container_port, remote_script, timeout=420)
    if proc.returncode != 0:
        raise RuntimeError(
            f"accuracy probe failed (rc={proc.returncode}):\n"
            f"stdout: {(proc.stdout or '')[-2000:]}\n"
            f"stderr: {(proc.stderr or '')[-2000:]}"
        )
    probe = _last_json_line(proc.stdout or "")
    if probe is None:
        raise RuntimeError(
            f"accuracy probe returned no JSON:\n{(proc.stdout or '')[-2000:]}"
        )
    probe["prompt_sha256"] = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    return probe


def _last_json_line(stdout: str) -> dict[str, Any] | None:
    """Parse the last ``{...}`` line of remote stdout as a JSON object."""
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None


# ---------------------------------------------------------------------------
# Metrics extraction
# ---------------------------------------------------------------------------

def extract_metrics(raw_result: dict[str, Any]) -> dict[str, Any]:
    """Extract key metrics from vllm bench serve result JSON.

    Keys mirror the real ``--save-result`` output (vllm/benchmarks/serve.py:
    the result dict plus the spec-decode and percentile-metric blocks). Note
    the totals are ``total_input_tokens`` / ``total_output_tokens`` and the
    spec-decode acceptance metric is ``spec_decode_acceptance_rate``; a bare
    ``acceptance_rate`` key has never existed in the result JSON.
    """
    metrics: dict[str, Any] = {}

    for key in ("output_throughput", "total_token_throughput", "mean_tpot_ms",
                "mean_ttft_ms", "median_tpot_ms", "median_ttft_ms",
                "spec_decode_acceptance_rate", "p99_tpot_ms", "p99_ttft_ms",
                "total_input_tokens", "total_output_tokens", "request_throughput",
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
