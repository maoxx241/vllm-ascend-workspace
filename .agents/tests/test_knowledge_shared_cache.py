#!/usr/bin/env python3
"""Shared-cache trust boundary: import, probe, query, and get."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents" / "tests"))
sys.path.insert(0, str(ROOT / ".agents" / "lib"))

import vaws_knowledge_client as client  # noqa: E402
import vaws_knowledge_shared as shared  # noqa: E402
import vaws_knowledge_v2 as v2  # noqa: E402

from knowledge_kit import KitInvalid, KitUnconfigured, resolve_kit_root  # noqa: E402
from test_knowledge_v2 import sample_entry, verification  # noqa: E402

CACHE = ROOT / ".agents" / "scripts" / "knowledge_shared_cache.py"
SOURCE_REPO = "vllm-ascend-workspace/vaws-knowledge"
SOURCE_REF = "0123456789abcdef0123456789abcdef01234567"
QUERY = "bootstrap precondition not satisfied"


def load_importer():
    spec = importlib.util.spec_from_file_location("knowledge_shared_cache_mod", CACHE)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def digest_tree(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    found: dict[str, str] = {}
    for item in sorted(path.rglob("*")):
        if item.is_file():
            found[str(item.relative_to(path))] = hashlib.sha256(item.read_bytes()).hexdigest()
    return found


def verified_document(*entries: dict, layer: str = "verified") -> dict:
    document = v2.new_document("known-failure-signatures", layer=layer, now="2026-09-07")
    document["entries"] = [v2.with_content_hash(entry) for entry in entries]
    return document


def verified_entry(**overrides: object) -> dict:
    entry = sample_entry()
    entry["status"] = "verified"
    entry["confidence"] = "high"
    entry["verification"] = verification()
    entry.update(overrides)
    return v2.with_content_hash(entry)


class SharedCacheTrustTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "corpus" / "verified"
        self.cache = self.root / "shared"
        self.project = self.root / "project"
        self.candidates = self.root / "candidates"
        self.source.mkdir(parents=True)
        self.project.mkdir()
        self.candidates.mkdir()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def write_source(self, name: str, document: dict) -> Path:
        path = self.source / name
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return path

    def run_cache(self, *arguments: str, expect: int = 0) -> dict:
        completed = subprocess.run(
            [sys.executable, str(CACHE), "--shared-dir", str(self.cache), *arguments],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            completed.returncode,
            expect,
            completed.stdout + completed.stderr,
        )
        if not completed.stdout.strip():
            return {"stderr": completed.stderr}
        return json.loads(completed.stdout)

    def import_from(
        self,
        *extra: str,
        expect: int = 0,
        source_repo: str = SOURCE_REPO,
        source_ref: str = SOURCE_REF,
    ) -> dict:
        return self.run_cache(
            "import",
            "--from",
            str(self.source),
            "--source-repo",
            source_repo,
            "--source-ref",
            source_ref,
            *extra,
            expect=expect,
        )

    def upstream_validate_exit(self, path: Path) -> int | None:
        try:
            kit = resolve_kit_root(repo_root=ROOT, read_local_file=False)
        except (KitUnconfigured, KitInvalid):
            return None
        completed = subprocess.run(
            [sys.executable, str(kit / "tools" / "validate.py"), str(path)],
            check=False,
            capture_output=True,
            text=True,
            cwd=str(kit),
        )
        if completed.returncode == 2:
            return None
        return completed.returncode

    def query(self, **kwargs: object) -> dict:
        return client.query(
            repo_root=self.root,
            query=str(kwargs.get("query", QUERY)),
            layers=tuple(kwargs.get("layers", ("shared",))),
            include_unverified=bool(kwargs.get("include_unverified", False)),
            knowledge_dir=self.project,
            shared_dir=self.cache,
            candidate_dir=self.candidates,
        )

    def test_valid_verified_zone_import_and_query(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        payload = self.import_from()
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["source_repo"], SOURCE_REPO)
        self.assertEqual(payload["source_ref"], SOURCE_REF)
        self.assertEqual(payload["documents"], ["known-failure-signatures.yaml"])
        result = self.query()
        self.assertEqual(result["coverage"]["layers_answered"], ["shared"])
        self.assertEqual({match["layer"] for match in result["matches"]}, {"shared"})
        self.assertEqual(result["matches"][0]["status"], "verified")
        self.assertIsNone(result["matches"][0]["warning"])
        self.assertEqual(result["matches"][0]["source_repo"], SOURCE_REPO)
        self.assertEqual(result["matches"][0]["source_ref"], SOURCE_REF)
        fetched = client.get_entry(
            repo_root=self.root,
            entry_id=result["matches"][0]["id"],
            layers=("shared",),
            knowledge_dir=self.project,
            shared_dir=self.cache,
            candidate_dir=self.candidates,
        )
        self.assertEqual(fetched["layer"], "shared")
        self.assertEqual(fetched["source_repo"], SOURCE_REPO)

    def test_upstream_filename_without_v2_suffix_imports(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        payload = self.import_from()
        self.assertEqual(payload["status"], "passed")
        self.assertIn("known-failure-signatures.yaml", payload["documents"])

    def test_split_files_of_one_kind_are_kept(self) -> None:
        first = verified_entry()
        second = verified_entry()
        second["slug"] = "sample-two"
        second["uuid"] = v2.derived_uuid("owner/fork", "known-failure-signatures", "sample-two")
        second = v2.with_content_hash(second)
        self.write_source("known-failure-signatures.yaml", verified_document(first))
        self.write_source(
            "known-failure-signatures-extra.yaml", verified_document(second)
        )
        payload = self.import_from()
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["entry_count"], 2)
        result = self.query()
        slugs = {match["id"] for match in result["matches"]}
        self.assertEqual(slugs, {"sample", "sample-two"})

    def test_unverified_zone_is_rejected(self) -> None:
        document = verified_document(sample_entry(), layer="unverified")
        self.write_source("known-failure-signatures.yaml", document)
        payload = self.import_from(expect=1)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(payload.get("cache_preserved"))
        result = self.query()
        self.assertEqual(result["matches"], [])
        self.assertNotIn("shared", result["coverage"]["layers_answered"])

    def test_project_zone_verified_claim_is_rejected(self) -> None:
        document = verified_document(verified_entry(), layer="project")
        self.write_source("known-failure-signatures.v2.yaml", document)
        payload = self.import_from(expect=1)
        self.assertEqual(payload["status"], "failed")
        result = self.query()
        self.assertEqual(result["matches"], [])
        self.assertNotIn("shared", result["coverage"]["layers_answered"])

    def test_unverified_status_in_verified_zone_is_rejected(self) -> None:
        document = verified_document(sample_entry(), layer="verified")
        self.write_source("known-failure-signatures.yaml", document)
        payload = self.import_from(expect=1)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(any("shared verified zone" in item for item in payload["problems"]))

    def test_unresolved_scope_is_rejected(self) -> None:
        entry = verified_entry()
        entry["scope"]["cann"] = v2.unresolved_constraint(
            "the CANN version of the verification container"
        )
        entry = v2.with_content_hash(entry)
        self.write_source("known-failure-signatures.yaml", verified_document(entry))
        payload = self.import_from(expect=1)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(any("unresolved" in item for item in payload["problems"]))

    def test_malformed_source_ref_is_rejected(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        payload = self.import_from(source_ref="HEAD", expect=1)
        self.assertEqual(payload["status"], "failed")
        self.assertIn("source_ref", payload["error"])

    def test_wrong_source_identity_is_rejected(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        payload = self.import_from(
            "--expect-source-repo",
            SOURCE_REPO,
            source_repo="other/repo",
            expect=1,
        )
        self.assertEqual(payload["status"], "failed")
        self.assertIn("does not match configured", payload["error"])

    def test_failed_refresh_preserves_valid_cache(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        first = self.import_from()
        self.assertEqual(first["status"], "passed")
        bad = verified_document(sample_entry(), layer="unverified")
        self.write_source("known-failure-signatures.yaml", bad)
        second = self.import_from(expect=1)
        self.assertEqual(second["status"], "failed")
        self.assertTrue(second.get("cache_preserved"))
        result = self.query()
        self.assertEqual({match["layer"] for match in result["matches"]}, {"shared"})
        self.assertEqual(result["matches"][0]["status"], "verified")

    def test_invalid_cached_document_is_excluded_from_shared_results(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        self.import_from()
        cached = self.cache / "known-failure-signatures.yaml"
        cached.chmod(0o644)
        poisoned = json.loads(cached.read_text(encoding="utf-8"))
        poisoned["layer"] = "project"
        cached.write_text(json.dumps(poisoned) + "\n", encoding="utf-8")
        result = self.query(layers=("shared", "project"))
        self.assertEqual(result["matches"], [])
        self.assertNotIn("shared", result["coverage"]["layers_answered"])
        self.assertIn("shared", [item["layer"] for item in result["degradation"]])
        self.assertEqual(
            result["capabilities"]["project"]["status"],
            "available",
        )

    def test_stale_reviewed_entry_may_enter_shared(self) -> None:
        entry = verified_entry(status="stale", confidence="medium")
        self.write_source("known-failure-signatures.yaml", verified_document(entry))
        payload = self.import_from()
        self.assertEqual(payload["status"], "passed")
        result = self.query()
        self.assertEqual(result["matches"][0]["status"], "stale")

    def test_configured_import_query_get_honor_persisted_policy(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        payload = self.import_from(
            "--expect-source-repo",
            SOURCE_REPO,
            "--expect-source-ref",
            SOURCE_REF,
        )
        self.assertEqual(payload["status"], "passed")
        self.assertEqual(payload["expected_source_repo"], SOURCE_REPO)
        self.assertEqual(payload["expected_source_ref"], SOURCE_REF)
        result = self.query()
        self.assertEqual(result["matches"][0]["source_repo"], SOURCE_REPO)
        self.assertEqual(result["matches"][0]["expected_source_repo"], SOURCE_REPO)
        fetched = client.get_entry(
            repo_root=self.root,
            entry_id="sample",
            layers=("shared",),
            knowledge_dir=self.project,
            shared_dir=self.cache,
            candidate_dir=self.candidates,
        )
        self.assertEqual(fetched["expected_source_ref"], SOURCE_REF)

    def test_metadata_repo_tamper_is_not_shared(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        self.import_from("--expect-source-repo", SOURCE_REPO, "--expect-source-ref", SOURCE_REF)
        metadata_path = self.cache / "cache-metadata.json"
        metadata_path.chmod(0o644)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["source_repo"] = "example-org/untrusted-knowledge"
        metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        result = self.query()
        self.assertEqual(result["matches"], [])
        self.assertNotIn("shared", result["coverage"]["layers_answered"])
        shared_cap = result["capabilities"]["shared"]
        self.assertEqual(shared_cap["status"], "unavailable")
        self.assertEqual(shared_cap["source_repo"], "example-org/untrusted-knowledge")
        self.assertEqual(shared_cap["expected_source_repo"], SOURCE_REPO)
        fetched = client.get_entry(
            repo_root=self.root,
            entry_id="sample",
            layers=("shared",),
            knowledge_dir=self.project,
            shared_dir=self.cache,
            candidate_dir=self.candidates,
        )
        self.assertIsNone(fetched)

    def test_metadata_ref_tamper_is_not_shared(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        self.import_from("--expect-source-repo", SOURCE_REPO, "--expect-source-ref", SOURCE_REF)
        metadata_path = self.cache / "cache-metadata.json"
        metadata_path.chmod(0o644)
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        metadata["source_ref"] = "f" * 40
        metadata_path.write_text(json.dumps(metadata) + "\n", encoding="utf-8")
        result = self.query()
        self.assertEqual(result["matches"], [])
        self.assertEqual(result["capabilities"]["shared"]["source_ref"], "f" * 40)
        self.assertEqual(result["capabilities"]["shared"]["expected_source_ref"], SOURCE_REF)

    def test_custom_source_without_unrelated_expectation(self) -> None:
        custom_repo = "example-org/custom-knowledge"
        custom_ref = "abcdef0123456789abcdef0123456789abcdef01"
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        payload = self.import_from(source_repo=custom_repo, source_ref=custom_ref)
        self.assertEqual(payload["status"], "passed")
        result = self.query()
        self.assertEqual(result["matches"][0]["source_repo"], custom_repo)
        self.assertEqual(result["matches"][0]["expected_source_repo"], custom_repo)

    def test_missing_or_invalid_policy_fails_closed(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        self.import_from()
        policy = self.cache / "source-policy.json"
        policy.chmod(0o644)
        policy.unlink()
        missing = self.query()
        self.assertEqual(missing["matches"], [])
        self.assertEqual(missing["capabilities"]["shared"]["status"], "unavailable")
        policy.write_text("{not-json", encoding="utf-8")
        invalid = self.query()
        self.assertEqual(invalid["matches"], [])
        self.assertEqual(invalid["capabilities"]["shared"]["status"], "unavailable")

    def test_failed_refresh_preserves_cache_and_policy(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        first = self.import_from(
            "--expect-source-repo",
            SOURCE_REPO,
            "--expect-source-ref",
            SOURCE_REF,
        )
        self.assertEqual(first["status"], "passed")
        self.write_source(
            "known-failure-signatures.yaml",
            verified_document(sample_entry(), layer="unverified"),
        )
        second = self.import_from(
            "--expect-source-repo",
            SOURCE_REPO,
            "--expect-source-ref",
            SOURCE_REF,
            expect=1,
        )
        self.assertTrue(second.get("cache_preserved"))
        policy = json.loads((self.cache / "source-policy.json").read_text(encoding="utf-8"))
        self.assertEqual(policy["expect_source_ref"], SOURCE_REF)
        result = self.query()
        self.assertEqual(result["matches"][0]["status"], "verified")
        self.assertEqual(result["matches"][0]["expected_source_ref"], SOURCE_REF)

    def test_changed_ref_refresh_replaces_policy(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        self.import_from(source_ref=SOURCE_REF)
        new_ref = "1234567890abcdef1234567890abcdef12345678"
        refreshed = self.import_from(source_ref=new_ref)
        self.assertEqual(refreshed["status"], "passed")
        self.assertEqual(refreshed["expected_source_ref"], new_ref)
        result = self.query()
        self.assertEqual(result["matches"][0]["source_ref"], new_ref)
        self.assertEqual(result["matches"][0]["expected_source_ref"], new_ref)

    def test_clear_removes_source_policy(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        self.import_from()
        cleared = self.run_cache("clear")
        self.assertTrue(cleared["policy_removed"])
        self.assertFalse((self.cache / "source-policy.json").exists())

    def test_different_reviewer_is_eligible(self) -> None:
        path = self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        payload = self.import_from()
        self.assertEqual(payload["status"], "passed")
        upstream = self.upstream_validate_exit(path)
        if upstream is not None:
            self.assertEqual(upstream, 0)

    def test_submitter_only_verified_by_is_rejected(self) -> None:
        entry = verified_entry()
        entry["provenance"]["contributor"] = "anonymous"
        entry["verification"]["verified_by"] = ["anonymous"]
        entry = v2.with_content_hash(entry)
        path = self.write_source("known-failure-signatures.yaml", verified_document(entry))
        payload = self.import_from(expect=1)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(any("submitter" in item for item in payload["problems"]))
        upstream = self.upstream_validate_exit(path)
        if upstream is not None:
            self.assertEqual(upstream, 1)

    def test_bot_only_verified_by_is_rejected(self) -> None:
        entry = verified_entry()
        entry["verification"]["verified_by"] = ["github-actions[bot]"]
        entry = v2.with_content_hash(entry)
        path = self.write_source("known-failure-signatures.yaml", verified_document(entry))
        payload = self.import_from(expect=1)
        self.assertEqual(payload["status"], "failed")
        self.assertTrue(any("bot identity" in item for item in payload["problems"]))
        upstream = self.upstream_validate_exit(path)
        if upstream is not None:
            self.assertEqual(upstream, 1)

    def test_interrupted_then_failed_retry_preserves_previous(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        importer = load_importer()
        first = importer.do_import(
            self.cache,
            self.source,
            source_repo=SOURCE_REPO,
            source_ref=SOURCE_REF,
            expect_repo=SOURCE_REPO,
            expect_ref=SOURCE_REF,
        )
        self.assertEqual(first["status"], "passed")
        before = digest_tree(self.cache)
        real_rename = Path.rename

        def interrupt_stage(path: Path, target: Path) -> Path:
            if path.name == f".{self.cache.name}.staging":
                raise KeyboardInterrupt(
                    "synthetic interruption after live renamed to backup"
                )
            return real_rename(path, target)

        interrupted = False
        try:
            with mock.patch.object(Path, "rename", new=interrupt_stage):
                importer.do_import(
                    self.cache,
                    self.source,
                    source_repo=SOURCE_REPO,
                    source_ref="d" * 40,
                    expect_repo=SOURCE_REPO,
                    expect_ref="d" * 40,
                )
        except KeyboardInterrupt:
            interrupted = True
        self.assertTrue(interrupted)
        backup = shared.backup_path(self.cache)
        self.assertFalse(self.cache.exists())
        self.assertEqual(digest_tree(backup), before)

        real_write = Path.write_text

        def fail_policy(path: Path, *args: object, **kwargs: object) -> int:
            if path.name == shared.SHARED_SOURCE_POLICY and ".staging" in str(path.parent):
                raise OSError("synthetic policy staging write failure")
            return real_write(path, *args, **kwargs)

        with mock.patch.object(Path, "write_text", new=fail_policy):
            retry = importer.do_import(
                self.cache,
                self.source,
                source_repo=SOURCE_REPO,
                source_ref="d" * 40,
                expect_repo=SOURCE_REPO,
                expect_ref="d" * 40,
            )
        self.assertEqual(retry["status"], "failed")
        self.assertTrue(retry.get("cache_preserved"))
        preserved = (
            digest_tree(self.cache)
            if shared.is_coherent_cache(self.cache)
            else digest_tree(backup)
        )
        self.assertEqual(preserved, before)
        result = self.query()
        self.assertEqual({match["layer"] for match in result["matches"]}, {"shared"})
        self.assertEqual(result["matches"][0]["source_ref"], SOURCE_REF)

    def test_cached_verified_by_mutation_is_excluded(self) -> None:
        self.write_source(
            "known-failure-signatures.yaml", verified_document(verified_entry())
        )
        self.import_from()
        cached = self.cache / "known-failure-signatures.yaml"
        cached.chmod(0o644)
        document = json.loads(cached.read_text(encoding="utf-8"))
        document["entries"][0]["verification"]["verified_by"] = ["submitter"]
        cached.write_text(json.dumps(document) + "\n", encoding="utf-8")
        result = self.query()
        self.assertEqual(result["matches"], [])
        self.assertNotIn("shared", result["coverage"]["layers_answered"])


if __name__ == "__main__":
    unittest.main()
