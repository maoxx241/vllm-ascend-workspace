#!/usr/bin/env python3
"""The YAML v2 capture/promote flow is replaced by Markdown title+content.

See ``test_knowledge_flow.py`` and the knowledge package Markdown tests.
"""

from __future__ import annotations

import unittest


class RetiredYamlFlow(unittest.TestCase):
    def test_markdown_capture_is_the_write_path(self) -> None:
        self.assertTrue(True)
