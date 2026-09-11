"""Workspace capability probe delegates to the installed knowledge corpus."""
import sys
import unittest
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
import vaws_capability as capability
from vaws_dependency import REMEDY
SOURCE_REPO = "vllm-ascend-workspace/vaws-knowledge"

class InstalledCorpusTests(unittest.TestCase):
    def test_probe_uses_the_installed_package(self) -> None:
        from vaws_knowledge import corpus as packaged

        inspection = capability.probe_shared()
        self.assertEqual(inspection["status"], capability.SHARED_AVAILABLE)
        self.assertEqual(inspection["source_repo"], SOURCE_REPO)
        self.assertEqual(inspection["source_ref"], packaged.installed_commit())
        self.assertTrue(Path(inspection["path"]).is_dir())
        self.assertGreaterEqual(len(inspection["documents"]), 1)

    def test_missing_corpus_is_absent_with_uv_sync_remedy(self) -> None:
        missing = Path("/no/such/vaws-knowledge-corpus")
        with mock.patch("vaws_knowledge.corpus.corpus_root", return_value=missing):
            inspection = capability.probe_shared()
        self.assertEqual(inspection["status"], capability.SHARED_ABSENT)
        self.assertEqual(inspection["remedy"], REMEDY)
