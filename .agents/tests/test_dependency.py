#!/usr/bin/env python3
"""Uniform dependency pin loader, identity, drift, and bootstrap contract."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_dependency as deps  # noqa: E402


class PinSchemaTests(unittest.TestCase):
    def test_all_four_pins_validate(self) -> None:
        pins = deps.all_pins()
        self.assertEqual(
            set(pins),
            {"remote-dev", "vaws-coordinator", deps.VAWS_TOP_NAME, "vaws-knowledge"},
        )
        for name, pin in pins.items():
            self.assertEqual(pin["schema_version"], 1, name)
            self.assertEqual(pin["name"], name)
            self.assertRegex(pin["commit"], r"^[0-9a-f]{40}$")
            self.assertIn(pin["visibility"], {"public", "private"})
            self.assertIsInstance(pin["consumed_surface"], list)
            self.assertIn("required_files", pin["identity"])
            self.assertIsInstance(pin["identity"]["canonical_origin"], bool)

    def test_coordinator_extensions_are_byte_identical(self) -> None:
        raw = json.loads((ROOT / ".agents/deps/coordinator.json").read_text(encoding="utf-8"))
        pin = deps.load_pin("vaws-coordinator")
        for key in ("tree", "pinned_mirrors", "arrival_blobs"):
            self.assertEqual(
                json.dumps(raw["extensions"][key], sort_keys=True, separators=(",", ":")),
                json.dumps(pin["extensions"][key], sort_keys=True, separators=(",", ":")),
                key,
            )

    def test_knowledge_pin_carries_the_agreed_defaults(self) -> None:
        pin = deps.load_pin("vaws-knowledge")
        self.assertEqual(pin["visibility"], "public")
        self.assertEqual(pin["ref"], "main")
        self.assertEqual(
            pin["default_checkout"],
            "{shared_workspace_root}/.vaws-local/vaws-knowledge",
        )
        self.assertEqual(
            pin["bootstrap"],
            "python3 .agents/scripts/vaws_deps.py bootstrap vaws-knowledge",
        )
        self.assertEqual(pin["consumed_surface"], ["conformance_kit", "shared_corpus"])

    def test_load_pin_names_the_field_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            path.write_text(json.dumps({"schema_version": 1, "name": "x"}), encoding="utf-8")
            with self.assertRaises(deps.DependencyPinError) as ctx:
                deps.load_pin_file(path)
            self.assertTrue(str(ctx.exception.field or "").startswith("$"))


class OriginNormalizationTests(unittest.TestCase):
    def test_https_and_ssh_origins_match(self) -> None:
        https = "https://github.com/vllm-ascend-workspace/vaws-coordinator.git"
        ssh = "git@" + "github.com:vllm-ascend-workspace/vaws-coordinator.git"
        self.assertTrue(deps.origins_match(https, ssh))
        self.assertTrue(deps.origins_match("ssh://git@" + "github.com/vllm-ascend-workspace/vaws-coordinator", https))


class InspectResolveTests(unittest.TestCase):
    def test_fake_checkout_is_not_git(self) -> None:
        pin = deps.load_pin("vaws-coordinator")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for relative in pin["identity"]["required_files"]:
                dest = root / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("", encoding="utf-8")
            env = {"VAWS_COORDINATOR_ROOT": str(root)}
            info = deps.inspect("vaws-coordinator", env)
            self.assertEqual(info["state"], "not_git")
            self.assertIsNone(deps.resolve("vaws-coordinator", required=False, env=env))
            with self.assertRaises(deps.DependencyUnavailable) as ctx:
                deps.resolve("vaws-coordinator", required=True, env=env)
            self.assertIn(pin["bootstrap"], str(ctx.exception))
            self.assertIn(pin["root_env"], str(ctx.exception))

    def test_missing_is_not_an_execution_path(self) -> None:
        env = {"VAWS_COORDINATOR_ROOT": "/nonexistent-vaws-coordinator"}
        info = deps.inspect("vaws-coordinator", env)
        self.assertEqual(info["state"], "missing")
        self.assertIsNone(deps.resolve("vaws-coordinator", required=False, env=env))

    def test_off_pin_returns_the_path_and_status_exits_one(self) -> None:
        pin = deps.load_pin("vaws-coordinator")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coord"
            root.mkdir()
            for relative in pin["identity"]["required_files"]:
                dest = root / relative
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text("ok\n", encoding="utf-8")
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "dev"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "off-pin"], cwd=root, check=True, capture_output=True)
            env = {"VAWS_COORDINATOR_ROOT": str(root)}
            info = deps.inspect("vaws-coordinator", env)
            self.assertEqual(info["state"], "off_pin")
            self.assertFalse(info["pin_matches"])
            resolved = deps.resolve("vaws-coordinator", required=True, env=env)
            self.assertEqual(Path(resolved).resolve(), root.resolve())
            self.assertEqual(deps.status_exit_code({"vaws-coordinator": "off_pin"}, env), 1)
            allowed = {**env, "VAWS_DEPS_ALLOW_OFF_PIN": "vaws-coordinator"}
            self.assertEqual(deps.status_exit_code({"vaws-coordinator": "off_pin"}, allowed), 0)
            self.assertEqual(deps.acknowledged_drift(allowed), ["vaws-coordinator"])


class HookLedgerTests(unittest.TestCase):
    def test_ledger_is_bounded_and_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
            path = deps.record_hook_degradation(
                hook="vaws",
                dep="vaws-coordinator",
                state="missing",
                repo_root=repo,
            )
            self.assertTrue(path.is_file())
            line = path.read_text(encoding="utf-8").strip()
            self.assertIn("hook=vaws", line)
            self.assertIn("dep=vaws-coordinator", line)
            self.assertIn("state=missing", line)
            recent = deps.read_hook_degradations(repo_root=repo)
            self.assertEqual(len(recent), 1)


if __name__ == "__main__":
    unittest.main()
