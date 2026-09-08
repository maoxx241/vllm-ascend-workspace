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

import vaws_knowledge_client as client  # noqa: E402
import vaws_knowledge_v2 as v2  # noqa: E402

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

        capability = client._probe_shared()
        self.assertEqual(capability["status"], client.AVAILABLE)
        self.assertEqual(capability["source_repo"], SOURCE_REPO)
        self.assertEqual(capability["source_ref"], packaged.installed_commit())
        self.assertTrue(Path(capability["path"]).is_dir())
        self.assertGreaterEqual(len(capability["documents"]), 1)

    def test_query_hits_a_packaged_measurement(self) -> None:
        from vaws_knowledge import corpus as packaged

        result = client.query(
            repo_root=ROOT,
            query="Ascend910B4",
            layers=("shared",),
            bodies=("measurement",),
            include_unverified=True,
        )
        self.assertIn("shared", result["coverage"]["layers_answered"])
        self.assertTrue(result["matches"])
        self.assertEqual({match["layer"] for match in result["matches"]}, {"shared"})
        self.assertEqual(result["matches"][0]["source_repo"], SOURCE_REPO)
        self.assertEqual(result["matches"][0]["source_ref"], packaged.installed_commit())
        self.assertEqual(result["matches"][0]["body"], "measurement")

    def test_missing_corpus_is_absent_with_uv_sync_remedy(self) -> None:
        missing = Path("/no/such/vaws-knowledge-corpus")
        with mock.patch("vaws_knowledge.corpus.corpus_root", return_value=missing):
            capability = client._probe_shared()
        self.assertEqual(capability["status"], client.ABSENT)
        self.assertEqual(capability["remedy"], "uv sync")
        with mock.patch("vaws_knowledge.corpus.corpus_root", return_value=missing):
            result = client.query(
                repo_root=ROOT,
                query=QUERY,
                layers=("shared",),
            )
        self.assertEqual(result["matches"], [])
        self.assertNotIn("shared", result["coverage"]["layers_answered"])
        self.assertEqual(result["degradation"][0]["remedy"], "uv sync")


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
        self.files = sorted(
            path
            for path in self.corpus.rglob("*.yaml")
            if path.is_file()
        )
        self.patches = [
            mock.patch("vaws_knowledge.corpus.corpus_root", return_value=self.corpus),
            mock.patch(
                "vaws_knowledge.corpus.iter_entry_files",
                return_value=self.files,
            ),
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

    def test_shared_layer_hangs_both_zones_without_filtering(self) -> None:
        entries, problems, inspection = client.load_shared_entries()
        self.assertEqual(inspection["status"], client.AVAILABLE)
        self.assertEqual(problems, [])
        slugs = {entry["slug"] for entry in entries}
        self.assertEqual(slugs, {"sample", "sample-unverified"})
        statuses = {entry["status"] for entry in entries}
        self.assertEqual(statuses, {"verified", "unverified"})

    def test_query_still_hides_unverified_unless_opted_in(self) -> None:
        default = client.query(
            repo_root=self.root,
            query=QUERY,
            layers=("shared",),
        )
        self.assertEqual({match["id"] for match in default["matches"]}, {"sample"})
        opted = client.query(
            repo_root=self.root,
            query=QUERY,
            layers=("shared",),
            include_unverified=True,
        )
        self.assertEqual(
            {match["id"] for match in opted["matches"]},
            {"sample", "sample-unverified"},
        )
        fetched = client.get_entry(
            repo_root=self.root,
            entry_id="sample",
            layers=("shared",),
        )
        self.assertEqual(fetched["layer"], "shared")
        self.assertEqual(fetched["source_repo"], SOURCE_REPO)
        self.assertEqual(
            fetched["source_ref"],
            "0123456789abcdef0123456789abcdef01234567",
        )


if __name__ == "__main__":
    unittest.main()
