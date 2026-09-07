#!/usr/bin/env python3
"""Plan a multi-node vLLM Ascend data-parallel serving deployment.

This helper is deliberately offline: it turns one topology description into an
explicit per-node launch contract, an identity/drift probe, and a staged
readiness gate. Executing the rendered commands is the agent's job, through the
remote-dev companion tools or the serving Skill, so that this script stays
deterministic and testable without an NPU.

Progress goes to stderr as ``__VAWS_PROGRESS__=<json>``; the final payload is a
single JSON object on stdout.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
NIC_RE = re.compile(r"^[A-Za-z0-9._:-]{1,32}$")
PORT_RANGE_RE = re.compile(r"^(\d{1,5})-(\d{1,5})$")
ENV_KEY_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
LB_MODES = ("internal", "hybrid")

# Defaults verified on the workspace A3 fleet. HCCL_CONNECT_TIMEOUT below the
# default is the most common cause of spurious multi-node init failures on
# large MoE weights, so the planner keeps a generous budget.
DEFAULT_HCCL_CONNECT_TIMEOUT = 1800
DEFAULT_OMP_NUM_THREADS = 10
DEFAULT_DP_RPC_PORT = 29550
DEFAULT_API_PORT = 8000
DEFAULT_HCCL_PORT_RANGE = "20000-20127"


class PlanError(RuntimeError):
    """Raised when the requested topology cannot produce a valid plan."""


def emit_progress(phase: str, message: str) -> None:
    payload = {"phase": phase, "message": message}
    print(f"__VAWS_PROGRESS__={json.dumps(payload, sort_keys=True)}", file=sys.stderr)


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_node(spec: str) -> tuple[str, str]:
    """Parse ``ALIAS=DATA_IP``.

    Only the data-plane address is taken here. SSH endpoints stay owned by
    session-management and machine inventory, so the planner never duplicates
    connection state.
    """
    if "=" not in spec:
        raise PlanError(f"--node must be ALIAS=DATA_IP, got {spec!r}")
    alias, _, data_ip = spec.partition("=")
    alias = alias.strip()
    data_ip = data_ip.strip()
    if not ALIAS_RE.fullmatch(alias):
        raise PlanError(f"node alias {alias!r} must match {ALIAS_RE.pattern}")
    if not IPV4_RE.fullmatch(data_ip):
        raise PlanError(f"node {alias!r} data address {data_ip!r} must be an IPv4 address")
    for octet in data_ip.split("."):
        if int(octet) > 255:
            raise PlanError(f"node {alias!r} data address {data_ip!r} is not a valid IPv4 address")
    return alias, data_ip


def parse_env_assignment(spec: str) -> tuple[str, str]:
    if "=" not in spec:
        raise PlanError(f"--extra-env must be KEY=VALUE, got {spec!r}")
    key, _, value = spec.partition("=")
    key = key.strip()
    if not ENV_KEY_RE.fullmatch(key):
        raise PlanError(f"environment key {key!r} must match {ENV_KEY_RE.pattern}")
    return key, value


def parse_port_range(spec: str) -> tuple[int, int]:
    match = PORT_RANGE_RE.fullmatch(spec.strip())
    if not match:
        raise PlanError(f"--hccl-port-range must be LOW-HIGH, got {spec!r}")
    low, high = int(match.group(1)), int(match.group(2))
    if not (1024 <= low < high <= 65535):
        raise PlanError(f"--hccl-port-range {spec!r} must satisfy 1024 <= low < high <= 65535")
    return low, high


def resolve_topology(
    *,
    tp: int,
    dp: int,
    dp_local: int | None,
    node_count: int,
    devices_per_node: int | None,
) -> dict[str, int]:
    """Derive and cross-check the device layout.

    The two invariants that actually bite in practice are
    ``devices_per_node == tp * dp_local`` and ``node_count * dp_local == dp``.
    Getting either wrong produces a rank map that only fails minutes later
    during collective init, so both are checked before anything is rendered.
    """
    if tp < 1 or dp < 1:
        raise PlanError("--tp and --dp must both be >= 1")
    if node_count < 1:
        raise PlanError("at least one --node is required")
    if dp_local is None:
        if dp % node_count:
            raise PlanError(
                f"--dp {dp} is not divisible by the {node_count} declared nodes; "
                "pass --dp-local explicitly"
            )
        dp_local = dp // node_count
    if dp_local < 1:
        raise PlanError("--dp-local must be >= 1")
    if dp_local > dp:
        raise PlanError(f"--dp-local {dp_local} cannot exceed --dp {dp}")
    if node_count * dp_local != dp:
        raise PlanError(
            f"{node_count} nodes x --dp-local {dp_local} = {node_count * dp_local}, "
            f"which does not equal --dp {dp}"
        )
    derived_devices = tp * dp_local
    if devices_per_node is None:
        devices_per_node = derived_devices
    elif devices_per_node != derived_devices:
        raise PlanError(
            f"--devices-per-node {devices_per_node} does not equal --tp {tp} x "
            f"--dp-local {dp_local} = {derived_devices}"
        )
    return {
        "tensor_parallel_size": tp,
        "data_parallel_size": dp,
        "data_parallel_size_local": dp_local,
        "nodes": node_count,
        "devices_per_node": devices_per_node,
        "world_devices": tp * dp,
    }


def node_env(
    *,
    data_ip: str,
    data_nic: str,
    devices_per_node: int,
    hccl_port_range: str,
    hccl_connect_timeout: int,
    omp_num_threads: int,
    extra_env: dict[str, str],
) -> dict[str, str]:
    """Build the per-node environment contract.

    Every name here has been the proximate cause of a multi-node failure at
    least once: an unset socket interface makes gloo pick the management NIC, a
    default HCCL port range collides with a co-tenant service, and a missing
    ``expandable_segments`` allocator setting turns a working topology into a
    fragmentation OOM.
    """
    env = {
        "ASCEND_RT_VISIBLE_DEVICES": ",".join(str(i) for i in range(devices_per_node)),
        "GLOO_SOCKET_IFNAME": data_nic,
        "HCCL_CONNECT_TIMEOUT": str(hccl_connect_timeout),
        "HCCL_IF_IP": data_ip,
        "HCCL_NPU_SOCKET_PORT_RANGE": hccl_port_range,
        "HCCL_SOCKET_IFNAME": data_nic,
        "OMP_NUM_THREADS": str(omp_num_threads),
        "OMP_PROC_BIND": "false",
        "PYTORCH_NPU_ALLOC_CONF": "expandable_segments:True",
        "TP_SOCKET_IFNAME": data_nic,
        "VLLM_HOST_IP": data_ip,
        "VLLM_LOGGING_LEVEL": "INFO",
    }
    env.update(extra_env)
    return dict(sorted(env.items()))


def serve_argv(
    *,
    model: str,
    served_model_name: str,
    topology: dict[str, int],
    master_ip: str,
    api_port: int,
    dp_rpc_port: int,
    start_rank: int,
    is_master: bool,
    lb_mode: str,
    expert_parallel: bool,
    serve_args: list[str],
) -> list[str]:
    """Render one node's ``vllm serve`` argv.

    Internal LB (the default) gives exactly one API server on the master node
    and headless engine workers everywhere else. Hybrid LB gives every node its
    own API server behind an external balancer. The two modes are mutually
    exclusive in vLLM, so the caller picks one and the planner never mixes them.
    """
    argv = [
        "vllm",
        "serve",
        model,
        "--served-model-name",
        served_model_name,
        "--tensor-parallel-size",
        str(topology["tensor_parallel_size"]),
        "--data-parallel-size",
        str(topology["data_parallel_size"]),
        "--data-parallel-size-local",
        str(topology["data_parallel_size_local"]),
        "--data-parallel-address",
        master_ip,
        "--data-parallel-rpc-port",
        str(dp_rpc_port),
    ]
    if expert_parallel:
        argv.append("--enable-expert-parallel")
    if lb_mode == "hybrid":
        argv.extend(["--data-parallel-hybrid-lb", "--data-parallel-start-rank", str(start_rank)])
        argv.extend(["--port", str(api_port)])
    elif is_master:
        argv.extend(["--port", str(api_port)])
    else:
        argv.extend(["--headless", "--data-parallel-start-rank", str(start_rank)])
    argv.extend(serve_args)
    return argv


def identity_probe(*, repos: list[str]) -> dict[str, Any]:
    """The command whose output binds a run to an exact code state.

    Running old code is the single most expensive recurring mistake in this
    workspace: it invalidates every number a run produces, and it is invisible
    unless the binary identity is captured before the workload starts. The
    probe stays a plain shell snippet so it can run through remote_bash on
    every node and be diffed across nodes without a bespoke transport.
    """
    lines = ["set -u"]
    for repo in repos:
        lines.append(f'echo "repo {repo}"')
        lines.append(
            f'git -C {repo} rev-parse HEAD 2>/dev/null || echo "  <no git repo at {repo}>"'
        )
        lines.append(f'git -C {repo} status --porcelain 2>/dev/null | wc -l')
    lines.append('python3 -c "import vllm; print(\'vllm\', vllm.__version__, vllm.__file__)"')
    lines.append(
        'python3 -c "import vllm_ascend; print(\'vllm_ascend\', vllm_ascend.__file__)"'
    )
    lines.append(
        'python3 -c "import vllm_ascend.vllm_ascend_C as c; print(\'native_ext\', c.__file__)" '
        '2>&1 | tail -1'
    )
    lines.append(
        "find / -name 'vllm_ascend_C*.so' -not -path '*/proc/*' 2>/dev/null "
        "| head -5 | xargs -r sha256sum"
    )
    return {
        "purpose": "bind this deployment to an exact code state and detect per-node drift",
        "run_on": "every node, before the first workload request",
        "command": "\n".join(lines),
        "compare": (
            "All nodes must report identical commit, dirty-file count, module paths "
            "and native-extension SHA256. Any difference means the nodes are not "
            "running the same code: stop and re-sync instead of interpreting results."
        ),
    }


def readiness_gate(*, master_alias: str, api_port: int, served_model_name: str) -> list[dict[str, str]]:
    """The staged gate that separates 'started' from 'usable'.

    A 200 from ``/health`` only proves the HTTP server is up. It is reached
    before graph capture finishes and before the engine can decode, so treating
    it as ready is how a deployment ends up benchmarked while still warming up.
    The last stage is a real completion with non-empty output.
    """
    base = f"http://127.0.0.1:{api_port}"
    return [
        {
            "stage": "processes-alive",
            "target": "every node",
            "check": "the launched process group is still alive on all nodes",
            "on_failure": "read that node's own log and find the first fatal line; "
            "a worker node failure surfaces on the master only as a timeout",
        },
        {
            "stage": "health",
            "target": master_alias,
            "check": f"curl -s -o /dev/null -w '%{{http_code}}' {base}/health",
            "on_failure": "not yet ready; keep waiting within the budget rather than restarting",
        },
        {
            "stage": "models",
            "target": master_alias,
            "check": f"curl -s {base}/v1/models",
            "on_failure": "engine core did not finish registering the model; check the master log",
        },
        {
            "stage": "real-completion",
            "target": master_alias,
            "check": (
                f"curl -s {base}/v1/completions -H 'Content-Type: application/json' "
                f"-d '{{\"model\":\"{served_model_name}\",\"prompt\":\"1+1=\","
                "\"max_tokens\":8,\"temperature\":0}'"
            ),
            "on_failure": "the service is started but not usable; do not record any "
            "measurement taken before this stage passes",
        },
    ]


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    nodes = [parse_node(spec) for spec in args.node]
    aliases = [alias for alias, _ in nodes]
    if len(set(aliases)) != len(aliases):
        raise PlanError("duplicate node aliases in --node")
    data_ips = [ip for _, ip in nodes]
    if len(set(data_ips)) != len(data_ips):
        raise PlanError("duplicate node data addresses in --node")
    if not NIC_RE.fullmatch(args.data_nic):
        raise PlanError(f"--data-nic {args.data_nic!r} must match {NIC_RE.pattern}")
    if args.lb_mode not in LB_MODES:
        raise PlanError(f"--lb-mode must be one of: {', '.join(LB_MODES)}")
    if not args.model.startswith("/"):
        raise PlanError("--model must be an absolute path on the remote container")
    parse_port_range(args.hccl_port_range)

    master_alias = args.master or aliases[0]
    if master_alias not in aliases:
        raise PlanError(f"--master {master_alias!r} is not one of the declared nodes: {aliases}")
    master_ip = dict(nodes)[master_alias]

    topology = resolve_topology(
        tp=args.tp,
        dp=args.dp,
        dp_local=args.dp_local,
        node_count=len(nodes),
        devices_per_node=args.devices_per_node,
    )
    extra_env = dict(parse_env_assignment(spec) for spec in args.extra_env)
    served_model_name = args.served_model_name or Path(args.model.rstrip("/")).name

    warnings: list[str] = []
    if args.ep is not None and args.ep != topology["world_devices"]:
        warnings.append(
            f"--ep {args.ep} differs from tp x dp = {topology['world_devices']}; "
            "confirm the expert-parallel layout is intentional"
        )
    if args.api_port == args.dp_rpc_port:
        raise PlanError("--api-port and --dp-rpc-port must differ")
    low, high = parse_port_range(args.hccl_port_range)
    for name, port in (("--api-port", args.api_port), ("--dp-rpc-port", args.dp_rpc_port)):
        if low <= port <= high:
            raise PlanError(
                f"{name} {port} falls inside --hccl-port-range {args.hccl_port_range}"
            )
    if topology["nodes"] == 1:
        warnings.append(
            "single-node topology: prefer the vllm-ascend-serving Skill, which owns "
            "single-node lifecycle and state"
        )

    node_records: list[dict[str, Any]] = []
    for index, (alias, data_ip) in enumerate(nodes):
        is_master = alias == master_alias
        start_rank = index * topology["data_parallel_size_local"]
        node_records.append(
            {
                "alias": alias,
                "data_address": data_ip,
                "node_index": index,
                "is_master": is_master,
                "role": "api+engine" if (is_master or args.lb_mode == "hybrid") else "headless-engine",
                "data_parallel_start_rank": start_rank,
                "data_parallel_ranks": list(
                    range(start_rank, start_rank + topology["data_parallel_size_local"])
                ),
                "log_path": f"{args.log_root.rstrip('/')}/{alias}.log",
                "environment_variables": node_env(
                    data_ip=data_ip,
                    data_nic=args.data_nic,
                    devices_per_node=topology["devices_per_node"],
                    hccl_port_range=args.hccl_port_range,
                    hccl_connect_timeout=args.hccl_connect_timeout,
                    omp_num_threads=args.omp_num_threads,
                    extra_env=extra_env,
                ),
                "command": serve_argv(
                    model=args.model,
                    served_model_name=served_model_name,
                    topology=topology,
                    master_ip=master_ip,
                    api_port=args.api_port,
                    dp_rpc_port=args.dp_rpc_port,
                    start_rank=start_rank,
                    is_master=is_master,
                    lb_mode=args.lb_mode,
                    expert_parallel=args.expert_parallel,
                    serve_args=list(args.serve_args),
                ),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": utc_now(),
        "lb_mode": args.lb_mode,
        "master": {"alias": master_alias, "data_address": master_ip},
        "topology": topology,
        "model": {"path": args.model, "served_model_name": served_model_name},
        "ports": {
            "api": args.api_port,
            "data_parallel_rpc": args.dp_rpc_port,
            "hccl_range": args.hccl_port_range,
        },
        "launch_order": [
            "Start every non-master node first, then the master.",
            "The master blocks until all declared data-parallel ranks report in, "
            "so a missing worker looks like a master-side hang.",
        ],
        "nodes": node_records,
        "identity_probe": identity_probe(repos=list(args.identity_repo)),
        "readiness_gate": readiness_gate(
            master_alias=master_alias,
            api_port=args.api_port,
            served_model_name=served_model_name,
        ),
        "warnings": warnings,
    }


def render_env_prefix(env: dict[str, str]) -> str:
    return "\n".join(f"export {key}={value}" for key, value in env.items())


def shell_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_@%+=:,./-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def render_node(plan: dict[str, Any], alias: str, what: str) -> str:
    for node in plan["nodes"]:
        if node["alias"] == alias:
            break
    else:
        known = ", ".join(item["alias"] for item in plan["nodes"])
        raise PlanError(f"node {alias!r} is not in this plan; known nodes: {known}")
    if what == "identity":
        return plan["identity_probe"]["command"]
    command = " ".join(shell_quote(part) for part in node["command"])
    return "\n".join(
        [
            render_env_prefix(node["environment_variables"]),
            f"mkdir -p {Path(node['log_path']).parent}",
            f"{command} > {node['log_path']} 2>&1",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Plan a multi-node vLLM Ascend data-parallel serving deployment.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="derive the per-node launch contract", allow_abbrev=False)
    plan.add_argument(
        "--node",
        action="append",
        required=True,
        metavar="ALIAS=DATA_IP",
        help="declare one node by alias and data-plane address; repeat per node, "
        "declaration order defines the data-parallel rank offsets",
    )
    plan.add_argument("--master", help="alias of the node that serves the API (default: first --node)")
    plan.add_argument("--tp", "--tensor-parallel-size", dest="tp", type=int, required=True)
    plan.add_argument("--dp", "--data-parallel-size", dest="dp", type=int, required=True)
    plan.add_argument(
        "--dp-local",
        "--data-parallel-size-local",
        dest="dp_local",
        type=int,
        help="data-parallel ranks per node (default: --dp divided by the node count)",
    )
    plan.add_argument("--ep", type=int, help="expected expert-parallel width, checked against tp x dp")
    plan.add_argument(
        "--expert-parallel",
        action="store_true",
        help="add --enable-expert-parallel to every node command",
    )
    plan.add_argument("--devices-per-node", type=int, help="checked against --tp x --dp-local")
    plan.add_argument("--data-nic", required=True, help="data-plane interface name, e.g. enp210s0f0")
    plan.add_argument("--model", required=True, help="absolute model weight path on the container")
    plan.add_argument("--served-model-name", help="default: basename of --model")
    plan.add_argument("--api-port", type=int, default=DEFAULT_API_PORT)
    plan.add_argument("--dp-rpc-port", type=int, default=DEFAULT_DP_RPC_PORT)
    plan.add_argument("--hccl-port-range", default=DEFAULT_HCCL_PORT_RANGE, metavar="LOW-HIGH")
    plan.add_argument("--hccl-connect-timeout", type=int, default=DEFAULT_HCCL_CONNECT_TIMEOUT)
    plan.add_argument("--omp-num-threads", type=int, default=DEFAULT_OMP_NUM_THREADS)
    plan.add_argument("--lb-mode", default="internal", choices=LB_MODES)
    plan.add_argument("--extra-env", action="append", default=[], metavar="KEY=VALUE")
    plan.add_argument(
        "--identity-repo",
        action="append",
        default=["/vllm-workspace/vllm", "/vllm-workspace/vllm-ascend"],
        help="container-side repository path included in the identity probe",
    )
    plan.add_argument("--log-root", default="/vllm-workspace/logs/multinode")
    plan.add_argument("--output", help="write the plan JSON here as well as to stdout")
    # Trailing pass-through section, matching the benchmark Skill's convention.
    # Everything after --serve-args is appended verbatim to every node command,
    # which is the only way flag-shaped values survive argument parsing.
    plan.add_argument(
        "--serve-args",
        nargs=argparse.REMAINDER,
        default=[],
        help="everything after this flag is appended verbatim to every node's "
        "vllm serve command; must come last",
    )

    render = sub.add_parser("render", help="print one node's shell block", allow_abbrev=False)
    render.add_argument("--plan", required=True, help="path to a plan JSON produced by `plan`")
    render.add_argument("--node", required=True, help="node alias to render")
    render.add_argument("--what", default="launch", choices=("launch", "identity"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "plan":
            emit_progress("plan", f"deriving layout for {len(args.node)} node(s)")
            plan = build_plan(args)
            if args.output:
                output = Path(args.output)
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                plan["plan_path"] = str(output.resolve())
            emit_progress("plan", "layout validated")
            print(json.dumps(plan, indent=2, sort_keys=True))
            return 0
        plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
        print(render_node(plan, args.node, args.what))
        return 0
    except PlanError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2))
        return 2
    except (OSError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}, indent=2))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
