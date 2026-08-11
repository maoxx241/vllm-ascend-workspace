#!/usr/bin/env python3

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
SCRIPTS = ROOT / ".agents" / "skills" / "vllm-ascend-serving" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from serve_start import service_runtime_dir  # noqa: E402


class ServingIdentityTests(unittest.TestCase):
    def test_alias_namespaces_runtime_directory(self) -> None:
        self.assertEqual(
            service_runtime_dir("/vllm-workspace", "20260811_120000", "agent12345"),
            "/vllm-workspace/.vaws-runtime/serving/agent12345/20260811_120000",
        )

    def test_missing_alias_preserves_legacy_layout(self) -> None:
        self.assertEqual(
            service_runtime_dir("/vllm-workspace", "20260811_120000", None),
            "/vllm-workspace/.vaws-runtime/serving/20260811_120000",
        )


if __name__ == "__main__":
    unittest.main()
