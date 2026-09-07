#!/usr/bin/env python3
"""Run the pinned Phase A shared kit against the real client adapter."""

from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

PINNED_KIT = Path(
    "/Users/maoxx241/code/vllm-ascend-workspace/.vaws-local/handoff-53a0a4ea"
    "/acceptance-evidence-docs/knowledge-canonical-e04d50f7"
)
ADAPTER = PINNED_KIT.parent / "implementation_adapter.py"


class SharedKitClientTests(unittest.TestCase):
    def test_pinned_kit_exists(self) -> None:
        self.assertTrue(PINNED_KIT.is_dir(), f"missing pinned kit at {PINNED_KIT}")
        self.assertTrue(ADAPTER.is_file(), f"missing implementation adapter at {ADAPTER}")
        vectors = PINNED_KIT / "conformance" / "vectors"
        self.assertEqual(len(list(vectors.glob("*.yaml"))), 17)

    def test_shared_kit_through_real_client_adapter(self) -> None:
        runner = PINNED_KIT / "conformance" / "runner.py"
        python = sys.executable
        adapter = f"{python} {ADAPTER} {ROOT} client"
        completed = subprocess.run(
            [
                python,
                str(runner),
                "--vectors",
                str(PINNED_KIT / "conformance" / "vectors"),
                "--gate-vectors",
                str(PINNED_KIT / "conformance" / "gate_vectors"),
                "--hash-cmd",
                f"{adapter} hash",
                "--payload-cmd",
                f"{adapter} payload",
                "--schema-cmd",
                f"{adapter} schema",
                "--redaction-cmd",
                f"{adapter} redaction",
                "--input-format",
                "entry-json",
                "--gate-format",
                "document-json",
            ],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(PINNED_KIT),
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("PASS  anchor-valid-entry", completed.stdout)
        self.assertIn("PASS  nested-scope-line-trailing-whitespace", completed.stdout)
        self.assertIn("PASS  redaction-email", completed.stdout)
        self.assertIn("PASS  schema-valid-control", completed.stdout)
        self.assertIn("29 passed, 0 failed, 2 skipped, 31 total", completed.stdout)
        self.assertIn("conformance PASSED", completed.stdout)

    def test_redaction_email_vector_through_adapter(self) -> None:
        import yaml  # noqa: PLC0415

        vector = yaml.safe_load(
            (PINNED_KIT / "conformance" / "gate_vectors" / "redaction-email.yaml").read_text(
                encoding="utf-8"
            )
        )
        completed = subprocess.run(
            [sys.executable, str(ADAPTER), str(ROOT), "client", "redaction"],
            check=False,
            capture_output=True,
            text=True,
            input=json.dumps(vector["document"]),
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertEqual(completed.stdout.strip(), "reject")
        self.assertEqual(vector["expected_verdict"], "reject")


if __name__ == "__main__":
    unittest.main()
