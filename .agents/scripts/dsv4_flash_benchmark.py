#!/usr/bin/env python3
"""Run fixed DeepSeek-V4-Flash 512/512 benchmark states on a VAWS session.

This script intentionally keeps the DSV4 Flash launch and benchmark arguments
in one place. Switching between baseline and PR states should only change the
git ref passed through --state, not the service parameters.

Examples:

  python3 .agents/scripts/dsv4_flash_benchmark.py \
      --state baseline=682cc2b938446b73a67e9ddfcc5ca2203a3f8088 \
      --state pr10805=pr:10805

  python3 .agents/scripts/dsv4_flash_benchmark.py --state pr10806=pr:10806
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shlex
import statistics
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
SERVING_DIR = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
BENCHMARK_DIR = ROOT / ".agents" / "skills" / "vllm-ascend-benchmark" / "scripts"
REMOTE_EXEC = ROOT / ".agents" / "scripts" / "remote_exec.py"

if str(BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(BENCHMARK_DIR))

import _common as bench_common  # noqa: E402


DEFAULT_SESSION_ID = "dsv4-w4a8-main-125"
DEFAULT_MODEL = "/home/weights/DeepSeek-V4-Flash-w4a8-mtp"
DEFAULT_SERVED_MODEL_NAME = "dsv4-w4a8"
DEFAULT_PORT = 30001
DEFAULT_DEVICES = "0,1,2,3,4,5,6,7"
DEFAULT_VLLM_REF = "967c5c3bc38891f4465d3f4e99917ed837bb3833"
DEFAULT_FIXED_DATASET_PATH = "/tmp/vaws_dsv4_fixed_requests_512x512.jsonl"

PYTHONPATH_VALUE = ":".join(
    [
        "/vllm-workspace/vllm",
        "/vllm-workspace/vllm-ascend",
        "/usr/local/Ascend/cann-9.0.0/python/site-packages",
        "/usr/local/Ascend/cann-9.0.0/opp/built-in/op_impl/ai_core/tbe",
    ]
)

# DSV4 Flash fixed service configuration. Keep this list stable across
# baseline/PR states so performance comparisons are attributable to code only.
EXTRA_ENV = {
    "PYTHONPATH": PYTHONPATH_VALUE,
    "VLLM_VERSION": "0.21.0",
    "OMP_PROC_BIND": "false",
    "OMP_NUM_THREADS": "10",
    "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
    "LD_PRELOAD": "/usr/lib/aarch64-linux-gnu/libjemalloc.so.2",
    "HCCL_BUFFSIZE": "1024",
    "VLLM_ASCEND_ENABLE_FLASHCOMM1": "1",
    "TASK_QUEUE_ENABLE": "1",
    "HCCL_OP_EXPANSION_MODE": "AIV",
}

SERVE_ARGS = [
    "--max-model-len",
    "3072",
    "--max-num-batched-tokens",
    "8192",
    "--max-num-seqs",
    "32",
    "--gpu-memory-utilization",
    "0.9",
    "--enable-expert-parallel",
    "--tokenizer-mode",
    "deepseek_v4",
    "--tool-call-parser",
    "deepseek_v4",
    "--enable-auto-tool-choice",
    "--reasoning-parser",
    "deepseek_v4",
    "--safetensors-load-strategy",
    "prefetch",
    "--no-enable-prefix-caching",
    "--model-loader-extra-config",
    '{"enable_multithread_load": "true", "num_threads": 128}',
    "--quantization",
    "ascend",
    "--block-size",
    "128",
    "--speculative-config",
    '{"num_speculative_tokens": 1, "method": "mtp", "enforce_eager": true}',
    "--compilation-config",
    '{"cudagraph_mode":"FULL_DECODE_ONLY"}',
    "--async-scheduling",
    "--additional-config",
    (
        '{"ascend_compilation_config":{"enable_npugraph_ex":true,'
        '"enable_static_kernel":false},"enable_cpu_binding":true,'
        '"multistream_overlap_shared_expert":true}'
    ),
]


def build_serve_args(args: argparse.Namespace) -> list[str]:
    serve_args = list(SERVE_ARGS)
    if args.enable_dsa_cp or args.enable_dsv4_dsa_overlap or args.disable_dsv4_dsa_overlap:
        idx = serve_args.index("--additional-config") + 1
        additional_config = json.loads(serve_args[idx])
        if args.enable_dsa_cp:
            additional_config["enable_dsa_cp"] = True
        if args.disable_dsv4_dsa_overlap:
            additional_config["multistream_dsv4_dsa_overlap"] = False
        elif args.enable_dsv4_dsa_overlap:
            additional_config["multistream_dsv4_dsa_overlap"] = True
        serve_args[idx] = json.dumps(additional_config, separators=(",", ":"))
    return serve_args

BENCH_ARGS = [
    "--dataset-name",
    "random",
    "--seed",
    "0",
    "--random-input-len",
    "512",
    "--random-output-len",
    "512",
    "--ignore-eos",
    "--num-prompts",
    "1",
    "--max-concurrency",
    "1",
]

METRIC_KEYS = [
    "mean_tpot_ms",
    "median_tpot_ms",
    "mean_ttft_ms",
    "median_ttft_ms",
    "output_throughput",
    "request_throughput",
    "spec_decode_acceptance_rate",
]


@dataclass(frozen=True)
class StateSpec:
    label: str
    refspec: str
    checkout_ref: str
    fetch_cmd: str | None
    branch: str


def emit(tag: str, payload: dict[str, Any]) -> None:
    print(f"__DSV4_FLASH_BENCH__ {tag} {json.dumps(payload, ensure_ascii=False)}", flush=True)


def safe_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip(".-")
    return token or "state"


def parse_state(value: str) -> StateSpec:
    if "=" not in value:
        raise argparse.ArgumentTypeError("--state must be LABEL=REF, for example pr10805=pr:10805")
    raw_label, raw_refspec = value.split("=", 1)
    label = safe_token(raw_label)
    refspec = raw_refspec.strip()
    if not label or not refspec:
        raise argparse.ArgumentTypeError("--state label/ref cannot be empty")

    branch = f"codex-bench-{label}"
    fetch_cmd: str | None = None
    checkout_ref = refspec

    pr_match = re.fullmatch(r"(?:pr:|#)?(\d+)", refspec)
    if pr_match:
        pr_num = pr_match.group(1)
        checkout_ref = f"refs/remotes/origin/pr-{pr_num}-latest"
        fetch_cmd = (
            "git fetch origin "
            f"{shlex.quote(f'pull/{pr_num}/head:{checkout_ref}')}"
        )
        branch = f"codex-bench-pr-{pr_num}-{label}"

    return StateSpec(
        label=label,
        refspec=refspec,
        checkout_ref=checkout_ref,
        fetch_cmd=fetch_cmd,
        branch=branch,
    )


def run_json_streaming(cmd: list[str], *, timeout: int | None = None) -> dict[str, Any]:
    emit("command", {"cmd": cmd})
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stderr_lines: list[str] = []

    def relay_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            stderr_lines.append(line)
            print(line, end="", file=sys.stderr, flush=True)

    thread = threading.Thread(target=relay_stderr, daemon=True)
    thread.start()
    try:
        assert proc.stdout is not None
        stdout = proc.stdout.read()
        rc = proc.wait(timeout=timeout)
    finally:
        thread.join(timeout=1)

    if stdout.strip():
        print(stdout, end="" if stdout.endswith("\n") else "\n", flush=True)

    if rc != 0:
        raise RuntimeError(
            f"command failed rc={rc}: {' '.join(cmd)}\n"
            f"stderr_tail={''.join(stderr_lines)[-3000:]}\n"
            f"stdout_tail={stdout[-3000:]}"
        )

    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"command did not return JSON: {exc}; stdout_tail={stdout[-3000:]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"command JSON is not an object: {type(parsed).__name__}")
    return parsed


def remote_checkout(state: StateSpec, args: argparse.Namespace) -> dict[str, Any]:
    lines = [
        "set -euo pipefail",
        "if [ -d /vllm-workspace/vllm/.git ]; then",
        "  cd /vllm-workspace/vllm",
        "  git reset --hard >/dev/null",
        "  git rev-parse --verify ${VAWS_DSV4_VLLM_REF:?} >/dev/null",
        "  git checkout -B codex-bench-vllm ${VAWS_DSV4_VLLM_REF:?}",
        "fi",
        "cd /vllm-workspace/vllm-ascend",
        "git reset --hard >/dev/null",
    ]
    if not args.skip_remote_fetch:
        lines.insert(4, "  git fetch origin main || true")
    if state.fetch_cmd and not args.skip_remote_fetch:
        lines.append(f"({state.fetch_cmd})")
    elif not args.skip_remote_fetch:
        lines.append("git fetch origin main || true")
    lines.extend(
        [
            f"git rev-parse --verify {shlex.quote(state.checkout_ref)} >/dev/null",
            (
                "git checkout -B "
                f"{shlex.quote(state.branch)} {shlex.quote(state.checkout_ref)}"
            ),
            "printf 'status:\\n'",
            "git status --short --branch",
            "printf 'head:\\n'",
            "git rev-parse HEAD",
            "git log -1 --oneline --decorate",
            "printf 'vllm_head:\\n'",
            "git -C /vllm-workspace/vllm rev-parse HEAD",
            "git -C /vllm-workspace/vllm log -1 --oneline --decorate",
            "printf 'custom_op_digest: '",
            (
                "find /vllm-workspace/vllm-ascend/vllm_ascend "
                "-path '*/_cann_ops_custom/*' -type f -print0 2>/dev/null "
                "| sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum || true"
            ),
        ]
    )
    return run_json_streaming(
        [
            "python3",
            str(REMOTE_EXEC),
            "--session-id",
            args.session_id,
            "--cwd",
            "/vllm-workspace/vllm-ascend",
            "--timeout",
            "300",
            "--env",
            f"VAWS_DSV4_VLLM_REF={DEFAULT_VLLM_REF}",
            "--command",
            "\n".join(lines),
        ],
        timeout=360,
    )


METADATA_PROBE = r'''
def _vaws_dsv4_probe_source():
    return r"""
    def _maybe_dump_dsv4_compressor_metadata(self, attn_metadata, tag: str) -> None:
        import dataclasses
        import os
        from pathlib import Path

        import torch

        dump_root = os.environ.get("VAWS_DSV4_METADATA_DUMP_DIR")
        if not dump_root or not getattr(self, "use_compress", False):
            return
        limit = int(os.environ.get("VAWS_DSV4_METADATA_DUMP_LIMIT", "8"))
        dump_count = getattr(self, "_vaws_dsv4_metadata_dump_count", 0)
        if dump_count >= limit:
            return
        setattr(self, "_vaws_dsv4_metadata_dump_count", dump_count + 1)

        rank = os.environ.get("RANK") or os.environ.get("LOCAL_RANK") or f"pid_{os.getpid()}"
        out_dir = Path(dump_root) / f"rank_{rank}"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{dump_count:04d}_{tag}.pt"

        allowed = {
            "num_input_tokens",
            "num_actual_tokens",
            "query_lens",
            "slot_mapping",
            "query_start_loc",
            "query_start_loc_cpu",
            "seq_lens",
            "seq_lens_list",
            "block_tables",
            "block_table",
            "input_positions",
            "start_pos",
            "sin",
            "cos",
            "compress_sin",
            "compress_cos",
            "hadamard",
            "cu_c4_cmp_seqlen_list",
            "cu_c128_cmp_seqlen_list",
            "sas_metadata",
            "qli_metadata",
            "num_decodes",
            "num_decode_tokens",
            "num_prefills",
            "max_seq_lens",
            "max_seqlen_kv",
            "max_seqlen_q",
            "prefill",
            "decode",
            "req_metadata",
            "cp_metadata",
            "local_query_start_loc",
            "local_seq_lens",
            "local_start",
            "local_end",
            "tokens_per_rank",
            "num_tokens_pad",
            "local_sin",
            "local_cos",
            "cu_cmp_seqlen_list",
        }
        tensors = {}
        scalars = {}
        debug = {}
        seen = set()

        def keep_tensor(tensor):
            detached = tensor.detach()
            if detached.device.type != "cpu":
                detached = detached.cpu()
            else:
                detached = detached.clone()
            return detached

        def collect(obj, path):
            if obj is None:
                scalars[path] = None
                return
            if torch.is_tensor(obj):
                tensors[path] = keep_tensor(obj)
                return
            if isinstance(obj, (int, float, str, bool)):
                scalars[path] = obj
                return
            if isinstance(obj, (list, tuple)):
                if all(isinstance(x, (int, float, str, bool)) or x is None for x in obj):
                    scalars[path] = list(obj)
                    return
                for idx, item in enumerate(obj):
                    collect(item, f"{path}.{idx}")
                return
            if isinstance(obj, dict):
                for key in sorted(obj.keys(), key=str):
                    collect(obj[key], f"{path}.{key}")
                return
            obj_id = id(obj)
            if obj_id in seen:
                return
            seen.add(obj_id)
            if dataclasses.is_dataclass(obj):
                for field in dataclasses.fields(obj):
                    if field.name in allowed:
                        collect(getattr(obj, field.name), f"{path}.{field.name}")
            attrs = getattr(obj, "__dict__", None)
            if isinstance(attrs, dict):
                for key in sorted(attrs.keys()):
                    if key in allowed:
                        collect(attrs[key], f"{path}.{key}")
            for key in sorted(allowed):
                if hasattr(obj, key):
                    collect(getattr(obj, key), f"{path}.{key}")

        def describe(obj, path):
            info = {"type": type(obj).__name__}
            if dataclasses.is_dataclass(obj):
                info["dataclass_fields"] = [field.name for field in dataclasses.fields(obj)]
            attrs = getattr(obj, "__dict__", None)
            if isinstance(attrs, dict):
                info["dict_keys"] = sorted(attrs.keys())
            for key in ("decode", "prefill", "req_metadata", "cp_metadata"):
                child = getattr(obj, key, None)
                info[f"{key}_type"] = type(child).__name__ if child is not None else None
            debug[path] = info

        key_pattern = os.environ.get("VAWS_DSV4_METADATA_KEY_PATTERN", "model.layers.0.self_attn")

        def keep_layer(layer_name):
            return key_pattern in str(layer_name)

        if isinstance(attn_metadata, list):
            for ubid, ubatch_metadata in enumerate(attn_metadata):
                if isinstance(ubatch_metadata, dict):
                    debug[f"ubatch_{ubid}.__layer_keys__"] = {
                        "type": "layer_key_list",
                        "count": len(ubatch_metadata),
                        "keys": sorted(map(str, ubatch_metadata.keys())),
                        "pattern": key_pattern,
                    }
                    for layer_name in sorted(ubatch_metadata.keys()):
                        if not keep_layer(layer_name):
                            continue
                        describe(ubatch_metadata[layer_name], f"ubatch_{ubid}.{layer_name}")
                        collect(ubatch_metadata[layer_name], f"ubatch_{ubid}.{layer_name}")
        elif isinstance(attn_metadata, dict):
            debug["__layer_keys__"] = {
                "type": "layer_key_list",
                "count": len(attn_metadata),
                "keys": sorted(map(str, attn_metadata.keys())),
                "pattern": key_pattern,
            }
            for layer_name in sorted(attn_metadata.keys()):
                if not keep_layer(layer_name):
                    continue
                describe(attn_metadata[layer_name], f"layer.{layer_name}")
                collect(attn_metadata[layer_name], f"layer.{layer_name}")
        else:
            describe(attn_metadata, "attn_metadata")
            collect(attn_metadata, "attn_metadata")

        torch.save(
            {
                "tag": tag,
                "rank": rank,
                "dump_count": dump_count,
                "runner_attrs": {
                    name: repr(getattr(self, name, None))
                    for name in ("rank", "local_rank", "global_rank", "tp_rank", "dp_rank")
                },
                "tensors": tensors,
                "scalars": scalars,
                "debug": debug,
            },
            out_path,
        )
