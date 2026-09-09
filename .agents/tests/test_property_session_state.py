#!/usr/bin/env python3
"""Property tests for local report-id canonicalization.

Task identity is the native context file. This suite only covers
``normalize_session_id`` used for local business-report filenames.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_session_id import normalize_session_id  # noqa: E402
from test_property_support import MULTIBYTE, Gen, run_cases  # noqa: E402

ID_CHARS = "abcXYZ019._-/ \t:@#" + MULTIBYTE


class NormalizeSessionIdProperties(unittest.TestCase):
    def test_output_is_none_or_a_bounded_canonical_id(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            raw = gen.one_of(
                lambda: gen.text(ID_CHARS, 0, 30),
                lambda: gen.text(ID_CHARS, 60, 200),
                lambda: gen.choice(("", "..", "---", "a", "ab", "abc", "A-B_C.d", "  x y z  ", "pr/12", "\u0130stanbul", "\u212aelvin")),
            )
            result = normalize_session_id(raw)
            if result is None:
                return
            self.assertTrue(3 <= len(result) <= 64, result)
            self.assertRegex(result, r"^[a-z0-9._-]+$")
            self.assertNotIn(result[0], ".-_")
            self.assertNotIn(result[-1], ".-_")
            self.assertNotIn("--", result)
            self.assertEqual(normalize_session_id(result), result, "normalization must be idempotent")
            self.assertEqual(normalize_session_id(raw), result, "normalization must be deterministic")
            self.assertTrue(result.isascii())

        run_cases(1500, body, label="normalize_session_id")

    def test_canonical_ids_are_fixed_points(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            middle = gen.text("abcxyz019._-", 1, 62).replace("--", "-x")
            value = gen.text("abcxyz019", 1, 1) + middle + gen.text("abcxyz019", 1, 1)
            self.assertEqual(normalize_session_id(value), value)

        run_cases(400, body, label="fixed points")

    def test_long_inputs_that_share_a_prefix_stay_distinct(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            prefix = gen.text("abcxyz019", 70, 90)
            a = normalize_session_id(prefix + gen.text("abc", 1, 4))
            b = normalize_session_id(prefix + gen.text("xyz", 1, 4))
            self.assertIsNotNone(a)
            self.assertIsNotNone(b)
            self.assertNotEqual(a, b)
            self.assertLessEqual(len(a or ""), 64)

        run_cases(200, body, label="long id distinctness")


if __name__ == "__main__":
    unittest.main()
