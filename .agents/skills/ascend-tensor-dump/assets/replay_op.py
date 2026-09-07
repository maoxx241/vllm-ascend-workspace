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

Inspect what was captured, including how many times each stage was hit::

    python replay_op.py --dump req-abc-rank0.pt --list

Replay against a reference. A stage captured once can be named directly; a
stage captured per layer needs an occurrence, as ``gmm1#3``::

    python replay_op.py \\
        --dump req-abc-rank0.pt \\
        --stage gmm1#3 \\
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
    """Load every captured input set, keyed ``stage#occurrence``.

    One stage name is usually captured many times in a single forward -- one
    per layer, for instance. Keying by bare stage name would silently keep
    only the last one, so the occurrence index is always part of the key.
    """
    import torch

    payload = torch.load(dump, map_location="cpu", weights_only=False)
    sets: dict[str, dict[str, Any]] = {}
    seen: dict[str, int] = {}
    for stage, inputs in payload.get("inputs") or []:
        name = str(stage)
        index = seen.get(name, 0)
        seen[name] = index + 1
        sets[f"{name}#{index}"] = inputs
    return sets


def resolve_stage(requested: str, input_sets: dict[str, dict[str, Any]]) -> str:
    """Accept an exact ``stage#N`` key, or a bare stage name when unambiguous."""
    if requested in input_sets:
        return requested
    candidates = [key for key in input_sets if key.rsplit("#", 1)[0] == requested]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise LookupError(f"stage {requested!r} not in dump")
    raise LookupError(
        f"stage {requested!r} was captured {len(candidates)} times; "
        f"address one with {requested}#0 .. {requested}#{len(candidates) - 1}"
    )


def summarize_stages(input_sets: dict[str, dict[str, Any]]) -> dict[str, Any]:
    grouped: dict[str, dict[str, Any]] = {}
    for key, inputs in input_sets.items():
        name = key.rsplit("#", 1)[0]
        entry = grouped.setdefault(
            name, {"occurrences": 0, "inputs": sorted(inputs), "addressable_as": ""}
        )
        entry["occurrences"] += 1
    for name, entry in grouped.items():
        last = entry["occurrences"] - 1
        entry["addressable_as"] = f"{name}#0 .. {name}#{last}" if last else f"{name}#0"
    return grouped


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
    parser.add_argument(
        "--stage",
        help=(
            "captured stage to replay; use stage#N to pick one occurrence when "
            "the stage was captured more than once"
        ),
    )
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
                    "stages": summarize_stages(input_sets),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    try:
        stage_key = resolve_stage(args.stage, input_sets)
    except LookupError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": str(exc),
                    "available": summarize_stages(input_sets),
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
        for name, value in input_sets[stage_key].items()
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
        emit_progress("replay", role=role, target=dotted, stage=stage_key)
        function = resolve(dotted)
        result = function(*positional, **keywords)
        path = args.out_dir / f"{role}.pt"
        save_payload(path, stage_key, result)
        written[role] = str(path)

    print(
        json.dumps(
            {
                "status": "ok",
                "stage": stage_key,
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
