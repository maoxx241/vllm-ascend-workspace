#!/usr/bin/env python3
"""P19: the scaffold knowledge CLI accepts reader coordinates."""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
QUERY = ROOT / ".agents" / "scripts" / "knowledge_query.py"
A3_ONLY = "5329d20b-8e64-4de9-ac53-ba766cf096eb"
A3_FINGERPRINT = "graph mode dump manifest far fewer records than eager"


def load_query_module():
    name = "_knowledge_query_p19"
    spec = importlib.util.spec_from_file_location(name, QUERY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


query_cli = load_query_module()


def _run(*argv: str) -> tuple[int, dict, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = query_cli.main(
            [
                "--knowledge-dir",
                str(ROOT / ".agents" / "knowledge"),
                "--candidate-dir",
                str(ROOT / ".vaws-local" / "knowledge" / "candidate"),
                *argv,
            ]
        )
    return code, json.loads(out.getvalue()), err.getvalue()


class ReaderCoordinateCliTests(unittest.TestCase):
    def test_soc_mismatch_withholds_the_a3_entry(self) -> None:
        code, payload, _ = _run(
            "--query",
            A3_FINGERPRINT,
            "--layer",
            "project",
            "--include-unverified",
            "--soc",
            "NotA3",
        )
        self.assertEqual(0, code)
        uuids = [item["uuid"] for item in payload["results"]]
        self.assertNotIn(A3_ONLY, uuids)
        self.assertEqual("NotA3", payload["reader_coordinate"]["supplied"]["soc"])

    def test_matching_soc_returns_the_entry_and_populates_the_coordinate(self) -> None:
        code, payload, _ = _run(
            "--query",
            A3_FINGERPRINT,
            "--layer",
            "project",
            "--include-unverified",
            "--soc",
            "A3",
        )
        self.assertEqual(0, code)
        uuids = [item["uuid"] for item in payload["results"]]
        self.assertIn(A3_ONLY, uuids)
        supplied = payload["reader_coordinate"]["supplied"]
        self.assertEqual("A3", supplied["soc"])
        self.assertIn("cann", payload["reader_coordinate"]["unsupplied_expected_dimensions"])

    def test_run_manifest_fills_derivable_dimensions(self) -> None:
        from vaws_coordinator.run_manifest import new_manifest, write_manifest

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "run-manifest.json"
            write_manifest(
                path,
                new_manifest(
                    run_type="debug",
                    run_id="debug-p19-reader",
                    workspace_root=ROOT,
                    environment={"soc": "A3", "cann": "8.2.RC1"},
                    created_at="2026-09-09T00:00:00Z",
                ),
            )
            code, payload, _ = _run(
                "--query",
                A3_FINGERPRINT,
                "--layer",
                "project",
                "--include-unverified",
                "--run-manifest",
                str(path),
            )
        self.assertEqual(0, code)
        supplied = payload["reader_coordinate"]["supplied"]
        self.assertEqual("A3", supplied["soc"])
        self.assertEqual("8.2.RC1", supplied["cann"])
        self.assertNotIn("torch", supplied)
        self.assertIn(A3_ONLY, [item["uuid"] for item in payload["results"]])


class ReaderCoordinateGuardTests(unittest.TestCase):
    def test_old_package_is_a_hard_failure(self) -> None:
        with mock.patch.object(query_cli, "version", return_value="0.1.3"):
            with self.assertRaises(SystemExit) as ctx:
                query_cli.require_knowledge_reader_cli()
        self.assertIn("too old", str(ctx.exception))
        self.assertIn("0.1.4", str(ctx.exception))
        self.assertIn("uv sync", str(ctx.exception))

    def test_missing_package_is_a_hard_failure(self) -> None:
        with mock.patch.object(
            query_cli, "version", side_effect=query_cli.PackageNotFoundError("vaws-knowledge")
        ):
            with self.assertRaises(SystemExit) as ctx:
                query_cli.require_knowledge_reader_cli()
        self.assertIn("not installed", str(ctx.exception))
        self.assertIn("uv sync", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
