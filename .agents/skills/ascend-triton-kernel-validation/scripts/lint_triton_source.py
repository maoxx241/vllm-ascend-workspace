#!/usr/bin/env python3
"""Advisory source lint for the ModelNew.forward Triton wrapper convention.

This scan cannot prove runtime reachability and does not cover arbitrary
function wrappers, aliases or imported launchers. Review its findings alongside
actual candidate execution; they are not a report-generation gate.
"""

from __future__ import annotations

import sys

import argparse
import ast
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


ALLOWED_TORCH_CALLS = {
    "torch.empty",
    "torch.empty_like",
    "torch.zeros",
    "torch.zeros_like",
    "torch.ones",
    "torch.ones_like",
    "torch.full",
    "torch.full_like",
    "torch.npu.current_device",
    "torch.npu.device",
}
FORBIDDEN_METHODS = {
    "add", "sub", "mul", "div", "matmul", "mm", "bmm", "sum", "mean",
    "max", "min", "softmax", "exp", "log", "sqrt", "pow", "where",
}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return None


def _decorator_is_triton_jit(node: ast.AST) -> bool:
    name = _dotted(node.func if isinstance(node, ast.Call) else node)
    return name in {"triton.jit", "jit"}


def _call_target(node: ast.Call) -> str | None:
    function = node.func
    if isinstance(function, ast.Subscript):
        function = function.value
    name = _dotted(function)
    return name.split(".")[-1] if name else None


def lint_tree(tree: ast.Module) -> dict[str, Any]:
    kernels = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_decorator_is_triton_jit(item) for item in node.decorator_list)
    }
    functions: dict[str, ast.AST] = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    forward: ast.AST | None = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "ModelNew":
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[item.name] = item
                    if item.name == "forward":
                        forward = item
    reachable: set[str] = set()
    kernel_calls: set[str] = set()
    violations: list[dict[str, Any]] = []

    def walk_function(name: str, node: ast.AST) -> None:
        if name in reachable:
            return
        reachable.add(name)
        for call in (item for item in ast.walk(node) if isinstance(item, ast.Call)):
            target = _call_target(call)
            if target in kernels:
                kernel_calls.add(target)
            if target in functions and target != name:
                walk_function(target, functions[target])
            dotted = _dotted(call.func)
            if dotted and (dotted.startswith("torch.") or dotted.startswith("F.")):
                if dotted not in ALLOWED_TORCH_CALLS:
                    violations.append({"line": call.lineno, "call": dotted})
            elif target in FORBIDDEN_METHODS:
                violations.append({"line": call.lineno, "call": dotted or target})

    if forward is not None:
        walk_function("forward", forward)
    return {"status": "inspected" if forward is not None else "out_of_scope",
            "advisory": True, "scope": "ModelNew.forward and same-file helpers; syntactic calls only",
            "runtime_execution": "unknown", "pytorch_fallback": "unknown",
            "declared_kernels": sorted(kernels), "kernel_call_names": sorted(kernel_calls),
            "potential_compute_calls": violations, "inspected_functions": sorted(reachable)}


def lint_file(path: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return {"status": "unreadable", "advisory": True, "runtime_execution": "unknown",
                "pytorch_fallback": "unknown", "error": f"{type(exc).__name__}: {exc}"}
    return lint_tree(tree)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = lint_file(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
    return 2 if result["status"] == "unreadable" else 0


if __name__ == "__main__":
    raise SystemExit(main())
