#!/usr/bin/env python3
"""Replay one captured operator call standalone and compare it to a reference.

This closes the loop for single-operator localization. ``dump_probe.capture_inputs``
saves a named input set for one operator call inside the running service; this
script feeds that exact input set to a candidate implementation and to a
reference implementation outside the service, then writes both results back in
probe payload format so ``scripts/dump_compare.py tensors`` produces the
metrics.

Run it on the machine that has the NPU and the dump, and keep it here as an
asset rather than a maintained library: an operator reproducer is meant to be
edited for the call you are chasing.

Inspect what was captured::

    python replay_op.py --dump req-abc-rank0.pt --list

Replay against a reference::

    python replay_op.py \\
        --dump req-abc-rank0.pt \\
        --stage gmm1 \\
        --candidate torch_npu.npu_grouped_matmul \\
        --reference my_refs.grouped_matmul_reference \\
        --out-dir /tmp/replay-gmm1

    python dump_compare.py tensors \\
        --left /tmp/replay-gmm1/reference.pt \\
        --right /tmp/replay-gmm1/candidate.pt

By default the captured names are passed as keyword arguments. Use
``--arg-order`` when the operator only accepts positional arguments.
"""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path
from typing import Any

PROBE_ID = "ascend-tensor-dump/1"


def emit_progress(phase: str, **details: Any) -> None:
    print(json.dumps({"phase": phase, **details}, ensure_ascii=False), file=sys.stderr)


def resolve(path: str) -> Any:
    """Resolve ``pkg.mod.attr`` or ``pkg.mod.attr.sub`` to a callable."""
    parts = path.split(".")
    for split in range(len(parts) - 1, 0, -1):
        module_name = ".".join(parts[:split])
        try:
            target: Any = importlib.import_module(module_name)
        except ImportError:
            continue
        for attribute in parts[split:]:
            target = getattr(target, attribute)
        return target
    raise SystemExit(f"cannot resolve callable: {path}")


def load_input_sets(dump: Path) -> dict[str, dict[str, Any]]:
    import torch

    payload = torch.load(dump, map_location="cpu", weights_only=False)
    sets: dict[str, dict[str, Any]] = {}
    for stage, inputs in payload.get("inputs") or []:
        sets[str(stage)] = inputs
    return sets


def to_device(value: Any, device: str) -> Any:
    import torch

    if isinstance(value, torch.Tensor):
        return value.to(device)
    return value


def as_tensor_list(result: Any) -> list[tuple[str, Any]]:
    """Normalize whatever the operator returned into named tensors."""
    import torch

    if isinstance(result, torch.Tensor):
        return [("out", result)]
    if isinstance(result, dict):
        return [(str(name), value) for name, value in result.items()]
    if isinstance(result, (list, tuple)):
        return [(f"out{index}", value) for index, value in enumerate(result)]
    return [("out", result)]


def save_payload(path: Path, stage: str, result: Any) -> None:
    import torch

    tensors = []
    extras: dict[str, Any] = {}
    for name, value in as_tensor_list(result):
        if isinstance(value, torch.Tensor):
            tensors.append((f"{stage}:{name}", value.detach().cpu()))
        else:
            extras[name] = repr(value)
    torch.save(
        {
            "probe": PROBE_ID,
            "label": f"replay-{stage}",
            "rank": 0,
            "metadata": {"replay": True, "non_tensor_outputs": extras},
            "tensors": tensors,
            "inputs": [],
            "graph_slots": {},
        },
        path,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", required=True, type=Path)
    parser.add_argument("--stage")
    parser.add_argument("--candidate")
    parser.add_argument("--reference")
    parser.add_argument("--out-dir", type=Path)
    parser.add_argument("--device", default="npu")
    parser.add_argument(
        "--arg-order",
        help="comma-separated input names to pass positionally instead of by keyword",
    )
    parser.add_argument(
        "--drop",
        default="",
        help="comma-separated captured names to omit from the call",
    )
    parser.add_argument(
        "--list", action="store_true", help="print captured stages and input names"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    input_sets = load_input_sets(args.dump)

    if args.list or not args.stage:
        print(
            json.dumps(
                {
                    "status": "ok",
                    "dump": str(args.dump),
                    "stages": {
                        stage: sorted(inputs) for stage, inputs in input_sets.items()
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if args.stage not in input_sets:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": f"stage {args.stage!r} not in dump",
                    "available": sorted(input_sets),
                },
                ensure_ascii=False,
            )
        )
        return 2
    if not args.candidate or not args.out_dir:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "--candidate and --out-dir are required to replay",
                },
                ensure_ascii=False,
            )
        )
        return 2

    dropped = {name.strip() for name in args.drop.split(",") if name.strip()}
    captured = {
        name: to_device(value, args.device)
        for name, value in input_sets[args.stage].items()
        if name not in dropped
    }

    if args.arg_order:
        names = [name.strip() for name in args.arg_order.split(",") if name.strip()]
        positional = [captured[name] for name in names]
        keywords = {
            name: value for name, value in captured.items() if name not in set(names)
        }
    else:
        positional = []
        keywords = captured

    args.out_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}

    for role, dotted in (("candidate", args.candidate), ("reference", args.reference)):
        if not dotted:
            continue
        emit_progress("replay", role=role, target=dotted, stage=args.stage)
        function = resolve(dotted)
        result = function(*positional, **keywords)
        path = args.out_dir / f"{role}.pt"
        save_payload(path, args.stage, result)
        written[role] = str(path)

    print(
        json.dumps(
            {
                "status": "ok",
                "stage": args.stage,
                "inputs": sorted(captured),
                "written": written,
                "next": (
                    "dump_compare.py tensors --left "
                    f"{written.get('reference', '<reference.pt>')} --right "
                    f"{written.get('candidate', '<candidate.pt>')}"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