"""

from pathlib import Path

path = Path("/vllm-workspace/vllm-ascend/vllm_ascend/worker/model_runner_v1.py")
text = path.read_text()
method_marker = "    def _maybe_dump_dsv4_compressor_metadata(self, attn_metadata, tag: str) -> None:"
if method_marker not in text:
    insert_before = "    def _rebuild_input_ids_with_corrected_positions("
    text = text.replace(insert_before, _vaws_dsv4_probe_source() + "\n" + insert_before, 1)
call = '                self._maybe_dump_dsv4_compressor_metadata(attn_metadata, "before_forward")\n'
if call not in text:
    marker = "\n                self._sanitize_placeholder_input_ids_for_forward(\n"
    text = text.replace(marker, "\n" + call + marker, 1)
path.write_text(text)
'''


def install_metadata_probe(session_id: str) -> dict[str, Any]:
    command = (
        "set -euo pipefail\n"
        "cat > /tmp/vaws_install_dsv4_metadata_probe.py <<'PY'\n"
        f"{METADATA_PROBE}\n"
        "PY\n"
        "python3 /tmp/vaws_install_dsv4_metadata_probe.py\n"
        "python3 -m py_compile /vllm-workspace/vllm-ascend/vllm_ascend/worker/model_runner_v1.py\n"
    )
    return run_json_streaming(
        [
            "python3",
            str(REMOTE_EXEC),
            "--session-id",
            session_id,
            "--cwd",
            "/vllm-workspace/vllm-ascend",
            "--timeout",
            "120",
            "--command",
            command,
        ],
        timeout=180,
    )


def apply_remote_patch(session_id: str, patch_path: Path) -> dict[str, Any]:
    patch_b64 = base64.b64encode(patch_path.read_bytes()).decode("ascii")
    command = (
        "set -euo pipefail\n"
        "python3 - <<'PY'\n"
        "import base64\n"
        f"data = {patch_b64!r}\n"
        "open('/tmp/vaws_dsv4_remote_patch.diff', 'wb').write(base64.b64decode(data))\n"
        "PY\n"
        "git apply /tmp/vaws_dsv4_remote_patch.diff\n"
        "git status --short\n"
        "python3 -m py_compile /vllm-workspace/vllm-ascend/vllm_ascend/worker/model_runner_v1.py\n"
    )
    return run_json_streaming(
        [
            "python3",
            str(REMOTE_EXEC),
            "--session-id",
            session_id,
            "--cwd",
            "/vllm-workspace/vllm-ascend",
            "--timeout",
            "120",
            "--command",
            command,
        ],
        timeout=180,
    )


def prepare_fixed_request_dataset(
    args: argparse.Namespace,
    request_counts: list[int | None],
) -> dict[str, Any]:
    max_requests = max(
        (
            count
            if count is not None
            else (args.bench_num_prompts if args.bench_num_prompts is not None else 1)
        )
        for count in request_counts
    )
    command = r'''
set -euo pipefail
python3 - <<'PY'
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
PY
'''
    cmd = [
        "python3",
        str(REMOTE_EXEC),
        "--session-id",
        args.session_id,
        "--cwd",
        "/vllm-workspace",
        "--timeout",
        "240",
        "--env",
        f"PYTHONPATH={PYTHONPATH_VALUE}",
        "--env",
        f"VAWS_FIXED_MODEL={args.model}",
        "--env",
        "VAWS_FIXED_TOKENIZER_MODE=deepseek_v4",
        "--env",
        f"VAWS_FIXED_INPUT_LEN={args.fixed_request_input_len}",
        "--env",
        f"VAWS_FIXED_OUTPUT_LEN={args.fixed_request_output_len}",
        "--env",
        f"VAWS_FIXED_NUM_ROWS={max_requests}",
        "--env",
        f"VAWS_FIXED_DATASET_PATH={args.fixed_request_dataset_path}",
    ]
    if args.fixed_request_prompt:
        cmd.extend(["--env", f"VAWS_FIXED_PROMPT={args.fixed_request_prompt}"])
    cmd.extend(["--command", command])
    return run_json_streaming(cmd, timeout=300)


def start_service(args: argparse.Namespace) -> dict[str, Any]:
    cmd = [
        "python3",
        str(SERVING_DIR / "serve_start.py"),
        "--session-id",
        args.session_id,
        "--model",
        args.model,
        "--served-model-name",
        args.served_model_name,
        "--tp",
        str(args.tp),
        "--dp",
        str(args.dp),
        "--devices",
        args.devices,
        "--port",
        str(args.port),
        "--health-timeout",
        str(args.health_timeout),
        "--skip-parity",
    ]
    extra_env = dict(EXTRA_ENV)
    if args.metadata_dump_dir:
        extra_env["VAWS_DSV4_METADATA_DUMP_DIR"] = f"{args.metadata_dump_dir.rstrip('/')}/{args.current_state_label}"
        extra_env["VAWS_DSV4_METADATA_DUMP_LIMIT"] = str(args.metadata_dump_limit)
        extra_env["VAWS_DSV4_METADATA_KEY_PATTERN"] = args.metadata_key_pattern
    for key, value in extra_env.items():
        cmd.extend(["--extra-env", f"{key}={value}"])
    cmd.append("--")
    cmd.extend(build_serve_args(args))
    return run_json_streaming(cmd, timeout=args.health_timeout + 300)


def cleanup_stale_processes(session_id: str, *, phase: str) -> dict[str, Any]:
    """Clean stale VLLM children left after interrupted service startup.

    serve_stop.py targets the recorded API server PID. If the API server is
    interrupted externally, EngineCore/Worker children can become orphaned and
    keep HBM allocated. This cleanup is intentionally scoped to the current
    session container and to process names/paths that identify vLLM runtime
    children.
    """
    command = r"""
