"""Property tests for the command classifiers in ``core.permissions``.

These detectors are advisory today (the hooks in ``.remote-dev/hooks`` are
permissive by default), so the properties pin the contract they *do* offer:
a secret-shaped assignment anywhere in a command is detected regardless of
case or surrounding text, and a raw transport invocation at a shell-word
boundary is detected. The known-defect test records where the word-boundary
rule misses transports glued to shell operators.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.permissions import contains_raw_remote_transport, contains_secret_in_argv  # noqa: E402
from test_property_support import Gen, run_cases  # noqa: E402

SECRET_SHAPES = ("--password={v}", "--password {v}", "password={v}", "token={v}", "api_key={v}", "api-key={v}", "apikey={v}")
TRANSPORTS = ("ssh", "scp", "sftp", "rsync")
FILLER = "abcdefghijklmnopqrstuvwxyz0123456789 -_./=:"


def mixed_case(gen: Gen, text: str) -> str:
    return "".join(ch.upper() if gen.boolean() else ch.lower() for ch in text)


class SecretDetectorProperties(unittest.TestCase):
    def test_secret_assignment_anywhere_is_detected_case_insensitively(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            value = gen.text("abcXYZ019!@#$%^&*", 1, 12)
            shape = mixed_case(gen, gen.choice(SECRET_SHAPES)).replace("{V}", value).replace("{v}", value)
            command = gen.text(FILLER, 0, 20) + gen.choice((" ", "")) + shape + gen.choice((" ", "")) + gen.text(FILLER, 0, 20)
            self.assertTrue(contains_secret_in_argv(command), command)

        run_cases(400, body, label="secret detection")

    def test_commands_without_secret_shapes_are_not_flagged(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            words = [gen.choice(("python3", "ls", "-la", "grep", "expected_sha256=abc", "tokens", "passwords.txt", "my_token_count=3", "--pass", "keyring")) for _ in range(gen.integer(1, 5))]
            command = " ".join(words)
            self.assertFalse(contains_secret_in_argv(command), command)

        run_cases(200, body, label="secret false positives")


class TransportDetectorProperties(unittest.TestCase):
    def test_transport_as_a_whitespace_separated_word_is_detected(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            transport = gen.choice(TRANSPORTS)
            prefix = gen.choice(("", "cd /tmp && ", "VAR=1 ", "\t", "  "))
            if prefix and not prefix[-1].isspace():
                prefix += " "
            command = prefix + transport + gen.choice((" root@192.0.2.10", " -p 46000 host", "")) + gen.choice((" " + gen.text(FILLER, 0, 10), ""))
            self.assertTrue(contains_raw_remote_transport(command), command)
            self.assertFalse(contains_raw_remote_transport(command.replace(transport, transport + "d", 1)), "suffix must break the word")

        run_cases(300, body, label="transport detection")

    @unittest.expectedFailure
    def test_known_defect_transport_glued_to_a_shell_operator_is_missed(self) -> None:
        """KNOWN DEFECT (low, advisory path): ``RAW_REMOTE_RE`` requires the
        transport to follow start-of-string or whitespace, so ``;ssh``, ``|ssh``,
        ``&&ssh``, ``$(ssh`` and ``(ssh`` are not detected. The hooks are
        permissive today, so this only weakens future enforcement.
        Evidence: ``contains_raw_remote_transport('true;ssh host') is False``."""
        for command in ("true;ssh host", "cat x|ssh host", "a&&ssh host", "$(ssh host)", "(ssh host)"):
            with self.subTest(command=command):
                self.assertTrue(contains_raw_remote_transport(command))


if __name__ == "__main__":
    unittest.main()
