#!/usr/bin/env python3
"""Statically reject missing Triton launches and PyTorch computation fallback."""

from __future__ import annotations

import argparse
import ast
import json
from pathlib import Path
from typing import Any

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


def analyze_tree(tree: ast.Module) -> dict[str, Any]:
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
    checks = {
        "triton_kernel_exists": {"passed": bool(kernels), "kernels": sorted(kernels)},
        "kernel_called_from_forward": {"passed": bool(kernel_calls), "called": sorted(kernel_calls)},
        "no_pytorch_fallback": {"passed": not violations, "violations": violations},
    }
    valid = all(item["passed"] for item in checks.values())
    return {"valid": valid, "checks": checks, "reachable_functions": sorted(reachable)}


def analyze_file(path: Path) -> dict[str, Any]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return {"valid": False, "error": f"{type(exc).__name__}: {exc}", "checks": {}}
    return analyze_tree(tree)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = analyze_file(args.path)
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
