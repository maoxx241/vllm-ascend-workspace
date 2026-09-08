"""Seeded, dependency-free support for the ``test_property_*`` suites.

This file intentionally duplicates (apart from this note)
``.remote-dev/tests/test_property_support.py``: the two test trees are
discovered independently and ``.remote-dev`` is being extracted into its own
repository, so neither tree may import from the other. Keep both in sync.

``hypothesis`` is deliberately not a dependency of this repository, so the
property suites use this small generator instead. Two rules make failures
reproducible:

* Every case derives its own ``random.Random`` seed from the suite seed and
  the case index, so a failing case can be re-run in isolation.
* The suite seed and the case-count scale are read from the environment
  (``VAWS_PROPTEST_SEED``, ``VAWS_PROPTEST_SCALE``) and printed in every
  failure message.

The module also carries a self-test so that a regression in the generator
itself is caught by the same ``unittest discover`` invocation.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import random
import sys
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

DEFAULT_SEED = 20260907
SEED = int(os.environ.get("VAWS_PROPTEST_SEED", str(DEFAULT_SEED)))
SCALE = float(os.environ.get("VAWS_PROPTEST_SCALE", "1"))

# Reserved documentation values only (RFC 5737 / RFC 2606); never real hosts.
DOC_HOSTS = ("192.0.2.10", "198.51.100.7", "203.0.113.5", "npu-a.example.invalid")

ASCII_WORD = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
ASCII_PUNCT = "!\"#$%&'()*+,-./:;<=>?@[\\]^`{|}~ "
# Characters that ``str.splitlines`` treats as line boundaries besides ``\n``.
SPLITLINES_EXTRA = "\r\x0b\x0c\x1c\x1d\x1e\x85\u2028\u2029"
MULTIBYTE = "é漢字🙂\u0301\u200d"


def case_count(base: int) -> int:
    return max(1, int(round(base * SCALE)))


class Gen:
    """Thin wrapper over ``random.Random`` with generators used by the suites."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.rng = random.Random(seed)

    def integer(self, low: int, high: int) -> int:
        return self.rng.randint(low, high)

    def boolean(self, probability: float = 0.5) -> bool:
        return self.rng.random() < probability

    def choice(self, values: Sequence[Any]) -> Any:
        return self.rng.choice(list(values))

    def sample(self, values: Sequence[Any], k: int) -> list[Any]:
        return self.rng.sample(list(values), k)

    def shuffled(self, values: Sequence[Any]) -> list[Any]:
        items = list(values)
        self.rng.shuffle(items)
        return items

    def subset(self, values: Sequence[Any]) -> list[Any]:
        return [item for item in values if self.boolean()]

    def text(self, alphabet: str, min_len: int, max_len: int) -> str:
        length = self.integer(min_len, max_len)
        return "".join(self.rng.choice(alphabet) for _ in range(length))

    def word(self, min_len: int = 1, max_len: int = 8) -> str:
        return self.text(ASCII_WORD, min_len, max_len)

    def lines(self, count: int, *, alphabet: str = ASCII_WORD + " ", max_len: int = 12) -> list[str]:
        return [self.text(alphabet, 0, max_len) + "\n" for _ in range(count)]

    def raw_bytes(self, min_len: int, max_len: int) -> bytes:
        return self.rng.randbytes(self.integer(min_len, max_len))

    def one_of(self, *makers: Callable[[], Any]) -> Any:
        return self.rng.choice(makers)()

    def json_scalar(self) -> Any:
        return self.one_of(
            lambda: self.word(0, 6),
            lambda: self.integer(-1000, 1000),
            lambda: self.boolean(),
            lambda: None,
        )

    def json_object(self, depth: int = 2, max_keys: int = 4) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for _ in range(self.integer(0, max_keys)):
            key = self.word(1, 6)
            if depth > 0 and self.boolean(0.3):
                result[key] = self.json_object(depth - 1, max_keys)
            elif depth > 0 and self.boolean(0.2):
                result[key] = [self.json_scalar() for _ in range(self.integer(0, 3))]
            else:
                result[key] = self.json_scalar()
        return result


