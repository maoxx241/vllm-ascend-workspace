"""Markdown capture/query stays with the package; workspace supplies its roots."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vaws_knowledge_service import knowledge_server_env, query_experience, query_knowledge, service_config
from vaws_knowledge.markdown import load_document
from vaws_knowledge.local.instance import instance_for_config
from vaws_knowledge.server.capture import capture
from vaws_knowledge.server.layers import load_config

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

    def test_client_environment_and_optional_lookup_work_without_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            code = (
                "import json, sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(ROOT / '.agents/lib')!r})\n"
                "from vaws_knowledge_service import knowledge_server_env, query_knowledge\n"
                f"root = Path({temporary!r})\n"
                "print(json.dumps({'env': knowledge_server_env(root), "
                "'lookup': query_knowledge(knowledge_dir=root / '.agents/knowledge', query='example')}))\n"
            )
            result = subprocess.run([sys.executable, "-I", "-S", "-c", code],
                                    capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(Path(payload["env"]["VAWS_KNOWLEDGE_PROJECT_ROOTS"]), Path(temporary).resolve() / ".agents/knowledge")
        self.assertTrue(payload["lookup"]["unavailable"])

    def test_mcp_and_hook_roots_agree_with_nested_service_config(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / ".vaws-local/knowledge/service.json"
            path.parent.mkdir(parents=True)
            (root / ".agents/knowledge").mkdir(parents=True)
            path.write_text(json.dumps({"backend": "memory", "layers": {
                "project": {"roots": [str(root / ".agents/knowledge")]},
                "candidate": {"root": str(root / ".vaws-local/knowledge/candidate")}}}), encoding="utf-8")
            mcp = load_config(env=knowledge_server_env(root))
            hook = service_config(root)
            for layer in ("project", "candidate"):
                self.assertEqual(mcp.mount(layer).roots, hook.mount(layer).roots)
                self.assertTrue(all(path.is_absolute() for path in mcp.mount(layer).roots))

    def test_client_wiring_does_not_require_git_for_optional_origin_label(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch("vaws_knowledge_service.subprocess.run", side_effect=FileNotFoundError("git")):
            env = knowledge_server_env(Path(temporary))
        self.assertEqual(env["VAWS_KNOWLEDGE_ORIGIN_REPO"], "local/unpublished")

    def test_existing_config_mounts_and_state_are_shared_by_mcp_and_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / ".vaws-local/knowledge/service.json"
            path.parent.mkdir(parents=True)
            project, candidate, state = (root / "custom" / name for name in ("project", "candidate", "state"))
            path.write_text(json.dumps({"backend": "memory", "state_root": str(state),
                "publishing": {"enabled": False}, "layers": {
                    "project": {"roots": [str(project)]}, "candidate": {"root": str(candidate)}}}), encoding="utf-8")
            environment = knowledge_server_env(root)
            self.assertNotIn("VAWS_KNOWLEDGE_PROJECT_ROOTS", environment)
            self.assertNotIn("VAWS_KNOWLEDGE_CANDIDATE_ROOT", environment)
            self.assertNotIn("VAWS_KNOWLEDGE_STATE", environment)
            mcp, hook = load_config(env=environment), service_config(root)
            for config in (mcp, hook):
                self.assertEqual(config.mount("project").roots, (project,))
                self.assertEqual(config.mount("candidate").roots, (candidate,))
                self.assertEqual(instance_for_config(config).state_root, state)
            result = capture(title="Existing observation", content="Useful existing summary.", config=hook, index=False)
            self.assertTrue(Path(result["path"]).is_relative_to(candidate))
            self.assertFalse((root / ".vaws-local/knowledge/candidate").exists())

    def test_unprepared_workspace_mcp_and_summary_use_the_same_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            mcp = load_config(env=knowledge_server_env(root))
            hook = service_config(root)
            self.assertEqual(instance_for_config(mcp).state_root, root / ".vaws-local/knowledge/instance")
            self.assertEqual(instance_for_config(mcp).state_root, instance_for_config(hook).state_root)
            for config in (mcp, hook):
                experience = config.for_kind("experience")
                self.assertEqual(experience.mount("project").roots, (root / ".agents/experiences",))
                self.assertEqual(experience.mount("candidate").roots, (root / ".vaws-local/experience/candidate",))
                self.assertEqual(instance_for_config(experience).state_root, instance_for_config(config).state_root)

    def test_explicit_experience_mounts_are_shared_by_mcp_hook_and_prepare(self):
        from vaws_knowledge.maintenance import project_config
        from vaws_knowledge.summary_hook import capture_summary

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / ".vaws-local/knowledge/service.json"
            path.parent.mkdir(parents=True)
            project, candidate = root / "custom-project", root / "custom-experiences"
            path.write_text(json.dumps({"backend": "memory", "experience": {"layers": {
                "project": {"roots": [str(project)]}, "candidate": {"root": str(candidate)}}},
                "publishing": {"enabled": False}}), encoding="utf-8")
            prepared = project_config(root)
            for config in (prepared, load_config(env=knowledge_server_env(root)), service_config(root)):
                experience = config.for_kind("experience")
                self.assertEqual(experience.mount("project").roots, (project,))
                self.assertEqual(experience.mount("candidate").roots, (candidate,))
            saved = capture_summary({"hook_event_name": "Stop", "last_assistant_message":
                "The original workaround helped this run, although the cause remains uncertain."},
                config=service_config(root), client="codex")
            self.assertEqual(saved["kind"], "experience")
            self.assertEqual(len(list(candidate.glob("*.md"))), 1)
            self.assertFalse((root / ".vaws-local/knowledge/candidate").exists())

    def test_prepare_supplies_both_default_roots_without_reclassifying_old_notes(self):
        from vaws_knowledge.maintenance import project_config

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            old = root / ".agents/knowledge/old.md"
            old.parent.mkdir(parents=True)
            old.write_text("# Older note\n\nHistorical observation awaiting review.\n", encoding="utf-8")
            config = project_config(root)
            self.assertEqual(config.for_kind("experience").mount("project").roots, (root / ".agents/experiences",))
            self.assertEqual(config.for_kind("experience").mount("candidate").roots, (root / ".vaws-local/experience/candidate",))
            self.assertTrue(old.is_file())
            self.assertFalse((root / ".agents/experiences/old.md").exists())

    def test_optional_experience_lookup_selects_only_experience(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / ".agents/experiences"
            with mock.patch("vaws_knowledge.server.query.query") as query:
                query.return_value.to_dict.return_value = {"kind": "experience", "results": []}
                result = query_experience(experience_dir=directory, query="old workaround")
            config = query.call_args.args[0]
            self.assertEqual(config.kind, "experience")
            self.assertEqual(config.mount("project").roots, (directory,))
            self.assertEqual(result["kind"], "experience")

    def test_existing_experience_config_retains_all_roots_with_a_project_override(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            path = root / ".vaws-local/knowledge/service.json"
            path.parent.mkdir(parents=True)
            roots = {layer: root / "custom-experience" / layer for layer in ("shared", "project", "candidate")}
            for directory in roots.values():
                directory.mkdir(parents=True)
            payload = {"backend": "memory", "experience": {"layers": {
                layer: {"roots": [str(directory)]} for layer, directory in roots.items()}}}
            path.write_text(json.dumps(payload), encoding="utf-8")

            configured = service_config(root, kind="experience")
            for layer, directory in roots.items():
                self.assertEqual(configured.mount(layer).roots, (directory,))

            replacement = root / "replacement-project"
            replacement.mkdir()
            overridden = service_config(root, kind="experience", project_root=replacement)
            self.assertEqual(overridden.mount("project").roots, (replacement,))
            for layer in ("candidate", "shared"):
                self.assertEqual(overridden.mount(layer).roots, (roots[layer],))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), payload)

    def test_historical_observation_retains_its_source_and_limits(self):
        root = ROOT / ".agents/knowledge"
        document = load_document(root / "tensor-graph-capture-observation.md", layer="project", root=root)
        self.assertIn("migrated 2026-09-11", document.content)
        self.assertIn("original run identifier are unavailable", document.content)
        self.assertIn("114", document.content)
        self.assertIn("contextual evidence only", document.content)


if __name__ == "__main__":
    unittest.main()
