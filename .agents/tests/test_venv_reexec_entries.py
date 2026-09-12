"""Tests must not disable interpreter selection for subsequent test modules.

Actual bootstrap handoff runs through the public entry in CI. Receipt selection,
flags, nested children and native owner behavior have dedicated process tests.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKIP_ENV = "VAWS_SKIP_VENV_REEXEC"
TEST_FILE_RE = re.compile(r"^(test_.*|.*_test|selftest_.*|conftest)\.py$")


def _const_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _dict_mentions_skip(node: ast.AST) -> bool:
    return isinstance(node, ast.Dict) and any(_const_str(key) == SKIP_ENV for key in node.keys)


def _call_writes_skip_on_os_environ(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or not _is_os_environ(func.value):
        return False
    if func.attr == "setdefault":
        first = node.args[0] if node.args else None
        return _const_str(first) == SKIP_ENV or (
            isinstance(first, ast.Name) and first.id in {"SKIP_ENV", "VAWS_SKIP_VENV_REEXEC"}
        )
    if func.attr == "update":
        if node.args and _dict_mentions_skip(node.args[0]):
            return True
        return any(
            (kw.arg is None and _dict_mentions_skip(kw.value)) or kw.arg == SKIP_ENV
            for kw in node.keywords
        )
    if func.attr in {"__setitem__", "pop"}:
        return bool(node.args) and _const_str(node.args[0]) == SKIP_ENV
    return False


def _target_writes_skip(target: ast.AST) -> bool:
    return (
        isinstance(target, ast.Subscript)
        and _is_os_environ(target.value)
        and _const_str(target.slice) == SKIP_ENV
    )


def import_time_os_environ_skip_writes(tree: ast.AST) -> list[int]:
    """Line numbers of import-time writes of SKIP_ENV onto ``os.environ``.

    Function bodies are ignored. ``patch.dict(os.environ, ...)`` is not a
    write of ``os.environ`` and is allowed — it restores on exit.
    """
    hits: list[int] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self._fn = 0

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._fn += 1
            self.generic_visit(node)
            self._fn -= 1

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Call(self, node: ast.Call) -> None:
            if self._fn == 0 and _call_writes_skip_on_os_environ(node):
                hits.append(node.lineno)
            self.generic_visit(node)

        def visit_Assign(self, node: ast.Assign) -> None:
            if self._fn == 0 and any(_target_writes_skip(t) for t in node.targets):
                hits.append(node.lineno)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if self._fn == 0 and _target_writes_skip(node.target):
                hits.append(node.lineno)
            self.generic_visit(node)

    Visitor().visit(tree)
    return hits


def _agents_test_py_files() -> list[Path]:
    agents = ROOT / ".agents"
    found: list[Path] = []
    for path in agents.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if any(part in {".venv", "__pycache__", ".git"} for part in rel.parts):
            continue
        if "tests" in rel.parts[:-1] or TEST_FILE_RE.match(path.name):
            found.append(path)
    return found


class SkipEnvImportIsolationTests(unittest.TestCase):
    def test_detector_flags_module_level_setdefault_only(self) -> None:
        bad = ast.parse(f'os.environ.setdefault("{SKIP_ENV}", "1")\n')
        self.assertEqual(import_time_os_environ_skip_writes(bad), [1])
        assign = ast.parse(f'os.environ["{SKIP_ENV}"] = "1"\n')
        self.assertEqual(import_time_os_environ_skip_writes(assign), [1])
        update = ast.parse(f'os.environ.update({{"{SKIP_ENV}": "1"}})\n')
        self.assertEqual(import_time_os_environ_skip_writes(update), [1])
        inside_fn = ast.parse(f'def f():\n    os.environ.setdefault("{SKIP_ENV}", "1")\n')
        self.assertEqual(import_time_os_environ_skip_writes(inside_fn), [])
        child_env = ast.parse(f'env = {{}}\nenv["{SKIP_ENV}"] = "1"\n')
        self.assertEqual(import_time_os_environ_skip_writes(child_env), [])
        scoped = ast.parse(
            f'with mock.patch.dict(os.environ, {{"{SKIP_ENV}": "1"}}):\n    pass\n'
        )
        self.assertEqual(import_time_os_environ_skip_writes(scoped), [])

    def test_tracked_tests_do_not_set_skip_env_at_import_time(self) -> None:
        leaks: list[str] = []
        for path in _agents_test_py_files():
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            except (SyntaxError, UnicodeDecodeError):
                continue
            lines = import_time_os_environ_skip_writes(tree)
            if lines:
                rel = path.relative_to(ROOT).as_posix()
                leaks.append(f"{rel}:{','.join(str(n) for n in lines)}")
        self.assertEqual(leaks, [])


if __name__ == "__main__":
    unittest.main()
