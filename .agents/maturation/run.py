#!/usr/bin/env python3
"""Run the deterministic-core maturation harness against real endpoints.

Real execution applies substrate environment defaults once, then constructs
the in-process/CLI invoker. ``--list`` and retained-evidence ``--report``
stay offline and do not require a remote-dev checkout.

Progress goes to stderr as ``__VAWS_MATURATION_PROGRESS__=<json>`` lines; the
only stdout output is one JSON report. Host identities are replaced by labels
(``host-a`` …) unless ``--reveal-hosts`` is given; the label map and all raw
evidence live under untracked ``.vaws-local/maturation/runs/<run-id>/``.

Examples::

    # every inventory host, default repetitions from operations.yaml
    python3 .agents/maturation/run.py --from-inventory

    # smoke: two repetitions of one operation on one explicit endpoint
    python3 .agents/maturation/run.py --endpoint HOST:22 --operation bash.echo --repetitions 2

    # include the managed containers and capture reproducible failures
    python3 .agents/maturation/run.py --from-inventory --include-containers --capture-knowledge

    # rebuild a report from retained evidence without touching any host
    python3 .agents/maturation/run.py --report <run-id> --capture-knowledge
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parents[1]
if str(AGENTS_DIR) not in sys.path:
    sys.path.insert(0, str(AGENTS_DIR))
LIB_DIR = AGENTS_DIR / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from maturation.evidence import DEFAULT_EVIDENCE_ROOT  # noqa: E402
from maturation.invoke import RemoteDevUnavailable, RemoteDevInvoker, apply_real_execution_environment  # noqa: E402
from maturation.runner import RunConfig, emit_progress, replay, run  # noqa: E402
from maturation.spec import DEFAULT_OPERATIONS_PATH, SpecError, load_operation_set  # noqa: E402
from maturation.targets import TargetError, assign_labels, endpoints_from_inventory, parse_endpoint_arg  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, allow_abbrev=False)
    targets = parser.add_argument_group("targets")
    targets.add_argument("--endpoint", action="append", default=[], metavar="HOST:PORT[:KIND]", help="explicit endpoint; KIND is host (default) or container")
    targets.add_argument("--from-inventory", action="store_true", help="use every machine in the shared local inventory")
    targets.add_argument("--inventory", type=Path, help="explicit inventory path (defaults to the primary worktree's .vaws-local/machine-inventory.json)")
    targets.add_argument("--select", action="append", default=[], help="inventory alias or host to include (repeatable)")
    targets.add_argument("--include-containers", action="store_true", help="also target each machine's managed container ssh port")
    targets.add_argument("--max-hosts", type=int, help="cap the number of endpoints taken from the inventory")

    selection = parser.add_argument_group("operations")
    selection.add_argument("--operations", type=Path, default=DEFAULT_OPERATIONS_PATH, help="operations YAML document")
    selection.add_argument("--operation", action="append", default=[], help="only run these operation ids (repeatable)")
    selection.add_argument("--shape", action="append", default=[], help="only run these shapes (repeatable)")
    selection.add_argument("--class", dest="op_class", action="append", default=[], help="only run these operation classes (repeatable)")
    selection.add_argument("--repetitions", type=int, help="override repetitions for every selected operation")
    selection.add_argument("--list", action="store_true", help="print the declared operations and exit")

    execution = parser.add_argument_group("execution")
    execution.add_argument("--endpoint-parallelism", type=int, default=2, help="endpoints exercised at the same time")
    execution.add_argument("--min-free-mib", type=int, default=512, help="skip an endpoint whose scratch filesystem has less free space")
    execution.add_argument("--max-load", type=float, help="skip an endpoint whose 1-minute load average exceeds this")
    execution.add_argument("--keep-remote-scratch", action="store_true", help="do not remove the remote scratch directory")
    execution.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    execution.add_argument("--run-id")
    execution.add_argument("--report", metavar="RUN_ID", help="do not touch any host; rebuild the report from retained evidence (and capture knowledge if asked)")

    knowledge = parser.add_argument_group("knowledge")
    knowledge.add_argument("--capture-knowledge", action="store_true", help="capture reproducible failures through knowledge_capture.py")
    knowledge.add_argument("--capture-arg", action="append", default=[], help="extra argument passed through to knowledge_capture.py (repeatable)")
    knowledge.add_argument("--min-reproductions", type=int, default=2, help="failures of one signature needed before a candidate is prepared")

    parser.add_argument("--reveal-hosts", action="store_true", help="keep real host identities in the stdout report (never paste this into tracked files)")
    return parser


def resolve_endpoints(args: argparse.Namespace) -> list[dict]:
    endpoints = [parse_endpoint_arg(item) for item in args.endpoint]
    if args.from_inventory or args.inventory or args.select:
        inventory_path = args.inventory
        if inventory_path is None:
            from vaws_local_state import shared_inventory_path  # type: ignore  # noqa: PLC0415

            inventory_path = shared_inventory_path()
        try:
            document = json.loads(Path(inventory_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TargetError(f"cannot read inventory: {exc}") from exc
        endpoints.extend(endpoints_from_inventory(document, include_containers=args.include_containers, select=args.select or None))
    if args.max_hosts is not None:
        endpoints = endpoints[: max(0, args.max_hosts)]
    if not endpoints:
        raise TargetError("no endpoints: pass --endpoint HOST:PORT or --from-inventory")
    return assign_labels(endpoints)


def _install_stack_dump() -> None:
    """``kill -USR1 <pid>`` prints every thread's stack to stderr.

    A stalled multi-endpoint run is itself evidence; this makes the stall
    attributable without killing the process.
    """
    import faulthandler
    import signal

    if hasattr(signal, "SIGUSR1"):
        faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _install_stack_dump()
    try:
        operation_set = load_operation_set(args.operations)
    except SpecError as exc:
        print(json.dumps({"status": "failed", "error": f"operations: {exc}"}))
        return 2
    if args.list:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "source": str(args.operations.name),
                    "classes": {name: vars(cls) for name, cls in operation_set.classes.items()},
                    "operations": [
                        {"id": op.id, "class": op.op_class, "shape": op.shape, "tool": op.tool, "repetitions": op.repetitions, "endpoint_kinds": list(op.endpoint_kinds), "enabled": op.enabled}
                        for op in operation_set.operations
                    ],
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    config = RunConfig(
        repetitions=args.repetitions,
        operation_filter=set(args.operation) or None,
        shape_filter=set(args.shape) or None,
        class_filter=set(args.op_class) or None,
        endpoint_parallelism=args.endpoint_parallelism,
        keep_remote_scratch=args.keep_remote_scratch,
        min_free_mib=args.min_free_mib,
        max_load_1m=args.max_load,
        capture_knowledge=args.capture_knowledge,
        capture_extra_args=args.capture_arg,
        min_reproductions=args.min_reproductions,
        reveal_hosts=args.reveal_hosts,
    )
    if args.report:
        try:
            report = replay(operation_set=operation_set, config=config, evidence_root=args.evidence_root, run_id=args.report)
        except (OSError, ValueError) as exc:
            print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}))
            return 2
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report.get("status") == "ok" else 1
    try:
        endpoints = resolve_endpoints(args)
    except TargetError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 2
    try:
        apply_real_execution_environment()
    except RemoteDevUnavailable as exc:
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    emit_progress("start", f"{len(endpoints)} endpoint(s), operations from {args.operations.name}")
    try:
        report = run(
            operation_set=operation_set,
            endpoints=endpoints,
            invoker=RemoteDevInvoker(),
            config=config,
            evidence_root=args.evidence_root,
            run_id=args.run_id,
        )
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"status": "failed", "error": f"{type(exc).__name__}: {exc}"}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
