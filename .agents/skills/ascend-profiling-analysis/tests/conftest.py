"""Test fixtures: put ``scripts/`` on sys.path so test files can import
``_common`` and ``ascend_profile`` as top-level modules without installing
the package.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
_COMMON_FILE = SCRIPTS_DIR / "_common.py"


def _claim_skill_imports() -> None:
    """Make this skill's ``_common`` win in a shared pytest process.

    Collection (and other skill) suites also expose a top-level ``_common``.
    Pytest may load this conftest before those suites finish importing, so a
    one-shot ``sys.path`` tweak at module import is not enough.
    """
    scripts = str(SCRIPTS_DIR)
    while scripts in sys.path:
        sys.path.remove(scripts)
    sys.path.insert(0, scripts)

    cached = sys.modules.get("_common")
    cached_file = Path(getattr(cached, "__file__", "") or "").resolve()
    if cached is not None and cached_file == _COMMON_FILE.resolve():
        return

    sys.modules.pop("_common", None)
    spec = importlib.util.spec_from_file_location("_common", _COMMON_FILE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["_common"] = module
    spec.loader.exec_module(module)

    for name in ("profile_analyze", "profile_sweep"):
        loaded = sys.modules.get(name)
        common = getattr(loaded, "common", None) if loaded is not None else None
        if loaded is not None and (
            common is None or not hasattr(common, "REQUIRED_SINGLE_ARTIFACTS")
        ):
            sys.modules.pop(name, None)


_claim_skill_imports()


def pytest_configure(config) -> None:
    del config
    _claim_skill_imports()


def pytest_collect_file(file_path, parent):
    del file_path, parent
    _claim_skill_imports()
    return None
