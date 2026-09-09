"""Shared layer reads the installed vaws-knowledge corpus."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "lib"))

import vaws_capability as capability  # noqa: E402
import vaws_knowledge_v2 as v2  # noqa: E402
from vaws_knowledge.server.query import query  # noqa: E402
from vaws_knowledge_service import service_config  # noqa: E402

from test_knowledge_v2 import sample_entry, verification  # noqa: E402

QUERY = "bootstrap precondition not satisfied"
SOURCE_REPO = "vllm-ascend-workspace/vaws-knowledge"


def verified_entry(**overrides: object) -> dict:
    entry = sample_entry()
    entry["status"] = "verified"
    entry["confidence"] = "high"
    entry["verification"] = verification()
    entry.update(overrides)
    return v2.with_content_hash(entry)


def write_document(directory: Path, name: str, *, layer: str, entries: list[dict]) -> Path:
    document = v2.new_document("known-failure-signatures", layer=layer, now="2026-09-07")
    document["entries"] = [v2.with_content_hash(entry) for entry in entries]
    path = directory / name
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


class InstalledCorpusTests(unittest.TestCase):
    def test_probe_uses_the_installed_package(self) -> None:
        from vaws_knowledge import corpus as packaged

        inspection = capability.probe_shared()
        self.assertEqual(inspection["status"], capability.SHARED_AVAILABLE)
        self.assertEqual(inspection["source_repo"], SOURCE_REPO)
        self.assertEqual(inspection["source_ref"], packaged.installed_commit())
        self.assertTrue(Path(inspection["path"]).is_dir())
        self.assertGreaterEqual(len(inspection["documents"]), 1)

    def test_query_hits_a_packaged_measurement(self) -> None:
        from vaws_knowledge.corpus import installed_commit

        payload = query(
            service_config(ROOT),
            text="Ascend910B4",
            layers=("shared",),
            bodies=("measurement",),
            include_unverified=True,
        ).to_dict()
        self.assertIn("shared", payload["layers_available"])
        self.assertTrue(payload["results"])
        self.assertEqual({match["layer"] for match in payload["results"]}, {"shared"})
        self.assertEqual(payload["source_repo"], SOURCE_REPO)
        self.assertEqual(payload["source_ref"], installed_commit())
        self.assertEqual(payload["results"][0]["body"], "measurement")
        self.assertFalse(payload["degraded"] and "shared" in payload["layers_absent"])

    def test_missing_corpus_is_absent_with_uv_sync_remedy(self) -> None:
        missing = Path("/no/such/vaws-knowledge-corpus")
        with mock.patch("vaws_knowledge.corpus.corpus_root", return_value=missing):
            inspection = capability.probe_shared()
        self.assertEqual(inspection["status"], capability.SHARED_ABSENT)
        self.assertEqual(inspection["remedy"], "uv sync")


class MonkeypatchedCorpusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.corpus = self.root / "corpus"
        verified = self.corpus / "verified"
        unverified = self.corpus / "unverified"
        verified.mkdir(parents=True)
        unverified.mkdir()
        write_document(verified, "rules.yaml", layer="verified", entries=[verified_entry()])
        unverified_entry = sample_entry()
        unverified_entry["slug"] = "sample-unverified"
        unverified_entry["uuid"] = v2.derived_uuid(
            "owner/fork", "known-failure-signatures", "sample-unverified"
        )
        write_document(
            unverified,
            "unverified.yaml",
            layer="unverified",
            entries=[unverified_entry],
        )
        self.patches = [
            mock.patch("vaws_knowledge.corpus.corpus_root", return_value=self.corpus),
            mock.patch(
                "vaws_knowledge.corpus.installed_commit",
                return_value="0123456789abcdef0123456789abcdef01234567",
            ),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self) -> None:
        for patch in reversed(self.patches):
            patch.stop()
        self.temp.cleanup()

    def _query(self, **kwargs):
        config = service_config(self.root)
        return query(config, text=QUERY, layers=("shared",), **kwargs).to_dict()

    def test_query_still_hides_unverified_unless_opted_in(self) -> None:
        default = self._query()
        self.assertEqual({match["slug"] for match in default["results"]}, {"sample"})
        opted = self._query(include_unverified=True)
        self.assertEqual(
            {match["slug"] for match in opted["results"]},
            {"sample", "sample-unverified"},
        )


if __name__ == "__main__":
    unittest.main()
