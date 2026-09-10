#!/usr/bin/env python3
"""The published capture example must stay a legal ``--input`` payload."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = (
    ROOT
    / ".agents"
    / "skills"
    / "curate-workspace-knowledge"
    / "references"
    / "capture-candidate.example.json"
)
CAPTURE = ROOT / ".agents" / "scripts" / "knowledge_capture.py"


class CaptureExampleTests(unittest.TestCase):
    def test_example_is_accepted_by_capture(self) -> None:
        self.assertTrue(EXAMPLE.is_file())
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            knowledge.mkdir()
            env = os.environ.copy()
            env["VAWS_KNOWLEDGE_BACKEND"] = "memory"
            env["VAWS_SKIP_VENV_REEXEC"] = "1"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(CAPTURE),
                    "--input",
                    str(EXAMPLE),
                    "--candidate-dir",
                    str(root / "candidate"),
                    "--knowledge-dir",
                    str(knowledge),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["status"], "passed")
            self.assertTrue(Path(payload["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
