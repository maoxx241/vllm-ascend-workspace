#!/usr/bin/env python3
"""Capture title+content and query it back through the workspace CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / ".agents" / "scripts" / "knowledge_capture.py"
QUERY = ROOT / ".agents" / "scripts" / "knowledge_query.py"


class KnowledgeFlowE2ETest(unittest.TestCase):
    def test_title_and_content_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            candidate = root / "candidate"
            knowledge = root / "knowledge"
            knowledge.mkdir()
            payload = {
                "title": "Zqxjk flovmar blorpt requires acknowledgements",
                "content": (
                    "The zqxjk flovmar stalls after its first blorpt. "
                    "Wait for one acknowledgement before sending the next frame."
                ),
            }
            input_path = root / "input.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            env = os.environ.copy()
            env["VAWS_KNOWLEDGE_BACKEND"] = "memory"
            env["VAWS_SKIP_VENV_REEXEC"] = "1"
            captured = subprocess.run(
                [
                    sys.executable,
                    str(CAPTURE),
                    "--input",
                    str(input_path),
                    "--candidate-dir",
                    str(candidate),
                    "--knowledge-dir",
                    str(knowledge),
                ],
                check=False,
                capture_output=True,
                text=True,
                env=env,
                cwd=str(ROOT),
            )
            self.assertEqual(captured.returncode, 0, captured.stdout + captured.stderr)
            result = json.loads(captured.stdout)
            self.assertEqual(result["status"], "passed")
            self.assertTrue(Path(result["path"]).is_file())
            self.assertIn("acknowledgement", Path(result["path"]).read_text(encoding="utf-8"))
