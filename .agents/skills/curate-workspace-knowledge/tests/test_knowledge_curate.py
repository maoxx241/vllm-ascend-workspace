"""Exercise the Markdown consumer against the installed package CLI."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[4]
SCRIPT = ROOT / '.agents/skills/curate-workspace-knowledge/scripts/knowledge_curate.py'
CAPTURE = ROOT / '.agents/scripts/knowledge_capture.py'


class MarkdownCurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / 'pending'
        self.public = self.root / 'public'
        self.env = {**os.environ, 'VAWS_KNOWLEDGE_BACKEND': 'memory'}

    def run_cli(self, script, *args):
        return subprocess.run([sys.executable, str(script), *map(str, args)],
                              cwd=ROOT, env=self.env, text=True,
                              capture_output=True, timeout=30)

    def prepare(self, candidate):
        result = self.run_cli(SCRIPT, 'prepare', '--candidate', candidate,
                              '--state-root', self.state, '--public-root', self.public)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_capture_prepare_preserves_markdown_and_reuses_pending_identity(self):
        result = self.run_cli(CAPTURE, '--title', 'Interactive EOF ordering',
                              '--content', 'Close stdin only after accepting the complete payload.',
                              '--candidate-dir', self.root / 'candidate',
                              '--knowledge-dir', self.root / 'project')
        self.assertEqual(result.returncode, 0, result.stderr)
        capture = json.loads(result.stdout)
        candidate = Path(capture['path'])
        original = candidate.read_bytes()
        first = self.prepare(candidate)
        second = self.prepare(candidate)
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(first['content_digest'], second['content_digest'])
        self.assertEqual(candidate.read_bytes(), original)
        public = self.public / first['public_relpath']
        self.assertTrue(public.is_file())
        self.assertIn('Close stdin only after accepting', public.read_text())
        self.assertEqual(len(list(self.public.glob('*.md'))), 1)

    def test_invalid_markdown_is_blocked_without_public_copy(self):
        candidate = self.root / 'invalid.md'
        candidate.write_text('No heading or frontmatter title.\n')
        original = candidate.read_bytes()
        record = self.prepare(candidate)
        self.assertEqual(record['status'], 'blocked_redaction')
        self.assertEqual(candidate.read_bytes(), original)
        self.assertFalse(list(self.public.glob('*.md')))

    def test_package_commands_replace_retired_yaml_verbs(self):
        help_result = self.run_cli(SCRIPT, '--help')
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn('prepare', help_result.stdout)
        self.assertIn('review', help_result.stdout)
        old = self.run_cli(SCRIPT, 'promote')
        self.assertEqual(old.returncode, 2)
        self.assertIn('invalid choice', old.stderr)


if __name__ == '__main__':
    unittest.main()
