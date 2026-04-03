import argparse
import sys
from pathlib import Path
from typing import List, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.lib.config import RepoPaths
from tools.lib import (
    vaws_benchmark,
    vaws_doctor,
    vaws_machine,
    vaws_reset,
    vaws_serving,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vaws")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("doctor", help="inspect canonical workspace state and report residue")
    reset_parser = subparsers.add_parser(
        "reset",
        help="prepare or execute destructive workspace teardown",
    )
    reset_subparsers = reset_parser.add_subparsers(dest="reset_command", required=True)
    reset_subparsers.add_parser(
        "prepare",
        help="record reset authorization and preview destructive scope",
    )

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
        help="run benchmark workflows against an explicit ready service",
    )
    benchmark_subparsers = benchmark_parser.add_subparsers(
        dest="benchmark_command",
        required=True,
    )
    benchmark_run_parser = benchmark_subparsers.add_parser("run")
    benchmark_run_parser.add_argument("--server-name", required=True)
    benchmark_run_parser.add_argument("--preset", required=True)
    benchmark_run_parser.add_argument("--service-id", required=True)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = RepoPaths(root=Path.cwd())

    if args.command == "doctor":
        return vaws_doctor.run(paths)
    if args.command == "reset":
        return vaws_reset.run(paths, args)
    if args.command == "machine":
        return vaws_machine.run(paths, args)
    if args.command == "serving":
        return vaws_serving.run(paths, args)
    if args.command == "benchmark":
        return vaws_benchmark.run(paths, args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
