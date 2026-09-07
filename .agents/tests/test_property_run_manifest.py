#!/usr/bin/env python3
"""Property tests for Run Manifest v1 (``.agents/lib/vaws_run_manifest.py``).

Properties:

* every generated valid manifest validates, survives ``write_manifest`` /
  ``load_manifest`` unchanged, and is never silently normalized;
* corrupting any single top-level field (or any artifact field) is rejected
  with a message that names the field;
* the status machine matches its documented transition table exactly;
* generated run ids are always safe ids;
* the Python validator agrees with ``run-manifest-v1.schema.json`` on the
  rules the schema states (known divergences are recorded as defects).
"""

from __future__ import annotations

import itertools
import json
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_run_manifest as rm  # noqa: E402
from vaws_run_manifest import RunManifestError, add_artifact, generate_run_id, load_manifest, new_manifest, transition_status, validate_manifest, write_manifest  # noqa: E402
from test_property_support import Gen, run_cases  # noqa: E402

SCHEMA = json.loads((ROOT / ".agents" / "schemas" / "run-manifest-v1.schema.json").read_text(encoding="utf-8"))
SAFE_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789._-"
SAFE_ENV_WORDS = ("VLLM", "HCCL", "ASCEND", "PATH", "HOME", "RANK", "WORLD", "SIZE", "DEBUG", "LEVEL", "TOKENIZER", "AUTHORITY", "PASSAGE", "KEYRING", "SECRETARY")
# Words the documented filter must reject when they appear as a whole
# underscore-delimited component (case-insensitive).
FORBIDDEN_ENV_WORDS = ("API_KEY", "APIKEY", "ACCESS_KEY", "ACCESSKEY", "AUTH", "CREDENTIAL", "PASS", "PASSWORD", "SECRET", "TOKEN")


def safe_id(gen: Gen) -> str:
    return gen.text("abcdefghijklmnopqrstuvwxyz0123456789", 1, 1) + gen.text(SAFE_ID_ALPHABET, 0, gen.choice((5, 40, 127)))


def timestamp(gen: Gen) -> str:
    base = f"{gen.integer(1970, 2999):04d}-{gen.integer(1, 12):02d}-{gen.integer(1, 28):02d}T{gen.integer(0, 23):02d}:{gen.integer(0, 59):02d}:{gen.integer(0, 59):02d}"
    if gen.boolean(0.3):
        base += "." + gen.text("0123456789", 1, 6)
    return base + "Z"


def env_name(gen: Gen) -> str:
    return "_".join(gen.choice(SAFE_ENV_WORDS) for _ in range(gen.integer(1, 3)))


def artifact(gen: Gen, name: str) -> dict[str, Any]:
    item = {"name": name, "kind": gen.choice(("report", "raw", "log", gen.word(1, 8))), "uri": gen.choice(("report.md", "file:///tmp/x", gen.word(1, 12)))}
    if gen.boolean(0.5):
        item["sha256"] = gen.text("0123456789abcdef", 64, 64)
    return item


def valid_manifest(gen: Gen) -> dict[str, Any]:
    names: list[str] = []
    while len(names) < gen.integer(0, 4):
        candidate = gen.word(1, 10)
        if candidate not in names:
            names.append(candidate)
    created = timestamp(gen)
    return {
        "schema_version": 1,
        "run_id": safe_id(gen),
        "parent_run_id": None if gen.boolean() else safe_id(gen),
        "run_type": gen.choice(sorted(rm.RUN_TYPES)),
        "workspace_snapshot": gen.json_object(),
        "environment": gen.json_object(),
        "model": gen.json_object(),
        "topology": gen.json_object(),
        "command": [gen.word(0, 10) for _ in range(gen.integer(0, 5))],
        "environment_variables": {env_name(gen): gen.word(0, 10) for _ in range(gen.integer(0, 4))},
        "artifacts": [artifact(gen, name) for name in names],
        "status": gen.choice(sorted(rm.RUN_STATUSES)),
        "created_at": created,
        "updated_at": created if gen.boolean() else timestamp(gen),
    }


