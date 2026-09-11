"""Markdown capture/query stays with the package; workspace supplies its roots."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vaws_knowledge_service import query_knowledge, service_config
from vaws_knowledge.markdown import iter_markdown_files, load_document

ROOT = Path(__file__).resolve().parents[2]


class KnowledgeFlowTests(unittest.TestCase):
    def test_package_cli_round_trip_in_isolated_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "service.json"
            config.write_text(json.dumps({"backend": "memory", "layers": {
                "candidate": {"root": str(root / "candidate")},
                "project": {"roots": [str(root / "project")]}}}), encoding="utf-8")
            def call(*args):
                result = subprocess.run([sys.executable, "-m", "vaws_knowledge", *args,
                                         "--config", str(config), "--backend", "memory"],
                                        capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return json.loads(result.stdout)
            captured = call("capture", "--title", "Zqxjk acknowledgements", "--content",
                 "The zqxjk blorpt waits for an acknowledgement before sending the next frame.")
            self.assertEqual(len(list((root / "candidate").glob("*.md"))), 1)
            result = call("query", "--ref", captured["ref"])
            self.assertIn("acknowledgement", json.dumps(result))

    def test_failed_lookup_is_not_an_empty_success(self):
        with mock.patch("vaws_knowledge.server.query.query", side_effect=OSError("index down")):
            result = query_knowledge(knowledge_dir=ROOT / ".agents/knowledge", query="failure")
        self.assertTrue(result["unavailable"])
        self.assertEqual(result["results"], [])
        self.assertIn("index down", result["index_detail"])

    def test_project_markdown_preserves_historical_uncertainty(self):
        paths = list(iter_markdown_files(ROOT / ".agents/knowledge"))
        self.assertTrue(paths)
        for path in paths:
            document = load_document(path, layer="project", root=ROOT / ".agents/knowledge")
            self.assertTrue(document.title)
            self.assertTrue(document.content)
            self.assertIn("unverified", document.content)
            self.assertIn("Recorded conditions", document.content)


if __name__ == "__main__":
    unittest.main()