set +e
pids=$(ps -eo pid=,comm=,cmd= | awk '
  $1 == 1 {next}
  $2 ~ /^VLLM::EngineCor/ ||
  $2 ~ /^VLLM::Worker_TP/ ||
  ($2 == "python3" && index($0, "-c from multiprocessing.resource_tracker import main") > 0)
  {print $1}
')
echo "stale_pids=${pids:-}"
if [ -n "${pids:-}" ]; then
  kill -TERM $pids 2>/dev/null || true
  sleep 3
  kill -KILL $pids 2>/dev/null || true
fi
sleep 2
echo "remaining:"
ps -eo pid,ppid,comm,stat,etime,cmd \
  | grep -E "VLLM::|-c from multiprocessing.resource_tracker import main" \
  | grep -v grep || true
exit 0
""".strip()
    result = run_json_streaming(
        [
            "python3",
            str(REMOTE_EXEC),
            "--session-id",
            session_id,
            "--cwd",
            "/vllm-workspace",
            "--timeout",
            "120",
            "--command",
            command,
        ],
        timeout=180,
    )
    emit(
        "cleanup_stale",
        {
            "phase": phase,
            "status": result.get("status"),
            "stdout_tail": result.get("stdout_tail"),
        },
    )
    return result


def stop_service(session_id: str) -> dict[str, Any]:
    return run_json_streaming(
        [
            "python3",
            str(SERVING_DIR / "serve_stop.py"),
            "--session-id",
            session_id,
            "--force",
        ],
        timeout=180,
    )


def run_accuracy_probe(args: argparse.Namespace, state_label: str) -> dict[str, Any]:
    payload = {
        "model": args.served_model_name,
        "prompt": args.accuracy_prompt,
        "max_tokens": args.accuracy_max_tokens,
        "temperature": 0.0,
        "stream": False,
    }
    command = r"""
import hashlib
import json
import os
import urllib.error
import urllib.request

payload = json.loads(os.environ["VAWS_DSV4_ACCURACY_PAYLOAD"])
url = os.environ["VAWS_DSV4_ACCURACY_URL"]
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
""".strip()
    result = run_json_streaming(
        [
            "python3",
            str(REMOTE_EXEC),
            "--session-id",
            args.session_id,
            "--cwd",
            "/vllm-workspace",
            "--timeout",
            "360",
            "--env",
            f"VAWS_DSV4_ACCURACY_URL=http://127.0.0.1:{args.port}/v1/completions",
            "--env",
            f"VAWS_DSV4_ACCURACY_PAYLOAD={json.dumps(payload, ensure_ascii=False)}",
            "--command",
            f"python3 - <<'PY'\n{command}\nPY",
        ],
        timeout=420,
    )
    stdout_tail = (result.get("stdout_tail") or "").strip()
    probe = json.loads(stdout_tail.splitlines()[-1]) if stdout_tail else {"status": "failed", "error": "empty stdout"}
    probe["label"] = state_label
    probe["prompt_sha256"] = hashlib.sha256(args.accuracy_prompt.encode("utf-8")).hexdigest()
    return probe


def shell_export(name: str, value: str) -> str:
    return f"export {name}={shlex.quote(value)}; "


def patch_benchmark_preamble() -> None:
    original = bench_common._ascend_env_preamble

    def patched() -> str:
        return (
            original()
            + shell_export("PYTHONPATH", PYTHONPATH_VALUE)
            + shell_export("VLLM_VERSION", EXTRA_ENV["VLLM_VERSION"])
        )

    bench_common._ascend_env_preamble = patched


def aggregate(raw_results: list[dict[str, Any]], warmup_runs: int) -> dict[str, Any]:
    used = raw_results[warmup_runs:]
    out: dict[str, Any] = {
        "count": len(used),
        "warmup_runs": warmup_runs,
        "total_runs": len(raw_results),
    }
    for key in METRIC_KEYS:
        values = [r.get(key) for r in used if isinstance(r.get(key), (int, float))]
        if not values:
            continue
        out[key] = {
            "mean": statistics.mean(values),
            "stddev": statistics.stdev(values) if len(values) > 1 else 0.0,
            "min": min(values),
            "max": max(values),
            "values": values,
        }
    return out


def per_run(raw_results: list[dict[str, Any]], warmup_runs: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_results, start=1):
        rows.append(
            {
                "run": idx,
                "warmup": idx <= warmup_runs,
                "metrics": {key: raw.get(key) for key in METRIC_KEYS},
            }
        )
    return rows


def parse_request_counts(value: str | None) -> list[int | None]:
    if not value:
        return [None]
    counts: list[int | None] = []
    for raw in value.split(","):
        item = raw.strip()
        if not item:
            continue
        count = int(item)
        if count <= 0:
            raise ValueError("--bench-request-counts entries must be positive")
        counts.append(count)
    return counts or [None]


def bench_case_label(request_count: int | None) -> str:
    return "default" if request_count is None else f"requests_{request_count}"


def build_bench_args(args: argparse.Namespace, request_count: int | None = None) -> list[str]:
    if args.fixed_request_dataset:
        bench_args = [
            "--dataset-name",
            "custom",
            "--dataset-path",
            args.fixed_request_dataset_path,
            "--custom-output-len",
            str(args.fixed_request_output_len),
            "--skip-chat-template",
            "--disable-shuffle",
            "--ignore-eos",
            "--num-prompts",
            "1",
            "--max-concurrency",
            "1",
        ]
    else:
        bench_args = list(BENCH_ARGS)
    if args.bench_seed is not None:
        if "--seed" in bench_args:
            idx = bench_args.index("--seed") + 1
            bench_args[idx] = str(args.bench_seed)
        else:
            bench_args.extend(["--seed", str(args.bench_seed)])
    if args.bench_random_input_len is not None:
        if "--random-input-len" in bench_args:
            idx = bench_args.index("--random-input-len") + 1
            bench_args[idx] = str(args.bench_random_input_len)
    if args.bench_random_output_len is not None:
        if "--random-output-len" in bench_args:
            idx = bench_args.index("--random-output-len") + 1
            bench_args[idx] = str(args.bench_random_output_len)
    if args.bench_num_prompts is not None:
        idx = bench_args.index("--num-prompts") + 1
        bench_args[idx] = str(args.bench_num_prompts)
    if args.bench_max_concurrency is not None:
        idx = bench_args.index("--max-concurrency") + 1
        bench_args[idx] = str(args.bench_max_concurrency)
    if request_count is not None:
        idx = bench_args.index("--num-prompts") + 1
        bench_args[idx] = str(request_count)
        idx = bench_args.index("--max-concurrency") + 1
        bench_args[idx] = str(request_count)
    if args.bench_temperature is not None:
        bench_args.extend(["--temperature", str(args.bench_temperature)])
    return bench_args


def build_bench_config(
    args: argparse.Namespace,
    request_count: int | None = None,
) -> bench_common.BenchConfig:
    return bench_common.BenchConfig(
        machine="",
        session_id=args.session_id,
        model=args.model,
        port=args.port,
        bench_args=build_bench_args(args, request_count),
    )


def run_state(args: argparse.Namespace, state: StateSpec) -> dict[str, Any]:
    emit("state_start", {"label": state.label, "refspec": state.refspec})
    args.current_state_label = state.label
    checkout_result = remote_checkout(state, args)
    if args.remote_patch_file:
        patch_result = apply_remote_patch(args.session_id, Path(args.remote_patch_file))
        emit("remote_patch_applied", {"label": state.label, "status": patch_result.get("status")})
    if args.metadata_dump_dir:
        probe_result = install_metadata_probe(args.session_id)
        emit("metadata_probe_installed", {"label": state.label, "status": probe_result.get("status")})
    request_counts = parse_request_counts(args.bench_request_counts)
    fixed_dataset_result: dict[str, Any] | None = None
    if args.fixed_request_dataset:
        fixed_dataset_result = prepare_fixed_request_dataset(args, request_counts)
        emit(
            "fixed_dataset_ready",
            {
                "label": state.label,
                "status": fixed_dataset_result.get("status"),
                "stdout_tail": fixed_dataset_result.get("stdout_tail", "")[-1000:],
            },
        )
    serve_result: dict[str, Any] | None = None
    raw_results: list[dict[str, Any]] = []

    try:
        if args.stale_cleanup:
            cleanup_stale_processes(args.session_id, phase=f"before-{state.label}")
        serve_result = start_service(args)
        ready = bool(serve_result.get("readiness", {}).get("ready"))
        status = serve_result.get("status")
        if status not in ("ready", "running") or not ready:
            raise RuntimeError(
                "service did not become ready: "
                + json.dumps(serve_result, ensure_ascii=False)[:3000]
            )

        emit(
            "service_ready",
            {
                "label": state.label,
                "pid": serve_result.get("pid"),
                "elapsed": serve_result.get("readiness", {}).get("elapsed_seconds"),
                "runtime_dir": serve_result.get("runtime_dir"),
            },
        )

        accuracy_probe: dict[str, Any] | None = None
        if args.accuracy_probe:
            emit("accuracy_start", {"label": state.label, "max_tokens": args.accuracy_max_tokens})
            accuracy_probe = run_accuracy_probe(args, state.label)
            emit(
                "accuracy_result",
                {
                    "label": state.label,
                    "status": accuracy_probe.get("status"),
                    "text_sha256": accuracy_probe.get("text_sha256"),
                    "finish_reason": accuracy_probe.get("finish_reason"),
                    "usage": accuracy_probe.get("usage"),
                },
            )

        container_ip, container_port = bench_common._get_ssh_endpoint(
            None,
            session_id=args.session_id,
        )
        benchmark_cases: list[dict[str, Any]] = []
        for request_count in request_counts:
            case_label = bench_case_label(request_count)
            config = build_bench_config(args, request_count)
            case_raw_results: list[dict[str, Any]] = []
            for run_idx in range(1, args.runs + 1):
                warmup = run_idx <= args.warmup_runs
                emit(
                    "bench_start",
                    {
                        "label": state.label,
                        "case": case_label,
                        "request_count": request_count,
                        "run": run_idx,
                        "runs": args.runs,
                        "warmup": warmup,
                    },
                )
                result = bench_common.run_bench_on_remote(
                    config,
                    f"http://127.0.0.1:{args.port}",
                    args.served_model_name,
                    container_ip,
                    container_port,
                )
                case_raw_results.append(result)
                emit(
                    "bench_result",
                    {
                        "label": state.label,
                        "case": case_label,
                        "request_count": request_count,
                        "run": run_idx,
                        "warmup": warmup,
                        "mean_tpot_ms": result.get("mean_tpot_ms"),
                        "output_throughput": result.get("output_throughput"),
                        "spec_decode_acceptance_rate": result.get("spec_decode_acceptance_rate"),
                    },
                )
                if args.between_run_sleep > 0 and run_idx < args.runs:
                    time.sleep(args.between_run_sleep)
            case_result = {
                "case": case_label,
                "request_count": request_count,
                "bench_args": build_bench_args(args, request_count),
                "per_run": per_run(case_raw_results, args.warmup_runs),
                "raw_results": case_raw_results,
                "aggregated": aggregate(case_raw_results, args.warmup_runs),
            }
            benchmark_cases.append(case_result)
            if request_count is None:
                raw_results = case_raw_results

        primary_case = benchmark_cases[0]
        if not raw_results:
            raw_results = primary_case["raw_results"]
        result_json: dict[str, Any] = {
            "status": "ok",
            "label": state.label,
            "refspec": state.refspec,
            "state": {
                "label": state.label,
                "refspec": state.refspec,
                "checkout_ref": state.checkout_ref,
                "branch": state.branch,
            },
            "checkout_result": checkout_result,
            "runs": args.runs,
            "warmup_runs": args.warmup_runs,
            "per_run": per_run(raw_results, args.warmup_runs),
            "raw_results": raw_results,
            "aggregated": primary_case["aggregated"],
            "benchmark_cases": benchmark_cases,
            "accuracy_probe": accuracy_probe,
            "fixed_dataset_result": fixed_dataset_result,
            "timestamp": bench_common.now_utc(),
            "config": {
                "session_id": args.session_id,
                "model": args.model,
                "served_model_name": args.served_model_name,
                "port": args.port,
                "tp": args.tp,
                "dp": args.dp,
                "devices": args.devices,
                "extra_env": EXTRA_ENV,
                "metadata_dump_dir": args.metadata_dump_dir,
                "metadata_dump_limit": args.metadata_dump_limit,
                "metadata_key_pattern": args.metadata_key_pattern,
                "serve_args": build_serve_args(args),
                "enable_dsa_cp": args.enable_dsa_cp,
                "bench_args": build_bench_args(args),
                "bench_request_counts": args.bench_request_counts,
                "fixed_request_dataset": args.fixed_request_dataset,
                "fixed_request_input_len": args.fixed_request_input_len,
                "fixed_request_output_len": args.fixed_request_output_len,
                "fixed_request_dataset_path": args.fixed_request_dataset_path,
            },
            "serve_result": serve_result,
        }
        result_path = bench_common.write_local_result(build_bench_config(args), result_json)
        result_json["result_path"] = str(result_path)
        result_json["run_dir"] = str(result_path.parent)
        emit(
            "state_done",
            {
                "label": state.label,
                "result_path": str(result_path),
                "mean_tpot_ms": result_json["aggregated"].get("mean_tpot_ms", {}).get("mean"),
                "std_tpot_ms": result_json["aggregated"].get("mean_tpot_ms", {}).get("stddev"),
            },
        )
        return result_json
    finally:
        emit("stop_service", {"label": state.label})
        try:
            stop = stop_service(args.session_id)
            emit("stop_result", {"label": state.label, "status": stop.get("status"), "pid": stop.get("pid")})
            if args.stale_cleanup:
                cleanup_stale_processes(args.session_id, phase=f"after-{state.label}")
        except Exception as exc:
            emit("stop_failed", {"label": state.label, "error": repr(exc)})


def build_comparison(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not results:
        return []
    base_cases = {
        case.get("case"): case
        for case in results[0].get("benchmark_cases", [])
        if isinstance(case, dict)
    }
    if not base_cases:
        base_cases = {"default": results[0]}
    rows: list[dict[str, Any]] = []
    for result in results:
        cases = result.get("benchmark_cases") or [{"case": "default", "aggregated": result.get("aggregated", {})}]
        for case in cases:
            if not isinstance(case, dict):
                continue
            agg = case.get("aggregated", {})
            tpot = agg.get("mean_tpot_ms", {}).get("mean")
            base_tpot = (
                base_cases.get(case.get("case"), {})
                .get("aggregated", {})
                .get("mean_tpot_ms", {})
                .get("mean")
            )
            row = {
                "label": result.get("label"),
                "case": case.get("case"),
                "request_count": case.get("request_count"),
                "result_path": result.get("result_path"),
                "mean_tpot_ms": tpot,
                "std_tpot_ms": agg.get("mean_tpot_ms", {}).get("stddev"),
                "output_throughput": agg.get("output_throughput", {}).get("mean"),
                "spec_decode_acceptance_rate": agg.get("spec_decode_acceptance_rate", {}).get("mean"),
            }
            if isinstance(base_tpot, (int, float)) and isinstance(tpot, (int, float)):
                row["delta_tpot_ms_vs_first"] = tpot - base_tpot
                row["delta_tpot_pct_vs_first"] = (tpot - base_tpot) / base_tpot * 100
            rows.append(row)
    return rows


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run fixed DeepSeek-V4-Flash W4A8 MTP benchmark states.",
        allow_abbrev=False,
    )
    parser.add_argument(
        "--state",
        action="append",
        type=parse_state,
        required=True,
        help="State to benchmark as LABEL=REF. Use pr:10805 for GitHub PR refs.",
    )
    parser.add_argument("--session-id", default=DEFAULT_SESSION_ID)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--served-model-name", default=DEFAULT_SERVED_MODEL_NAME)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--devices", default=DEFAULT_DEVICES)
    parser.add_argument("--tp", type=int, default=8)
    parser.add_argument("--dp", type=int, default=1)
    parser.add_argument("--runs", type=int, default=6)
    parser.add_argument("--warmup-runs", type=int, default=1)
    parser.add_argument("--health-timeout", type=int, default=1200)
    parser.add_argument("--between-run-sleep", type=float, default=15.0)
    parser.add_argument(
        "--enable-dsa-cp",
        action="store_true",
        help="Add additional_config.enable_dsa_cp=true to the fixed DSV4 Flash serve args.",
    )
    parser.add_argument(
        "--enable-dsv4-dsa-overlap",
        action="store_true",
        help="Add additional_config.multistream_dsv4_dsa_overlap=true to the fixed DSV4 Flash serve args.",
    )
    parser.add_argument(
        "--disable-dsv4-dsa-overlap",
        action="store_true",
        help="Add additional_config.multistream_dsv4_dsa_overlap=false to the fixed DSV4 Flash serve args.",
    )
    parser.add_argument(
        "--stale-cleanup",
        action="store_true",
        help="Clean stale VLLM EngineCore/Worker processes before/after each state.",
    )
    parser.add_argument(
        "--metadata-dump-dir",
        help="Remote directory for validation-only DSV4 compressor metadata dumps.",
    )
    parser.add_argument("--metadata-dump-limit", type=int, default=8)
    parser.add_argument(
        "--metadata-key-pattern",
        default="model.layers.0.self_attn",
        help=(
            "Substring used by the validation probe to select attention metadata "
            "layer keys. Use a compressor layer such as model.layers.2.self_attn "
            "when validating DSV4 compressor metadata."
        ),
    )
    parser.add_argument(
        "--bench-temperature",
        type=float,
        help="Optional temperature passed to vllm bench serve, e.g. 0 for deterministic validation.",
    )
    parser.add_argument(
        "--bench-seed",
        type=int,
        default=0,
        help="Seed passed to vllm bench serve for deterministic random dataset generation.",
    )
    parser.add_argument(
        "--bench-random-input-len",
        type=int,
        help="Optional override for the random dataset input length. Defaults to fixed 512.",
    )
    parser.add_argument(
        "--bench-random-output-len",
        type=int,
        help="Optional override for the random dataset output length. Defaults to fixed 512.",
    )
    parser.add_argument(
        "--bench-num-prompts",
        type=int,
        help="Optional override for benchmark num-prompts. Use 1 or 2 for the DSV4 comparison.",
    )
    parser.add_argument(
        "--bench-max-concurrency",
        type=int,
        help="Optional override for benchmark max-concurrency. Match num-prompts for fixed-load runs.",
    )
    parser.add_argument(
        "--bench-request-counts",
        help="Comma-separated request counts to run on one service, e.g. 1,2. Each count overrides num-prompts and max-concurrency.",
    )
    parser.add_argument(
        "--fixed-request-dataset",
        action="store_true",
        help=(
            "Use a generated custom JSONL dataset with identical fixed prompts. "
            "For request-counts 1,2 this keeps the 2-request case as two copies "
            "of the same prompt instead of adding a different random prompt."
        ),
    )
    parser.add_argument(
        "--fixed-request-input-len",
        type=int,
        default=512,
        help="Exact tokenizer length for the generated fixed prompt.",
    )
    parser.add_argument(
        "--fixed-request-output-len",
        type=int,
        default=512,
        help="Output tokens per fixed custom-dataset request.",
    )
    parser.add_argument(
        "--fixed-request-dataset-path",
        default=DEFAULT_FIXED_DATASET_PATH,
        help="Remote JSONL path for the generated fixed custom dataset.",
    )
    parser.add_argument(
        "--fixed-request-prompt",
        help="Optional fixed prompt text. If provided, its tokenized length must match --fixed-request-input-len.",
    )
    parser.add_argument(
        "--remote-patch-file",
        help="Local patch file to git-apply in the remote vllm-ascend checkout after each state checkout.",
    )
    parser.add_argument(
        "--accuracy-probe",
        action="store_true",
        help="Run one fixed deterministic chat completion after service ready and before benchmark.",
    )
    parser.add_argument(
        "--accuracy-prompt",
        default=(
            "Solve this exactly and return only the final integer: "
            "12345 + 67890 - 11111."
        ),
        help="Prompt used by --accuracy-probe.",
    )
    parser.add_argument(
        "--accuracy-max-tokens",
        type=int,
        default=64,
        help="max_tokens for --accuracy-probe.",
    )
    parser.add_argument(
        "--skip-remote-fetch",
        action="store_true",
        help="Skip remote git fetches; refs must already exist in the remote checkout.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs must be >= 1")
    if args.warmup_runs < 0 or args.warmup_runs >= args.runs:
        parser.error("--warmup-runs must be >= 0 and < --runs")

    patch_benchmark_preamble()

    results: list[dict[str, Any]] = []
    for state in args.state:
        results.append(run_state(args, state))

    summary = {
        "status": "ok",
        "states": [state.label for state in args.state],
        "comparison": build_comparison(results),
        "result_paths": [result.get("result_path") for result in results],
        "fixed_config": {
            "extra_env": EXTRA_ENV,
            "metadata_dump_dir": args.metadata_dump_dir,
            "metadata_dump_limit": args.metadata_dump_limit,
            "metadata_key_pattern": args.metadata_key_pattern,
            "serve_args": build_serve_args(args),
            "enable_dsa_cp": args.enable_dsa_cp,
            "enable_dsv4_dsa_overlap": args.enable_dsv4_dsa_overlap,
            "disable_dsv4_dsa_overlap": args.disable_dsv4_dsa_overlap,
            "bench_args": build_bench_args(args),
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
