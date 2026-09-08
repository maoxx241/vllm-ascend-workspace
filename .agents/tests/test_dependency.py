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
SCRIPTS = ROOT / ".agents" / "scripts"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_coordinator as coordinator  # noqa: E402
import vaws_dependency as deps  # noqa: E402
import vaws_remote_dev as remote_dev  # noqa: E402


def _write_required(root: Path, pin: dict) -> None:
    for relative in pin["identity"]["required_files"]:
        dest = root / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("ok\n", encoding="utf-8")


def _git(root: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, capture_output=True)


def _git_init(root: Path, origin: str | None, message: str) -> None:
    _git(root, "init")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "dev")
    if origin:
        _git(root, "remote", "add", "origin", origin)
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)


def _synthetic_off_pin(root: Path, pin: dict) -> None:
    """Pinned-tree files plus one local commit so HEAD != pin while files stay."""
    _write_required(root, pin)
    _git_init(root, pin["url"], "pin-tree")
    (root / "scratch-off-pin.txt").write_text("local\n", encoding="utf-8")
    _git(root, "add", "scratch-off-pin.txt")
    _git(root, "commit", "-m", "synthetic off-pin")


def _minimal_pin(**overrides: object) -> dict:
    pin = {
        "schema_version": 1,
        "name": "example-dep",
        "repository": "vllm-ascend-workspace/example-dep",
        "url": "https://github.com/vllm-ascend-workspace/example-dep.git",
        "visibility": "public",
        "ref": "main",
        "commit": "a" * 40,
        "root_env": "VAWS_EXAMPLE_DEP_ROOT",
        "default_checkout": "{shared_workspace_root}/.vaws-local/example-dep",
        "bootstrap": "echo bootstrap",
        "consumed_surface": ["cli"],
        "identity": {"required_files": ["README.md"], "canonical_origin": True},
    }
    pin.update(overrides)
    return pin


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
            self.assertTrue(
                isinstance(pin["consumed_surface"], list)
                or isinstance(pin["consumed_surface"], dict),
                name,
            )
            self.assertIn("required_files", pin["identity"])
            self.assertTrue(pin["identity"]["canonical_origin"], name)

    def test_pin_required_files_match_wrapper_lists(self) -> None:
        self.assertEqual(
            tuple(deps.load_pin("vaws-coordinator")["identity"]["required_files"]),
            coordinator.REQUIRED_FILES,
        )
        self.assertEqual(
            tuple(deps.load_pin("remote-dev")["identity"]["required_files"]),
            remote_dev.REQUIRED_FILES,
        )
        scripts = ROOT / ".agents" / "skills" / "npu-fleet-monitor" / "scripts"
        if str(scripts) not in sys.path:
            sys.path.insert(0, str(scripts))
        import manage_monitor  # noqa: E402

        self.assertEqual(
            tuple(deps.load_pin(deps.VAWS_TOP_NAME)["identity"]["required_files"]),
            manage_monitor.REQUIRED_FILES,
        )
        self.assertIn(
            "task_server.py",
            deps.load_pin("vaws-coordinator")["identity"]["required_files"],
        )

    def test_coordinator_surfaces_match_origin_main_values(self) -> None:
        raw = json.loads((ROOT / ".agents/deps/coordinator.json").read_text(encoding="utf-8"))
        pin = deps.load_pin("vaws-coordinator")
        main = json.loads(
            subprocess.check_output(
                ["git", "show", "origin/main:.agents/deps/coordinator.json"],
                cwd=ROOT,
                text=True,
            )
        )
        self.assertEqual(
            json.dumps(main["consumed_surface"], sort_keys=True, separators=(",", ":")),
            json.dumps(pin["consumed_surface"], sort_keys=True, separators=(",", ":")),
        )
        for key in ("tree", "pinned_mirrors", "arrival_blobs"):
            self.assertEqual(
                json.dumps(main[key], sort_keys=True, separators=(",", ":")),
                json.dumps(pin["extensions"][key], sort_keys=True, separators=(",", ":")),
                key,
            )
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
        self.assertEqual(
            pin["consumed_surface"],
            {"conformance_kit": "conformance/runner.py", "shared_corpus": "corpus/verified"},
        )

    def test_consumed_surface_accepts_array_or_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            array_path = Path(tmp) / "array.json"
            array_path.write_text(json.dumps(_minimal_pin(consumed_surface=["cli"])), encoding="utf-8")
            loaded = deps.load_pin_file(array_path)
            self.assertEqual(loaded["consumed_surface"], ["cli"])
            object_path = Path(tmp) / "object.json"
            object_path.write_text(
                json.dumps(_minimal_pin(consumed_surface={"cli": "scripts/vaws.py"})),
                encoding="utf-8",
            )
            loaded = deps.load_pin_file(object_path)
            self.assertEqual(loaded["consumed_surface"], {"cli": "scripts/vaws.py"})
            hooks_path = Path(tmp) / "hooks.json"
            hooks_path.write_text(
                json.dumps(
                    _minimal_pin(consumed_surface={"hooks": ["hooks/a.py", "hooks/b.py"]})
                ),
                encoding="utf-8",
            )
            loaded = deps.load_pin_file(hooks_path)
            self.assertEqual(loaded["consumed_surface"]["hooks"], ["hooks/a.py", "hooks/b.py"])

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
            _write_required(root, pin)
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

    def test_incomplete_git_checkout_names_the_missing_file(self) -> None:
        pin = deps.load_pin("vaws-coordinator")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coord"
            root.mkdir()
            _write_required(root, pin)
            _git_init(root, pin["url"], "complete")
            deleted = "scripts/vaws.py"
            (root / deleted).unlink()
            env = {"VAWS_COORDINATOR_ROOT": str(root)}
            info = deps.inspect("vaws-coordinator", env)
            self.assertEqual(info["state"], "incomplete")
            self.assertTrue(any(deleted in item for item in info["problems"]))
            with self.assertRaises(deps.DependencyUnavailable) as ctx:
                deps.resolve("vaws-coordinator", required=True, env=env)
            self.assertIn(deleted, str(ctx.exception))
            self.assertIn(pin["bootstrap"], str(ctx.exception))
            self.assertIn("incomplete", str(ctx.exception))

    def test_synthetic_off_pin_keeps_required_files(self) -> None:
        pin = deps.load_pin("vaws-coordinator")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coord"
            root.mkdir()
            _synthetic_off_pin(root, pin)
            env = {"VAWS_COORDINATOR_ROOT": str(root)}
            info = deps.inspect("vaws-coordinator", env)
            self.assertEqual(info["state"], "off_pin")
            self.assertFalse(info["pin_matches"])
            for relative in pin["identity"]["required_files"]:
                self.assertTrue((root / relative).is_file(), relative)
            resolved = deps.resolve("vaws-coordinator", required=True, env=env)
            self.assertEqual(Path(resolved).resolve(), root.resolve())
            self.assertEqual(deps.status_exit_code({"vaws-coordinator": "off_pin"}, env), 1)
            allowed = {**env, "VAWS_DEPS_ALLOW_OFF_PIN": "vaws-coordinator"}
            self.assertEqual(deps.status_exit_code({"vaws-coordinator": "off_pin"}, allowed), 0)
            self.assertEqual(deps.acknowledged_drift(allowed), ["vaws-coordinator"])

    def test_wrong_origin_returns_the_path_and_status_exits_one(self) -> None:
        pin = deps.load_pin("vaws-coordinator")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "coord"
            root.mkdir()
            _write_required(root, pin)
            _git_init(root, "https://github.com/example/fork.git", "fork")
            env = {"VAWS_COORDINATOR_ROOT": str(root)}
            info = deps.inspect("vaws-coordinator", env)
            self.assertEqual(info["state"], "wrong_origin")
            resolved = deps.resolve("vaws-coordinator", required=True, env=env)
            self.assertEqual(Path(resolved).resolve(), root.resolve())
            self.assertEqual(deps.status_exit_code({"vaws-coordinator": "wrong_origin"}, env), 1)
            allowed = {**env, "VAWS_DEPS_ALLOW_OFF_PIN": "vaws-coordinator"}
            self.assertEqual(deps.status_exit_code({"vaws-coordinator": "wrong_origin"}, allowed), 0)
            self.assertEqual(deps.acknowledged_drift(allowed), ["vaws-coordinator"])

            from vaws_capability import build_doctor_envelope

            envelope = build_doctor_envelope(
                argv=["python3", ".agents/scripts/vaws_deps.py", "doctor"],
                env={
                    **env,
                    "VAWS_REMOTE_DEV_ROOT": "/nonexistent-remote-dev",
                    "VAWS_TOP_ROOT": "/nonexistent-fleet-dashboard",
                    "VAWS_KNOWLEDGE_KIT_ROOT": "/nonexistent-vaws-knowledge",
                },
            )
            task = envelope["extensions"]["capability_report"]["capabilities"]["task_pool"]
            self.assertTrue(task["available"])
            self.assertTrue(task["degraded"])
            self.assertTrue(any("wrong_origin" in item.get("detail", "") for item in task["degradation"]))

            allow_env = {key: value for key, value in os.environ.items() if not key.startswith("VAWS_")}
            allow_env.update(allowed)
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "vaws_deps.py"), "status", "vaws-coordinator"],
                capture_output=True,
                text=True,
                env=allow_env,
                check=False,
                cwd=str(ROOT),
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["state"], "wrong_origin")


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
