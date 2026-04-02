import argparse
import sys
from pathlib import Path
from typing import List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.lib.config import RepoPaths
from tools.lib.doctor import doctor
from tools.lib.init_flow import init_request_from_args, run_init
from tools.lib.machine import add_machine, list_machines, remove_machine, verify_machine
from tools.lib.repo_topology import normalize_remotes
from tools.lib.reset import execute_reset, prepare_reset
from tools.lib.session import create_session, status_session, switch_session
from tools.lib.benchmark import run_benchmark
from tools.lib.acceptance import AcceptanceRequest, run_acceptance
from tools.lib.serving import list_services, start_service, status_service, stop_service


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaws")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="inspect canonical workspace state and report residue")
    init_parser = subparsers.add_parser("init", help="prepare this workspace for development")
    init_parser.add_argument(
        "--server-host",
        help="machine host for first-machine setup; omit for local-only baseline",
    )
    init_parser.add_argument("--server-name")
    init_parser.add_argument("--server-user", default="root")
    init_parser.add_argument("--server-port", type=int, default=22)
    init_parser.add_argument("--server-auth-mode", default="ssh-key")
    init_parser.add_argument("--server-password-env")
    init_parser.add_argument("--server-key-path")
    init_parser.add_argument("--local-only", action="store_true")
    init_parser.add_argument(
        "--runtime-image",
        default="quay.nju.edu.cn/ascend/vllm-ascend:latest",
    )
    init_parser.add_argument("--runtime-container", default="vaws-workspace")
    init_parser.add_argument("--runtime-ssh-port", type=int, default=63269)
    init_parser.add_argument(
        "--runtime-workspace-root",
        default="/vllm-workspace",
    )
    init_parser.add_argument("--vllm-origin-url")
    init_parser.add_argument("--vllm-ascend-origin-url")
    reset_parser = subparsers.add_parser(
        "reset",
        help="prepare or execute destructive workspace teardown",
    )
    reset_subparsers = reset_parser.add_subparsers(dest="reset_command", required=True)
    reset_subparsers.add_parser(
        "prepare",
        help="record reset authorization and preview destructive scope",
    )
    reset_execute_parser = reset_subparsers.add_parser(
        "execute",
        help="execute destructive workspace teardown after confirmation",
    )
    reset_execute_parser.add_argument("--confirmation-id")
    reset_execute_parser.add_argument("--confirm")

    machine_parser = subparsers.add_parser(
        "machine",
        help="attach, verify, list, or remove a managed machine for this workspace",
    )
    machine_subparsers = machine_parser.add_subparsers(dest="machine_command", required=True)
    machine_subparsers.add_parser("list", help="list machines attached to this workspace")
    machine_add_parser = machine_subparsers.add_parser("add", help="attach a machine to this workspace")
    machine_add_parser.add_argument("server_name")
    machine_add_parser.add_argument("--server-host", required=True)
    machine_add_parser.add_argument("--server-user", default="root")
    machine_add_parser.add_argument("--server-port", type=int, default=22)
    machine_add_parser.add_argument("--ssh-auth-ref")
    machine_add_parser.add_argument(
        "--runtime-image",
        default="quay.nju.edu.cn/ascend/vllm-ascend:latest",
    )
    machine_add_parser.add_argument("--runtime-container", default="vaws-workspace")
    machine_add_parser.add_argument("--runtime-ssh-port", type=int, default=63269)
    machine_add_parser.add_argument(
        "--runtime-workspace-root",
        default="/vllm-workspace",
    )
    machine_add_parser.add_argument(
        "--runtime-bootstrap-mode",
        default="host-then-container",
    )
    machine_verify_parser = machine_subparsers.add_parser("verify", help="verify whether a machine is ready")
    machine_verify_parser.add_argument("server_name")
    machine_remove_parser = machine_subparsers.add_parser("remove", help="remove a machine from this workspace")
    machine_remove_parser.add_argument("server_name")

    serving_parser = subparsers.add_parser(
        "serving",
        help="start, inspect, list, or stop explicit model services",
    )
    serving_subparsers = serving_parser.add_subparsers(dest="serving_command", required=True)
    serving_subparsers.add_parser("list", help="list explicit services in this workspace")
    serving_start_parser = serving_subparsers.add_parser("start", help="start an explicit service")
    serving_start_parser.add_argument("--server-name", required=True)
    serving_start_parser.add_argument("--preset", required=True)
    serving_start_parser.add_argument("--weights-path", required=True)
    serving_start_parser.add_argument("--api-key-env")
    serving_status_parser = serving_subparsers.add_parser("status", help="inspect one service")
    serving_status_parser.add_argument("service_id")
    serving_stop_parser = serving_subparsers.add_parser("stop", help="stop one explicit service")
    serving_stop_parser.add_argument("service_id")

    benchmark_parser = subparsers.add_parser(
        "benchmark",
        help="run benchmark workflows on a ready machine",
    )
    benchmark_subparsers = benchmark_parser.add_subparsers(
        dest="benchmark_command",
        required=True,
    )
    benchmark_run_parser = benchmark_subparsers.add_parser("run")
    benchmark_run_parser.add_argument("--server-name", required=True)
    benchmark_run_parser.add_argument("--preset", required=True)
    benchmark_run_parser.add_argument("--weights-path")
    benchmark_run_parser.add_argument("--service-id")

    internal_parser = subparsers.add_parser("internal", help=argparse.SUPPRESS)
    internal_subparsers = internal_parser.add_subparsers(
        dest="internal_command",
        required=True,
    )
    internal_session_parser = internal_subparsers.add_parser("session", help=argparse.SUPPRESS)
    internal_session_subparsers = internal_session_parser.add_subparsers(
        dest="session_command",
        required=True,
    )
    internal_session_create_parser = internal_session_subparsers.add_parser(
        "create", help=argparse.SUPPRESS
    )
    internal_session_create_parser.add_argument("session_name")
    internal_session_switch_parser = internal_session_subparsers.add_parser(
        "switch", help=argparse.SUPPRESS
    )
    internal_session_switch_parser.add_argument("session_name")
    internal_session_subparsers.add_parser("status", help=argparse.SUPPRESS)

    internal_acceptance_parser = internal_subparsers.add_parser("acceptance", help=argparse.SUPPRESS)
    internal_acceptance_subparsers = internal_acceptance_parser.add_subparsers(
        dest="acceptance_command",
        required=True,
    )
    acceptance_run_parser = internal_acceptance_subparsers.add_parser("run", help=argparse.SUPPRESS)
    acceptance_run_parser.add_argument("--server-name", required=True)
    acceptance_run_parser.add_argument("--vllm-origin-url")
    acceptance_run_parser.add_argument("--vllm-ascend-origin-url")
    acceptance_run_parser.add_argument("--vllm-upstream-tag")
    acceptance_run_parser.add_argument("--vllm-ascend-upstream-branch", default="main")
    acceptance_run_parser.add_argument("--weights-path", required=True)
    acceptance_run_parser.add_argument("--benchmark-preset", default="qwen3_5_35b_tp4_perf")

    internal_remotes_parser = internal_subparsers.add_parser("remotes", help=argparse.SUPPRESS)
    internal_remotes_subparsers = internal_remotes_parser.add_subparsers(
        dest="remotes_command",
        required=True,
    )
    internal_remotes_subparsers.add_parser("normalize", help=argparse.SUPPRESS)
    return parser


