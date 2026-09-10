#!/usr/bin/env python3
"""Workspace knowledge query CLI: text in, degraded-or-results out."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
QUERY = ROOT / ".agents" / "scripts" / "knowledge_query.py"


def load_query_module():
    os.environ["VAWS_SKIP_VENV_REEXEC"] = "1"
    name = "_knowledge_query_cli_test"
    spec = importlib.util.spec_from_file_location(name, QUERY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class QueryCliTests(unittest.TestCase):
    def test_missing_text_is_usage(self) -> None:
        module = load_query_module()
        with self.assertRaises(SystemExit):
            module.main([])

    def test_query_path_calls_engine(self) -> None:
        module = load_query_module()
        fake = mock.Mock()
        fake.to_dict.return_value = {
            "count": 0,
            "results": [],
            "unavailable": False,
            "degraded": False,
            "absent_fact_semantics": "unknown",
            "no_result_meaning": "UNKNOWN",
        }
        with mock.patch.object(module, "require_knowledge_reader_cli"):
            with mock.patch.object(module, "query", return_value=fake):
                with mock.patch.object(module, "service_config"):
                    out, err = io.StringIO(), io.StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = module.main(["--query", "graph replay"])
        self.assertEqual(0, code)
        payload = json.loads(out.getvalue())
        self.assertEqual(0, payload["count"])