def case_seed(index: int, *, seed: int = SEED) -> int:
    digest = hashlib.sha256(f"{seed}:{index}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def run_cases(
    count: int,
    body: Callable[[Gen, int], None],
    *,
    seed: int = SEED,
    label: str = "property",
) -> int:
    """Run ``body(gen, index)`` for ``case_count(count)`` seeded cases.

    Failures are re-raised with the suite seed and the per-case seed so the
    exact case can be reproduced with ``VAWS_PROPTEST_SEED``.
    """
    total = case_count(count)
    for index in range(total):
        current = case_seed(index, seed=seed)
        try:
            body(Gen(current), index)
        except Exception as exc:  # noqa: BLE001 - re-raise with reproduction info
            raise AssertionError(
                f"{label}: case #{index}/{total} failed "
                f"(VAWS_PROPTEST_SEED={seed}, case_seed={current}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    return total


def run_remote_script(code: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one of the remote-side executor scripts in-process.

    The ``REMOTE_*_PY`` scripts read a JSON payload from stdin, print one JSON
    object and exit via ``SystemExit(0)``. Running them in-process (instead of
    through ``python3 -c``) keeps property suites fast while exercising the
    exact code that runs on the remote host.
    """
    stdout = io.StringIO()
    stdin = io.StringIO(json.dumps(payload))
    namespace: dict[str, Any] = {"__name__": "__remote_dev_script__"}
    original_stdin = sys.stdin
    sys.stdin = stdin
    try:
        with contextlib.redirect_stdout(stdout):
            try:
                exec(compile(code, "<remote-dev-script>", "exec"), namespace)
            except SystemExit as exc:
                if exc.code not in (0, None):
                    raise AssertionError(f"remote script exited with {exc.code}: {stdout.getvalue()}") from exc
    finally:
        sys.stdin = original_stdin
    lines = stdout.getvalue().strip().splitlines()
    if not lines:
        raise AssertionError("remote script produced no output")
    data = json.loads(lines[-1])
    if not isinstance(data, dict):
        raise AssertionError(f"remote script returned non-object: {data!r}")
    return data


def snapshot_tree(root: Path) -> dict[str, tuple[str, Any]]:
    """Return ``{relpath: (kind, payload)}`` for every entry below ``root``.

    Symlinks are recorded by their target, files by their bytes, directories by
    a marker, so two snapshots compare equal only when the trees are identical.
    """
    result: dict[str, tuple[str, Any]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        if path.is_symlink():
            result[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            result[rel] = ("dir", None)
        elif path.is_file():
            result[rel] = ("file", path.read_bytes())
        else:
            result[rel] = ("other", None)
    return result


class GeneratorSelfTests(unittest.TestCase):
    def test_case_seed_is_deterministic_and_distinct_per_index(self) -> None:
        seeds = [case_seed(i, seed=123) for i in range(50)]
        self.assertEqual(seeds, [case_seed(i, seed=123) for i in range(50)])
        self.assertEqual(len(set(seeds)), 50)
        self.assertNotEqual(case_seed(0, seed=1), case_seed(0, seed=2))

    def test_same_seed_replays_same_values(self) -> None:
        first = Gen(7)
        second = Gen(7)
        for _ in range(20):
            self.assertEqual(first.text(ASCII_WORD, 0, 10), second.text(ASCII_WORD, 0, 10))
            self.assertEqual(first.integer(0, 1000), second.integer(0, 1000))
            self.assertEqual(first.json_object(), second.json_object())

    def test_run_cases_reports_seed_in_failure(self) -> None:
        def body(gen: Gen, index: int) -> None:
            if index == 3:
                raise AssertionError("boom")

        with self.assertRaisesRegex(AssertionError, r"case #3/.*VAWS_PROPTEST_SEED=99.*case_seed=\d+.*boom"):
            run_cases(10, body, seed=99, label="self-test")

    def test_run_remote_script_round_trips_json(self) -> None:
        code = "import json, sys\npayload = json.loads(sys.stdin.read())\nprint(json.dumps({'echo': payload}))\nraise SystemExit(0)\n"
        self.assertEqual(run_remote_script(code, {"k": "v"}), {"echo": {"k": "v"}})


if __name__ == "__main__":
    unittest.main()