def _dispatch_internal_placeholder(_args: argparse.Namespace) -> int:
    print("internal adapter not wired yet: continue with Tasks 3-5")
    return 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = RepoPaths(root=Path.cwd())

    if args.command == "doctor":
        return doctor(paths)
    if args.command == "init":
        return run_init(paths, init_request_from_args(args))
    if args.command == "reset":
        if args.reset_command == "prepare":
            return prepare_reset(paths)
        if args.reset_command == "execute":
            return execute_reset(paths, args.confirmation_id, args.confirm)
        parser.error(f"unknown reset command: {args.reset_command}")
    if args.command == "machine":
        if args.machine_command == "list":
            return list_machines(paths)
        if args.machine_command == "add":
            return add_machine(
                paths,
                args.server_name,
                args.server_host,
                ssh_auth_ref=args.ssh_auth_ref,
                server_user=args.server_user,
                server_port=args.server_port,
                runtime_image=args.runtime_image,
                runtime_container=args.runtime_container,
                runtime_ssh_port=args.runtime_ssh_port,
                runtime_workspace_root=args.runtime_workspace_root,
                runtime_bootstrap_mode=args.runtime_bootstrap_mode,
            )
        if args.machine_command == "verify":
            return verify_machine(paths, args.server_name)
        if args.machine_command == "remove":
            return remove_machine(paths, args.server_name)
        parser.error(f"unknown machine command: {args.machine_command}")
    if args.command == "serving":
        if args.serving_command == "list":
            return list_services(paths)
        if args.serving_command == "start":
            return start_service(paths, args.server_name, args.preset, args.weights_path, args.api_key_env)
        if args.serving_command == "status":
            return status_service(paths, args.service_id)
        if args.serving_command == "stop":
            return stop_service(paths, args.service_id)
        parser.error(f"unknown serving command: {args.serving_command}")
    if args.command == "benchmark" and args.benchmark_command == "run":
        return run_benchmark(paths, args.server_name, args.preset, args.weights_path, args.service_id)
    if args.command == "internal":
        if args.internal_command == "session" and args.session_command == "create":
            return create_session(paths, args.session_name)
        if args.internal_command == "session" and args.session_command == "switch":
            return switch_session(paths, args.session_name)
        if args.internal_command == "session" and args.session_command == "status":
            return status_session(paths)
        if args.internal_command == "acceptance" and args.acceptance_command == "run":
            return run_acceptance(
                paths.root,
                AcceptanceRequest(
                    server_name=args.server_name,
                    vllm_origin_url=args.vllm_origin_url,
                    vllm_ascend_origin_url=args.vllm_ascend_origin_url,
                    vllm_upstream_tag=args.vllm_upstream_tag,
                    vllm_ascend_upstream_branch=args.vllm_ascend_upstream_branch,
                    benchmark_preset=args.benchmark_preset,
                    weights_path=args.weights_path,
                ),
            )
        if args.internal_command == "remotes" and args.remotes_command == "normalize":
            return normalize_remotes(paths)
        return _dispatch_internal_placeholder(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