class ValidManifestProperties(unittest.TestCase):
    def test_generated_manifests_validate_and_round_trip_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def body(gen: Gen, index: int) -> None:
                manifest = valid_manifest(gen)
                frozen = json.dumps(manifest, sort_keys=True)
                validate_manifest(manifest)
                self.assertEqual(json.dumps(manifest, sort_keys=True), frozen, "validate_manifest must not mutate")
                path = Path(tmp) / f"m{index}.json"
                write_manifest(path, manifest)
                loaded = load_manifest(path)
                self.assertEqual(loaded, manifest)
                self.assertEqual(json.dumps(manifest, sort_keys=True), frozen, "write_manifest must not mutate")
                self.assertEqual(set(Path(tmp).glob(".m*.tmp")), set(), "no temp files left behind")
                # The on-disk form is canonical JSON and re-validates as-is.
                on_disk = json.loads(path.read_text(encoding="utf-8"))
                validate_manifest(on_disk)
                self.assertEqual(on_disk, manifest)

            run_cases(200, body, label="manifest round trip")

    def test_new_manifest_matches_its_arguments_and_starts_planned(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            run_type = gen.choice(sorted(rm.RUN_TYPES))
            created = timestamp(gen)
            env = {env_name(gen): gen.word(0, 6) for _ in range(gen.integer(0, 3))}
            manifest = new_manifest(run_type=run_type, environment_variables=env, command=["a", "b"], created_at=created)
            self.assertEqual(manifest["status"], "planned")
            self.assertEqual(manifest["run_type"], run_type)
            self.assertEqual(manifest["created_at"], created)
            self.assertEqual(manifest["updated_at"], created)
            self.assertEqual(manifest["environment_variables"], env)
            self.assertIsNone(manifest["parent_run_id"])
            self.assertTrue(manifest["run_id"].startswith(run_type + "-"))
            self.assertRegex(manifest["run_id"], rm.SAFE_ID_RE.pattern)

        run_cases(100, body, label="new_manifest")

    def test_generate_run_id_is_always_a_safe_id(self) -> None:
        seen: set[str] = set()

        def body(gen: Gen, _index: int) -> None:
            run_type = gen.choice(sorted(rm.RUN_TYPES))
            run_id = generate_run_id(run_type, now=timestamp(gen))
            self.assertRegex(run_id, rm.SAFE_ID_RE.pattern)
            self.assertRegex(run_id, SCHEMA["properties"]["run_id"]["pattern"])
            self.assertNotIn(run_id, seen, "run ids must not repeat")
            seen.add(run_id)
            with self.assertRaises(RunManifestError):
                generate_run_id(run_type + "x", now=timestamp(gen))

        run_cases(200, body, label="generate_run_id")


class StatusMachineProperties(unittest.TestCase):
    def test_transition_table_is_exhaustive_and_terminal_states_are_absorbing(self) -> None:
        for current, target in itertools.product(sorted(rm.RUN_STATUSES), repeat=2):
            with self.subTest(current=current, target=target):
                manifest = new_manifest(run_type="debug", run_id="debug-case", created_at="2026-07-25T12:00:00Z")
                manifest["status"] = current
                allowed = target in rm.STATUS_TRANSITIONS[current]
                if allowed:
                    updated = transition_status(manifest, target, updated_at="2026-07-25T12:00:01Z")
                    self.assertEqual(updated["status"], target)
                    self.assertEqual(updated["updated_at"], "2026-07-25T12:00:01Z")
                    self.assertEqual(manifest["status"], current, "transition must not mutate its input")
                    self.assertNotEqual(current, target)
                else:
                    with self.assertRaisesRegex(RunManifestError, "invalid status transition"):
                        transition_status(manifest, target, updated_at="2026-07-25T12:00:01Z")
                if current in rm.TERMINAL_STATUSES:
                    self.assertFalse(rm.STATUS_TRANSITIONS[current], "terminal states must have no exits")

    def test_every_non_terminal_state_can_reach_every_terminal_state(self) -> None:
        for terminal in rm.TERMINAL_STATUSES:
            with self.subTest(terminal=terminal):
                reachable = {"planned"}
                frontier = ["planned"]
                while frontier:
                    state = frontier.pop()
                    for nxt in rm.STATUS_TRANSITIONS[state]:
                        if nxt not in reachable:
                            reachable.add(nxt)
                            frontier.append(nxt)
                self.assertIn(terminal, reachable)


CORRUPTIONS: dict[str, list[Any]] = {
    "schema_version": [2, "1", None, 0],
    "run_id": ["", "A", "-x", "x/y", "a" * 129, None, 1, "a b"],
    "parent_run_id": ["", "-x", "a" * 129, 1, "Upper"],
    "run_type": ["unknown", "", None, "Correctness"],
    "workspace_snapshot": ["str", [], None, 1],
    "environment": ["str", [], None],
    "model": [[], None, 1],
    "topology": ["x", None],
    "command": ["str", [1], [None], {"a": 1}, None],
    "environment_variables": [[], "str", {"A": 1}, {1: "x"}, {"OK": None}],
    "artifacts": [{}, "str", None, [1], [{"name": "a"}], [{"name": "", "kind": "k", "uri": "u"}], [{"name": " ", "kind": "k", "uri": "u"}], [{"name": "a", "kind": "k", "uri": "u", "sha256": "abc"}], [{"name": "a", "kind": "k", "uri": "u"}, {"name": "a", "kind": "k", "uri": "u"}]],
    "status": ["done", "", None, "Passed"],
    "created_at": ["2026-07-25 12:00:00", "2026-07-25T12:00:00", "2026-07-25T12:00:00+00:00", "", None, 1],
    "updated_at": ["2026-07-25T12:00", "yesterday", None],
}


class InvalidManifestProperties(unittest.TestCase):
    def test_single_field_corruption_is_rejected_with_a_locatable_message(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            manifest = valid_manifest(gen)
            field = gen.choice(sorted(CORRUPTIONS))
            corrupted = dict(manifest)
            corrupted[field] = gen.choice(CORRUPTIONS[field])
            with self.assertRaises(RunManifestError) as ctx:
                validate_manifest(corrupted)
            message = str(ctx.exception)
            token = "artifact" if field == "artifacts" else field
            self.assertIn(token, message, f"error for {field} must name the field: {message}")
            # A rejected manifest is never written.
            with tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(RunManifestError):
                    write_manifest(Path(tmp) / "m.json", corrupted)
                self.assertEqual(list(Path(tmp).iterdir()), [])

        run_cases(400, body, label="single-field corruption")

    def test_missing_and_unknown_fields_are_named(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            manifest = valid_manifest(gen)
            removed = gen.sample(sorted(manifest), gen.integer(1, 3))
            extra = [gen.word(1, 8) + "_extra" for _ in range(gen.integer(0, 2))]
            corrupted = {k: v for k, v in manifest.items() if k not in removed}
            for key in extra:
                corrupted[key] = 1
            with self.assertRaises(RunManifestError) as ctx:
                validate_manifest(corrupted)
            message = str(ctx.exception)
            for key in removed:
                self.assertIn(key, message)
            for key in extra:
                self.assertIn(key, message)

        run_cases(150, body, label="missing/unknown fields")

    def test_secret_like_environment_keys_are_rejected_in_every_position_and_case(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            word = gen.choice(FORBIDDEN_ENV_WORDS)
            word = "".join(ch.upper() if gen.boolean() else ch.lower() for ch in word)
            parts = [gen.choice(SAFE_ENV_WORDS) for _ in range(gen.integer(0, 2))]
            position = gen.integer(0, len(parts))
            parts.insert(position, word)
            key = "_".join(parts)
            with self.assertRaisesRegex(RunManifestError, "secret-like key"):
                new_manifest(run_type="debug", environment_variables={key: "x"}, created_at="2026-07-25T12:00:00Z")
            # The same word embedded without delimiters is a different identifier and is allowed.
            embedded = "MY" + word.upper().replace("_", "") + "S"
            new_manifest(run_type="debug", environment_variables={embedded: "x"}, created_at="2026-07-25T12:00:00Z")

        run_cases(200, body, label="secret env keys")

    @unittest.expectedFailure
    def test_known_defect_common_secret_spellings_pass_the_filter(self) -> None:
        """KNOWN DEFECT (low): the secret-key filter matches PASS/PASSWORD but
        not ``PASSWD``, and matches API_KEY/ACCESS_KEY but not ``PRIVATE_KEY``
        or ``SSH_KEY``. Manifests are meant to be shareable evidence, so these
        spellings let credentials into a tracked artifact.
        Evidence: ``DB_PASSWD`` and ``SSH_PRIVATE_KEY`` are accepted."""
        for key in ("DB_PASSWD", "SSH_PRIVATE_KEY", "TLS_KEY", "CLIENT_CERT_KEY"):
            with self.subTest(key=key):
                with self.assertRaises(RunManifestError):
                    new_manifest(run_type="debug", environment_variables={key: "x"}, created_at="2026-07-25T12:00:00Z")

    def test_add_artifact_never_produces_duplicates_or_invalid_hashes(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            manifest = new_manifest(run_type="performance", created_at="2026-07-25T12:00:00Z")
            names = []
            for _ in range(gen.integer(1, 4)):
                name = gen.word(1, 8)
                sha = gen.text("0123456789abcdef", 64, 64) if gen.boolean() else None
                if name in names:
                    with self.assertRaisesRegex(RunManifestError, "duplicated"):
                        add_artifact(manifest, name=name, kind="k", uri="u", sha256=sha, updated_at="2026-07-25T12:00:01Z")
                    continue
                manifest = add_artifact(manifest, name=name, kind="k", uri="u", sha256=sha, updated_at="2026-07-25T12:00:01Z")
                names.append(name)
            self.assertEqual([item["name"] for item in manifest["artifacts"]], names)
            for bad in ("ABC", "0" * 63, "g" * 64, ""):
                with self.assertRaises(RunManifestError):
                    add_artifact(manifest, name="fresh-" + bad[:1], kind="k", uri="u", sha256=bad)

        run_cases(100, body, label="add_artifact")


class SchemaAgreementProperties(unittest.TestCase):
    """The Python validator and the JSON schema must draw the same boundary."""

    def test_schema_and_validator_share_patterns_and_enums(self) -> None:
        props = SCHEMA["properties"]
        self.assertEqual(props["run_id"]["pattern"], rm.SAFE_ID_RE.pattern)
        self.assertEqual(props["parent_run_id"]["oneOf"][1]["pattern"], rm.SAFE_ID_RE.pattern)
        self.assertEqual(set(props["run_type"]["enum"]), set(rm.RUN_TYPES))
        self.assertEqual(set(props["status"]["enum"]), set(rm.RUN_STATUSES))
        self.assertEqual(props["created_at"]["pattern"], rm.RFC3339_UTC_RE.pattern)
        self.assertEqual(props["artifacts"]["items"]["properties"]["sha256"]["pattern"], rm.SHA256_RE.pattern)
        self.assertEqual(set(SCHEMA["required"]), {
            "schema_version", "run_id", "parent_run_id", "run_type", "workspace_snapshot", "environment", "model",
            "topology", "command", "environment_variables", "artifacts", "status", "created_at", "updated_at",
        })

    def test_manifests_generated_here_satisfy_the_schema_shape(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            manifest = valid_manifest(gen)
            self.assertEqual(set(manifest), set(SCHEMA["required"]))
            self.assertEqual(manifest["schema_version"], SCHEMA["properties"]["schema_version"]["const"])
            for item in manifest["artifacts"]:
                self.assertTrue(set(item) <= set(SCHEMA["properties"]["artifacts"]["items"]["properties"]))

        run_cases(50, body, label="schema shape")

    @unittest.expectedFailure
    def test_known_defect_validator_accepts_schema_version_true_and_float(self) -> None:
        """KNOWN DEFECT (low): the schema says ``schema_version: {const: 1}``;
        JSON Schema distinguishes ``1`` from ``true`` and (for const) from
        ``1.0``. The Python check ``!= 1`` accepts ``True`` and ``1.0`` and
        preserves them on write, so a manifest the library calls valid fails
        external schema validation. Evidence: ``schema_version=True`` validates."""
        manifest = new_manifest(run_type="debug", created_at="2026-07-25T12:00:00Z")
        self.assertEqual(SCHEMA["properties"]["schema_version"], {"const": 1})
        for value in (True, 1.0):
            with self.subTest(value=value):
                bad = dict(manifest, schema_version=value)
                with self.assertRaises(RunManifestError):
                    validate_manifest(bad)

    @unittest.expectedFailure
    def test_known_defect_validator_accepts_artifact_shapes_the_schema_forbids(self) -> None:
        """KNOWN DEFECT (low-medium): artifacts are ``additionalProperties:
        false`` with ``sha256: {type: string}`` in the schema, but the Python
        validator neither rejects unknown artifact keys nor ``sha256: null``.
        Manifests carrying either are accepted, written and re-loaded here yet
        rejected by any schema-based consumer. Evidence: both validate."""
        manifest = new_manifest(run_type="debug", created_at="2026-07-25T12:00:00Z")
        self.assertFalse(SCHEMA["properties"]["artifacts"]["items"]["additionalProperties"])
        for item in ({"name": "a", "kind": "k", "uri": "u", "extra": 1}, {"name": "a", "kind": "k", "uri": "u", "sha256": None}):
            with self.subTest(item=item):
                with self.assertRaises(RunManifestError):
                    validate_manifest(dict(manifest, artifacts=[item]))

    @unittest.expectedFailure
    def test_known_defect_free_form_objects_may_validate_but_not_round_trip(self) -> None:
        """KNOWN DEFECT (low): ``workspace_snapshot``/``environment``/``model``/
        ``topology`` are only checked to be mappings, so non-JSON-native content
        (integer keys, NaN, tuples) validates; ``write_manifest`` then coerces
        keys to strings and emits non-standard ``NaN``, and ``load_manifest``
        returns something different from what validated.
        Evidence: ``{1: 'x'}`` loads back as ``{'1': 'x'}``."""
        manifest = new_manifest(run_type="debug", environment={1: "x"}, created_at="2026-07-25T12:00:00Z")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.json"
            write_manifest(path, manifest)
            self.assertEqual(load_manifest(path), manifest)


if __name__ == "__main__":
    unittest.main()
