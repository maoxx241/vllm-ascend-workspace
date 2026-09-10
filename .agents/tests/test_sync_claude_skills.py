#!/usr/bin/env python3
"""Tests for generated Claude shims and the ModelScope Trae projection."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".agents" / "scripts" / "sync_claude_skills.py"
CATALOG_SCRIPT = ROOT / ".agents" / "scripts" / "skill_catalog.py"
CANONICAL_MODELSCOPE = ROOT / ".agents" / "skills" / "modelscope"
TRAE_MODELSCOPE = ROOT / ".trae" / "skills" / "modelscope"
FOREIGN_TRAE_SKILL = ROOT / ".trae" / "skills" / "repo-init" / "SKILL.md"
MODELSCOPE_SCRIPTS = (
    "modelscope_auto.py",
    "download_from_modelscope.py",
    "modelscope_download_status.py",
    "verify_modelscope_sha256.py",
)
HELP_MARKERS = {
    "modelscope_auto.py": ("ensure", "status", "verify", "worker"),
    "download_from_modelscope.py": ("--model-id", "--local-dir", "--revision"),
    "modelscope_download_status.py": ("--model", "--revision"),
    "verify_modelscope_sha256.py": ("--model", "--output-dir"),
}


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sync = load_module(SCRIPT, "_sync_claude_skills_test")
catalog = load_module(CATALOG_SCRIPT, "_skill_catalog_for_sync_test")


def _unquote_yaml_scalar(raw: str) -> object:
    if raw in {"true", "True"}:
        return True
    if raw in {"false", "False"}:
        return False
    if raw in {"null", "Null", "~"}:
        return None
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {'"', "'"}:
        return raw[1:-1]
    return raw


def parse_yaml_mapping(text: str) -> dict[str, object]:
    """Parse a nested block mapping. Used when PyYAML is not a package dependency."""
    root: dict[str, object] = {}
    stack: list[tuple[int, dict[str, object]]] = [(-1, root)]
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if ":" not in stripped:
            raise ValueError(f"invalid YAML mapping line: {raw_line!r}")
        key, remainder = stripped.split(":", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"invalid YAML key: {raw_line!r}")
        value_text = remainder.strip()
        while indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value_text == "":
            nested: dict[str, object] = {}
            parent[key] = nested
            stack.append((indent, nested))
        else:
            parent[key] = _unquote_yaml_scalar(value_text)
    return root


def frontmatter_yaml(text: str) -> str:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("missing opening YAML frontmatter delimiter")
    try:
        end = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError("missing closing YAML frontmatter delimiter") from exc
    return "\n".join(lines[1:end]) + "\n"


def write_owned_modelscope(package: Path, payload_prefix: str) -> None:
    for relative in sync.MODELSCOPE_TRAE_PATHS:
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{payload_prefix}:{relative}\n", encoding="utf-8")
        if relative.endswith(".py"):
            path.chmod(path.stat().st_mode | 0o111)


def snapshot_package(package: Path) -> dict[str, tuple[bytes, int]]:
    snapshot: dict[str, tuple[bytes, int]] = {}
    for relative in sync.MODELSCOPE_TRAE_PATHS:
        path = package / relative
        snapshot[relative] = (path.read_bytes(), path.stat().st_mode & 0o111)
    return snapshot


def deny_network_pythonpath(root: Path) -> str:
    package = root / "requests"
    package.mkdir()
    (package / "__init__.py").write_text(
        "class Session:\n"
        "    def get(self, *args, **kwargs):\n"
        "        raise RuntimeError("
        "'network and ModelScope provider calls are denied in this fixture')\n",
        encoding="utf-8",
    )
    return str(root)


class ModelScopeTraeProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.agents_skills = self.root / "agents-skills"
        self.claude_skills = self.root / "claude-skills"
        self.trae_skills = self.root / "trae-skills"
        self.canonical = self.agents_skills / "modelscope"
        self.projected = self.trae_skills / "modelscope"
        self.foreign = self.trae_skills / "other-skill" / "SKILL.md"
        self.agents_skills.mkdir()
        self.claude_skills.mkdir()
        self.trae_skills.mkdir()
        write_owned_modelscope(self.canonical, "canonical")
        self.foreign.parent.mkdir()
        self.foreign.write_text("foreign-skill-body\n", encoding="utf-8")
        sync.AGENTS_SKILLS = self.agents_skills
        sync.CLAUDE_SKILLS = self.claude_skills
        sync.TRAE_SKILLS = self.trae_skills
        shim_dir = self.claude_skills / "modelscope"
        shim_dir.mkdir()
        (shim_dir / "SKILL.md").write_text(
            sync.expected_skill_body(self.canonical),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        sync.AGENTS_SKILLS = ROOT / ".agents" / "skills"
        sync.CLAUDE_SKILLS = ROOT / ".claude" / "skills"
        sync.TRAE_SKILLS = ROOT / ".trae" / "skills"
        self._tmp.cleanup()

    def test_mapping_is_the_six_modelscope_outputs(self) -> None:
        self.assertEqual(
            sync.MODELSCOPE_TRAE_PATHS,
            (
                "SKILL.md",
                "agents/openai.yaml",
                "scripts/download_from_modelscope.py",
                "scripts/modelscope_auto.py",
                "scripts/modelscope_download_status.py",
                "scripts/verify_modelscope_sha256.py",
            ),
        )

    def test_generate_copies_exact_bytes_and_preserves_foreign_package(self) -> None:
        self.assertEqual(sync.sync_modelscope_trae(), None)
        for relative in sync.MODELSCOPE_TRAE_PATHS:
            source = self.canonical / relative
            target = self.projected / relative
            self.assertEqual(source.read_bytes(), target.read_bytes(), relative)
            self.assertEqual(
                source.stat().st_mode & 0o111,
                target.stat().st_mode & 0o111,
                relative,
            )
        self.assertEqual(self.foreign.read_text(encoding="utf-8"), "foreign-skill-body\n")
        self.assertEqual(sync.check_modelscope_trae(), [])

    def test_generate_is_idempotent(self) -> None:
        sync.sync_modelscope_trae()
        first = snapshot_package(self.projected)
        sync.sync_modelscope_trae()
        second = snapshot_package(self.projected)
        self.assertEqual(first, second)
        self.assertEqual(sync.check_modelscope_trae(), [])
        with redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(sync.main(["--check"]), 0)
        self.assertEqual(captured.getvalue(), "")

    def test_check_detects_projected_file_drift_then_regenerate_restores(self) -> None:
        sync.sync_modelscope_trae()
        drifted = self.projected / "SKILL.md"
        drifted.write_text("projected-drift\n", encoding="utf-8")
        errors = sync.check_modelscope_trae()
        self.assertIn("stale Trae modelscope projection: SKILL.md", errors)
        with redirect_stdout(io.StringIO()) as captured:
            self.assertEqual(sync.main(["--check"]), 1)
        self.assertIn("stale Trae modelscope projection: SKILL.md", captured.getvalue())
        sync.sync_modelscope_trae()
        self.assertEqual((self.canonical / "SKILL.md").read_bytes(), drifted.read_bytes())
        self.assertEqual(sync.check_modelscope_trae(), [])

    def test_check_detects_canonical_edit_until_regenerated(self) -> None:
        sync.sync_modelscope_trae()
        canonical_script = self.canonical / "scripts" / "modelscope_auto.py"
        canonical_script.write_text("canonical-edit:modelscope_auto.py\n", encoding="utf-8")
        canonical_script.chmod(canonical_script.stat().st_mode | 0o111)
        errors = sync.check_modelscope_trae()
        self.assertIn(
            "stale Trae modelscope projection: scripts/modelscope_auto.py",
            errors,
        )
        sync.sync_modelscope_trae()
        self.assertEqual(
            canonical_script.read_bytes(),
            (self.projected / "scripts" / "modelscope_auto.py").read_bytes(),
        )
        self.assertEqual(sync.check_modelscope_trae(), [])

    def test_check_detects_missing_projected_file(self) -> None:
        sync.sync_modelscope_trae()
        (self.projected / "agents" / "openai.yaml").unlink()
        errors = sync.check_modelscope_trae()
        self.assertIn("missing Trae modelscope projection: agents/openai.yaml", errors)

    def test_unexpected_projection_file_is_reported_and_not_deleted(self) -> None:
        sync.sync_modelscope_trae()
        extra = self.projected / "notes.txt"
        extra.write_text("user-owned extra\n", encoding="utf-8")
        errors = sync.check_modelscope_trae()
        self.assertIn("unexpected file in Trae modelscope projection: notes.txt", errors)
        sync.sync_modelscope_trae()
        self.assertEqual(extra.read_text(encoding="utf-8"), "user-owned extra\n")
        self.assertEqual(self.foreign.read_text(encoding="utf-8"), "foreign-skill-body\n")
        self.assertIn(
            "unexpected file in Trae modelscope projection: notes.txt",
            sync.check_modelscope_trae(),
        )


class CurrentTreeProjectionTests(unittest.TestCase):
    def test_canonical_and_trae_payloads_match(self) -> None:
        owned = set(sync.MODELSCOPE_TRAE_PATHS)
        observed = {
            path.relative_to(TRAE_MODELSCOPE).as_posix()
            for path in TRAE_MODELSCOPE.rglob("*")
            if path.is_file() and "__pycache__" not in path.parts
        }
        self.assertEqual(observed, owned)
        for relative in sync.MODELSCOPE_TRAE_PATHS:
            source = CANONICAL_MODELSCOPE / relative
            target = TRAE_MODELSCOPE / relative
            self.assertEqual(source.read_bytes(), target.read_bytes(), relative)
            self.assertEqual(
                source.stat().st_mode & 0o111,
                target.stat().st_mode & 0o111,
                relative,
            )

    def test_check_passes_on_current_tree(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--check"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(proc.stdout, "")

    def test_foreign_trae_package_remains_on_current_tree(self) -> None:
        self.assertTrue(FOREIGN_TRAE_SKILL.is_file())
        body = FOREIGN_TRAE_SKILL.read_text(encoding="utf-8")
        self.assertIn("repo-init", body)
        self.assertNotEqual(body, (CANONICAL_MODELSCOPE / "SKILL.md").read_text(encoding="utf-8"))

    def test_all_claude_shims_expose_the_canonical_metadata(self) -> None:
        for source in sync.source_skill_dirs():
            target = ROOT / ".claude/skills" / source.name / "SKILL.md"
            with self.subTest(skill=source.name):
                import yaml
                expected = yaml.safe_load(frontmatter_yaml((source / "SKILL.md").read_text()))
                actual = yaml.safe_load(frontmatter_yaml(target.read_text()))
                self.assertEqual(actual, expected)

    def test_skill_frontmatter_parses_through_catalog(self) -> None:
        for skill_file in (
            CANONICAL_MODELSCOPE / "SKILL.md",
            TRAE_MODELSCOPE / "SKILL.md",
        ):
            record = catalog.parse_skill(skill_file, ROOT)
            self.assertEqual(record.name, "modelscope")
            self.assertIn("ModelScope", record.description)
            self.assertGreater(len(record.description), 20)

    def test_modelscope_yaml_documents_parse(self) -> None:
        try:
            import yaml
        except ImportError:
            yaml = None
        for skill_file in (
            CANONICAL_MODELSCOPE / "SKILL.md",
            TRAE_MODELSCOPE / "SKILL.md",
        ):
            frontmatter = frontmatter_yaml(skill_file.read_text(encoding="utf-8"))
            metadata = parse_yaml_mapping(frontmatter)
            self.assertEqual(metadata["name"], "modelscope")
            self.assertIsInstance(metadata["description"], str)
            self.assertIn("ModelScope", metadata["description"])
            if yaml is not None:
                loaded = yaml.safe_load(frontmatter)
                self.assertEqual(loaded["name"], metadata["name"])
                self.assertEqual(loaded["description"], metadata["description"])
        for yaml_file in (
            CANONICAL_MODELSCOPE / "agents" / "openai.yaml",
            TRAE_MODELSCOPE / "agents" / "openai.yaml",
        ):
            text = yaml_file.read_text(encoding="utf-8")
            document = parse_yaml_mapping(text)
            interface = document["interface"]
            self.assertIsInstance(interface, dict)
            self.assertEqual(interface["display_name"], "ModelScope")
            self.assertIn("ModelScope", interface["short_description"])
            self.assertIn("$modelscope", interface["default_prompt"])
            if yaml is not None:
                loaded = yaml.safe_load(text)
                self.assertEqual(loaded["interface"]["display_name"], interface["display_name"])
                self.assertEqual(
                    loaded["interface"]["short_description"],
                    interface["short_description"],
                )
                self.assertEqual(
                    loaded["interface"]["default_prompt"],
                    interface["default_prompt"],
                )

    def test_generator_help_names_trae_output(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--help"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("ModelScope Trae", proc.stdout)
        self.assertIn(".trae/skills/modelscope", proc.stdout)
        self.assertIn("--check", proc.stdout)


class ModelScopeHelpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._tmpdir = tempfile.TemporaryDirectory()
        cls.pythonpath = deny_network_pythonpath(Path(cls._tmpdir.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmpdir.cleanup()

    def _run_help(
        self, script: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            self.pythonpath if not existing else self.pythonpath + os.pathsep + existing
        )
        for name in (
            "HTTP_PROXY",
            "HTTPS_PROXY",
            "ALL_PROXY",
            "http_proxy",
            "https_proxy",
            "all_proxy",
            "MODELSCOPE_TOKEN",
            "MODELSCOPE_API_TOKEN",
        ):
            env.pop(name, None)
        return subprocess.run(
            [sys.executable, str(script), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
            timeout=20,
        )

    def test_canonical_and_trae_help_match(self) -> None:
        for name in MODELSCOPE_SCRIPTS:
            with self.subTest(script=name):
                canonical = self._run_help(
                    CANONICAL_MODELSCOPE / "scripts" / name, "--help"
                )
                trae = self._run_help(TRAE_MODELSCOPE / "scripts" / name, "--help")
                self.assertEqual(canonical.returncode, 0, canonical.stderr)
                self.assertEqual(trae.returncode, 0, trae.stderr)
                self.assertEqual(canonical.stdout, trae.stdout)
                self.assertEqual(canonical.stderr, trae.stderr)
                self.assertIn("usage:", canonical.stdout)
                for marker in HELP_MARKERS[name]:
                    self.assertIn(marker, canonical.stdout)
                if name == "modelscope_auto.py":
                    for command in ("ensure", "status", "verify"):
                        canonical_sub = self._run_help(
                            CANONICAL_MODELSCOPE / "scripts" / name,
                            command,
                            "--help",
                        )
                        trae_sub = self._run_help(
                            TRAE_MODELSCOPE / "scripts" / name,
                            command,
                            "--help",
                        )
                        self.assertEqual(canonical_sub.returncode, 0, canonical_sub.stderr)
                        self.assertEqual(trae_sub.stdout, canonical_sub.stdout)
                        self.assertIn("--model", canonical_sub.stdout)


if __name__ == "__main__":
    unittest.main()
