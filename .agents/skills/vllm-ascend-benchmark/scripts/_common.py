#!/usr/bin/env python3
"""Shared utilities for vllm-ascend-benchmark scripts."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]

SERVING_SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
NIGHTLY_CONFIGS_DIR = (
    ROOT / "vllm-ascend" / "tests" / "e2e" / "nightly"
    / "single_node" / "models" / "configs"
)
BENCHMARK_STATE_DIR = ROOT / ".vaws-local" / "benchmark"
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
    machine: str = ""
    model: str = ""
    tp: int | None = None
    port: int | None = None
    serve_args: list[str] = field(default_factory=list)
    bench_args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    skip_parity: bool = False
    nightly_ref: NightlyReference | None = None

    def to_serve_start_args(self) -> list[str]:
        """Build CLI args for serve_start.py."""
        args = ["--machine", self.machine, "--model", self.model]
        if self.tp is not None:
            args.extend(["--tp", str(self.tp)])
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

        args = [
            "vllm", "bench", "serve",
            "--backend", "openai-chat",
            "--endpoint", "/v1/chat/completions",
            "--host", host,
            "--port", port,
            "--model", served_model_name,
            "--tokenizer", self.model,
            "--save-result",
        ]
        has_num_prompts = any(a.startswith("--num-prompts") for a in self.bench_args)
        has_concurrency = any(a.startswith("--max-concurrency") for a in self.bench_args)

        if not has_num_prompts:
            args.extend(["--num-prompts", "64"])
        if not has_concurrency:
            args.extend(["--max-concurrency", "16"])

        args.extend(self.bench_args)
        return args

    def summary_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"machine": self.machine, "model": self.model}
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
    machine: str,
    model: str,
    tp: int | None = None,
    port: int | None = None,
    serve_args: list[str] | None = None,
    bench_args: list[str] | None = None,
    extra_env: list[str] | None = None,
    refer_nightly: str | None = None,
    skip_parity: bool = False,
) -> BenchConfig:
    """Assemble a BenchConfig with user > nightly priority."""
    nightly_ref: NightlyReference | None = None
    if refer_nightly:
        nightly_ref = parse_nightly_yaml(refer_nightly)

    cfg = BenchConfig(
        machine=machine,
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
        env.update(nightly_ref.envs)
    if extra_env:
        for item in extra_env:
            if "=" in item:
                k, v = item.split("=", 1)
                env[k] = v
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
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))

    for line in result.stderr.splitlines():
        if "__VAWS_SERVING_PROGRESS__=" in line or "__VAWS_PARITY_PROGRESS__=" in line:
            sys.stderr.write(line + "\n")

    if not result.stdout.strip():
        raise RuntimeError(
            f"serve_start.py produced no output (rc={result.returncode}):\n"
            f"{result.stderr[:2000]}"
        )
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(
            f"serve_start.py output is not JSON (rc={result.returncode}):\n"
            f"stdout: {result.stdout[:1000]}\nstderr: {result.stderr[:1000]}"
        )
    return data


def call_serve_stop(machine: str, force: bool = False) -> dict[str, Any]:
    """Call serve_stop.py and return its JSON output."""
    script = str(SERVING_SCRIPTS / "serve_stop.py")
    cmd = [sys.executable, script, "--machine", machine]
    if force:
        cmd.append("--force")

    emit_progress("serve_stop", "stopping service")
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if not result.stdout.strip():
        return {"status": "unknown", "message": "no output from serve_stop"}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {"status": "unknown", "message": result.stdout[:500]}


# ---------------------------------------------------------------------------
# Remote benchmark execution
# ---------------------------------------------------------------------------

def _get_ssh_endpoint(machine: str) -> tuple[str, int]:
    """Resolve container SSH host and port from inventory."""
    lib_dir = str(ROOT / ".agents" / "lib")
    mm_dir = str(ROOT / ".agents" / "skills" / "machine-management" / "scripts")
    for p in (lib_dir, mm_dir):
        if p not in sys.path:
            sys.path.insert(0, p)
    import inventory as inv_store
    read_path = inv_store.read_inventory_path(
        inv_store.preferred_inventory_path(inv_store.DEFAULT_PATH)
    )
    inv = inv_store.load_inventory(read_path)
    matches = inv_store._find_matches(inv, identifier=machine)
    if not matches:
        raise RuntimeError(f"machine {machine!r} not found in inventory")
    rec = matches[0]
    return rec["host"]["ip"], rec["container"]["ssh_port"]


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
    result_filename = f"result_bench_{now_utc().replace(':', '-')}.json"
    bench_cmd_parts.extend(["--result-filename", result_filename])

    bench_cmd = " ".join(shlex.quote(str(s)) for s in bench_cmd_parts)

    remote_script = (
        _ascend_env_preamble()
        + f"cd /tmp && {bench_cmd} 2>&1 && cat /tmp/{result_filename}"
    )

    ssh_cmd = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "LogLevel=ERROR",
        "-p", str(container_port),
        f"root@{container_ip}",
        "bash", "-c", shlex.quote(remote_script),
    ]

    emit_progress("bench_run", f"running vllm bench serve on {config.machine}")
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


def compute_delta(baseline: dict[str, Any], patched: dict[str, Any]) -> dict[str, str]:
    """Compute percentage delta between baseline and patched metrics."""
    delta: dict[str, str] = {}
    for key in baseline:
        if key not in patched:
            continue
        try:
            bv = float(baseline[key])
            pv = float(patched[key])
        except (TypeError, ValueError):
            continue
        if bv == 0:
            delta[key] = "N/A (baseline=0)"
            continue
        pct = (pv - bv) / abs(bv) * 100
        sign = "+" if pct >= 0 else ""
        delta[key] = f"{sign}{pct:.2f}%"
    return delta


def check_regression(
    baseline_metrics: dict[str, Any],
    patched_metrics: dict[str, Any],
    threshold: float = 0.97,
) -> bool:
    """Return True if patched throughput regressed below threshold."""
    bv = baseline_metrics.get("output_throughput")
    pv = patched_metrics.get("output_throughput")
    if bv is None or pv is None:
        return False
    try:
        return float(pv) < float(bv) * threshold
    except (TypeError, ValueError):
        return False
