#!/usr/bin/env python3
"""Property tests for the shared validators (``.agents/lib/vaws_validate.py``).

Property: the accept/reject boundary is exactly where the docstrings and
error messages say it is. Each validator is compared against an independent
reference predicate over generated inputs that include the shapes an injection
would take (path separators, traversal, whitespace, shell metacharacters,
NUL, newlines, Unicode look-alikes, full-width and non-ASCII digits).
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_validate import ValidationError, ensure_child_path, parse_device_csv, require_env_name, require_remote_leaf, require_safe_id  # noqa: E402
from test_property_support import MULTIBYTE, Gen, run_cases  # noqa: E402

ASCII_ALNUM = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
SAFE_ID_EXTRA = "_.-"
HOSTILE = "/\\ \t\n\r\x00;$`'\"|&<>*?~()[]{}!#%^=+,:@" + MULTIBYTE + "\u0661\uff11\u212a\u0130\u00df"
UNICODE_DIGITS = "\u0660\u0661\u0662\u06f1\u0967\uff10\uff11\uff12\u0be7"


def reference_safe_id(value: object) -> bool:
    if not isinstance(value, str) or not 3 <= len(value) <= 64:
        return False
    if value[0] not in ASCII_ALNUM:
        return False
    return all(ch in ASCII_ALNUM or ch in SAFE_ID_EXTRA for ch in value[1:])


def reference_env_name(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value[0] not in ASCII_ALNUM[:52] + "_":
        return False
    return all(ch in ASCII_ALNUM or ch == "_" for ch in value)


def reference_devices(value: object) -> list[int] | None | str:
    """Return the parsed list, None for None, or the string 'reject'."""
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        return "reject"
    seen: set[int] = set()
    for token in value.split(","):
        token = token.strip()
        if not token or not all(ch in "0123456789" for ch in token):
            return "reject"
        device = int(token)
        if device in seen:
            return "reject"
        seen.add(device)
    return sorted(seen)


def hostile_string(gen: Gen) -> str:
    return gen.one_of(
        lambda: gen.text(ASCII_ALNUM + SAFE_ID_EXTRA, 0, 70),
        lambda: gen.text(ASCII_ALNUM + SAFE_ID_EXTRA + HOSTILE, 0, 70),
        lambda: gen.text(ASCII_ALNUM, 1, 5) + gen.choice(HOSTILE) + gen.text(ASCII_ALNUM, 0, 5),
        lambda: gen.choice(("", "..", ".", "...", "a/b", "../x", "/tmp/x", "a b", "abc\n", "abc\x00", "a" * 64, "a" * 65, "-abc", "_abc", ".abc", "ab", "abc")),
    )


class SafeIdProperties(unittest.TestCase):
    def test_safe_id_boundary_matches_reference(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            value = hostile_string(gen)
            expected = reference_safe_id(value)
            try:
                result = require_safe_id(value, label="job id")
            except ValidationError as exc:
                self.assertFalse(expected, f"reference accepts {value!r} but validator rejected")
                self.assertIn("job id", str(exc))
                return
            self.assertTrue(expected, f"validator accepted {value!r} but reference rejects")
            self.assertEqual(result, value, "accepted ids are returned unchanged, never normalized")

        run_cases(1500, body, label="require_safe_id")

    def test_safe_ids_are_single_path_segments_without_traversal(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            value = hostile_string(gen)
            try:
                require_safe_id(value)
            except ValidationError:
                return
            self.assertNotIn("/", value)
            self.assertNotIn("\\", value)
            self.assertNotIn("\x00", value)
            self.assertNotIn(value, {".", ".."})
            self.assertEqual(require_remote_leaf(value), value, "every safe id is a valid remote leaf")
            self.assertEqual(Path("/state") / value, Path("/state", value))
            self.assertEqual((Path("/state") / value).parent, Path("/state"))

        run_cases(800, body, label="safe id as path segment")

    def test_non_string_inputs_are_rejected(self) -> None:
        for value in (None, 123, b"abc", ["abc"], {"a": 1}, 3.5):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    require_safe_id(value)  # type: ignore[arg-type]
                with self.assertRaises(ValidationError):
                    require_env_name(value)  # type: ignore[arg-type]


class EnvNameProperties(unittest.TestCase):
    def test_env_name_boundary_matches_reference(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            value = gen.one_of(
                lambda: hostile_string(gen),
                lambda: gen.text(ASCII_ALNUM + "_", 0, 20),
                lambda: gen.choice(("VLLM_USE_V1", "_", "__", "A", "1A", "A-B", "A B", "A=B", "A;echo", "ÄB", "A\u200bB", "A\n", "\nA", "A" * 200)),
            )
            expected = reference_env_name(value)
            try:
                result = require_env_name(value)
            except ValidationError:
                self.assertFalse(expected, f"reference accepts {value!r} but validator rejected")
                return
            self.assertTrue(expected, f"validator accepted {value!r} but reference rejects")
            self.assertEqual(result, value)
            self.assertTrue(value.isidentifier() and value.isascii())

        run_cases(1500, body, label="require_env_name")


class DeviceCsvProperties(unittest.TestCase):
    def test_device_csv_boundary_matches_reference_on_ascii_inputs(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            tokens = []
            for _ in range(gen.integer(0, 5)):
                tokens.append(gen.one_of(
                    lambda: str(gen.integer(0, 15)),
                    lambda: str(gen.integer(0, 15)) + gen.choice(("", " ", "\t")),
                    lambda: gen.choice(("", " ", "-1", "abc", "0x1", "1.0", "1e2", "00", "007", " 3 ")),
                ))
            value = gen.choice((",".join(tokens), None, "", "   ", ","))
            expected = reference_devices(value)
            try:
                result = parse_device_csv(value)
            except ValidationError as exc:
                self.assertEqual(expected, "reject", f"reference accepts {value!r} but parser rejected: {exc}")
                self.assertIn("devices", str(exc))
                return
            self.assertNotEqual(expected, "reject", f"parser accepted {value!r} -> {result} but reference rejects")
            self.assertEqual(result, expected)
            if result is not None:
                self.assertEqual(result, sorted(set(result)))
                self.assertTrue(all(isinstance(d, int) and d >= 0 for d in result))

        run_cases(1200, body, label="parse_device_csv")

    @unittest.expectedFailure
    def test_known_defect_int_parsing_accepts_more_than_decimal_ascii_digits(self) -> None:
        """KNOWN DEFECT (low): tokens are parsed with ``int(token, 10)``, which
        also accepts a leading ``+``, digit-group underscores (``1_0`` -> 10)
        and any Unicode decimal digit (Arabic-Indic ``\u0661``, full-width
        ``\uff11`` -> 1). The documented boundary is "comma-separated device
        ids"; ``1_0`` silently becomes device 10.
        Evidence: ``parse_device_csv('1_0') == [10]``."""
        for value in ("1_0", "+1", "\u0661", "\uff11", "\u0660,\u0661"):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    parse_device_csv(value)

    def test_unicode_digit_inputs_never_crash(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            value = ",".join(gen.text(UNICODE_DIGITS + "0123456789_+", 1, 3) for _ in range(gen.integer(1, 3)))
            try:
                result = parse_device_csv(value)
            except ValidationError:
                return
            self.assertTrue(all(isinstance(d, int) and d >= 0 for d in result or []))

        run_cases(200, body, label="unicode device tokens")


class ChildPathProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name).resolve()
        self.root = base / "state"
        self.outside = base / "outside"
        (self.root / "a" / "b").mkdir(parents=True)
        self.outside.mkdir()
        (self.root / "escape_link").symlink_to(self.outside)
        (self.root / "inner_link").symlink_to(self.root / "a")

    def test_child_accepted_iff_its_resolved_location_is_under_root(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            parts = [gen.choice(("a", "b", "..", ".", "escape_link", "inner_link", "new", gen.text(ASCII_ALNUM, 1, 5))) for _ in range(gen.integer(0, 5))]
            child = self.root.joinpath(*parts) if parts else self.root
            if gen.boolean(0.15):
                child = self.outside.joinpath(*parts)
            resolved = child.resolve()
            expected = resolved == self.root or self.root in resolved.parents
            try:
                result = ensure_child_path(self.root, child)
            except ValidationError:
                self.assertFalse(expected, f"{child} resolves to {resolved} under root but was rejected")
                return
            self.assertTrue(expected, f"{child} -> {resolved} escapes {self.root} but was accepted")
            self.assertEqual(result, resolved)
            self.assertTrue(result.is_absolute())

        run_cases(600, body, label="ensure_child_path")

    def test_relative_children_resolve_against_cwd_not_root(self) -> None:
        # Documented contract: ``ensure_child_path`` resolves both sides with
        # ``Path.resolve``; a *relative* child is relative to the process cwd.
        original = os.getcwd()
        try:
            os.chdir(self.root)
            self.assertEqual(ensure_child_path(self.root, Path("a/b")), self.root / "a" / "b")
            with self.assertRaises(ValidationError):
                ensure_child_path(self.root, Path("../outside"))
            os.chdir(self.outside)
            with self.assertRaises(ValidationError):
                ensure_child_path(self.root, Path("x"))
        finally:
            os.chdir(original)


if __name__ == "__main__":
    unittest.main()
