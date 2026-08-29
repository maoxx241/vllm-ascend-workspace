from __future__ import annotations

import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REMOTE_DEV = ROOT / ".remote-dev"
if str(REMOTE_DEV) not in sys.path:
    sys.path.insert(0, str(REMOTE_DEV))

from mcp.schemas import TOOL_SCHEMAS  # noqa: E402


class CliHelpTests(unittest.TestCase):
    def test_cli_wrappers_have_help(self) -> None:
        scripts = sorted((ROOT / ".remote-dev" / "tools").glob("remote_*.py"))
        expected_scripts = {ROOT / ".remote-dev" / "tools" / (name.replace(".", "_") + ".py")
                            for name in TOOL_SCHEMAS if name.startswith("remote.")}
        self.assertEqual(set(scripts), expected_scripts)
        for script_path in scripts:
            script = str(script_path.relative_to(ROOT))
            with self.subTest(script=script):
                proc = subprocess.run([sys.executable, str(script_path), "--help"], capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)

    def test_task_facade_uses_one_cli_without_endpoint_or_network_requirements(self):
        script = ROOT / ".agents/scripts/vaws.py"
        for args in (["--help"], ["attach", "--help"], ["session", "--help"],
                     ["run", "--help"], ["execution", "--help"], ["finish", "--help"]):
            with self.subTest(args=args):
                proc = subprocess.run([sys.executable, str(script), *args], capture_output=True, text=True, check=False)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("usage:", proc.stdout)

    def test_claude_skill_shim_check_passes(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(ROOT / ".remote-dev" / "tools" / "sync_claude_skills.py"), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_claude_skills_are_lightweight_shims(self) -> None:
        for source in sorted((ROOT / ".agents" / "skills").glob("*/SKILL.md")):
            target = ROOT / ".claude" / "skills" / source.parent.name / "SKILL.md"
            with self.subTest(skill=source.parent.name):
                self.assertTrue(target.exists())
                body = target.read_text(encoding="utf-8")
                self.assertIn(f"`.agents/skills/{source.parent.name}/SKILL.md`", body)
                self.assertLessEqual(len(body.splitlines()), 60)
                self.assertNotEqual(body, source.read_text(encoding="utf-8"))

    def test_vaws_ops_import_defers_agents_lib_dependency(self) -> None:
        # A standalone remote-dev deployment has no .agents/lib; importing the
        # module must not require it or touch that path at import time.
        code = (
            "import sys; sys.path.insert(0, r'%s'); import core.vaws_ops; "
            "print(any('.agents' in path for path in sys.path))" % REMOTE_DEV
        )
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "False")

    def test_json_arguments_are_not_overridden_by_argparse_defaults(self) -> None:
        code = """
import importlib.util, json, sys
from unittest import mock
spec = importlib.util.spec_from_file_location("vaws_cli", r"%s")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
captured = {}
def fake(name, args):
    captured.update(args)
    return {"result": {"outcome": "success"}}
argv = ["vaws.py", "execution", "--execution-id", "e1", "--json", json.dumps({"action": "stop"})]
with mock.patch.object(module, "vaws_call", side_effect=fake), mock.patch.object(sys, "argv", argv):
    module.main()
print(json.dumps(captured))
""" % (ROOT / ".agents/scripts/vaws.py")
        proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        merged = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertEqual(merged["action"], "stop")

    def test_cli_errors_return_result_contract_without_traceback(self) -> None:
        proc = subprocess.run(
            [
                sys.executable,
                str(ROOT / ".remote-dev" / "tools" / "remote_job_status.py"),
                "--job-id",
                "job-does-not-exist",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("Traceback", proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["result"]["schema_version"], "remote-dev.result.v1")
        self.assertEqual(payload["result"]["tool"], "remote.job_status")
        self.assertEqual(payload["result"]["outcome"], "needs_input")


if __name__ == "__main__":
    unittest.main()
