"""Property and adversarial tests for the remote path policy.

Two layers decide what a remote write may touch:

* the string layer (``core.path_policy``) that runs locally before anything
  is sent, and
* the resolving layer inside the ``REMOTE_*_PY`` executor scripts that runs on
  the remote host and sees symlinks.

The properties below are stated against an independent lexical reference
implementation rather than against hand-picked examples, and the remote layer
is exercised in-process on a temporary tree that contains symlinks pointing
outside the configured root.
"""

from __future__ import annotations

import posixpath
import sys
import tempfile
import unicodedata
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.errors import PathPolicyError  # noqa: E402
from core.path_policy import assert_under_root, join_under_root, normalize_remote_path, path_fingerprint  # noqa: E402
import core.file_ops as file_ops  # noqa: E402
import core.patch_ops as patch_ops  # noqa: E402
from test_property_support import MULTIBYTE, Gen, run_cases, run_remote_script, snapshot_tree  # noqa: E402

SEGMENTS = (
    "..",
    ".",
    "",
    "vllm-workspace",
    "vllm-workspace2",
    "vllm",
    "src",
    "a b",
    "..hidden",
    "...",
    "a\\..\\b",
    "%2e%2e",
    "x\x00y",
    "é",
    unicodedata.normalize("NFD", "é"),
    "漢字",
    "🙂",
    "-rf",
    "~",
    "$HOME",
)
ROOTS = ("/", "/vllm-workspace", "/vllm-workspace/", "/vllm-workspace/../vllm-workspace", "/a/b/c", "//vllm-workspace")


def reference_normalize(path: str) -> tuple[str, tuple[str, ...]] | None:
    """Lexical reference: returns (leading-marker, components) or None if relative.

    POSIX leaves a leading ``//`` implementation-defined, and ``posixpath``
    preserves it, so the marker distinguishes ``/`` from ``//``.
    """
    if not path.startswith("/"):
        return None
    marker = "//" if path.startswith("//") and not path.startswith("///") else "/"
    components: list[str] = []
    for part in path.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if components:
                components.pop()
            continue
        components.append(part)
    return marker, tuple(components)


def reference_contains(root: str, path: str) -> bool:
    ref_root = reference_normalize(root)
    ref_path = reference_normalize(path)
    if ref_root is None or ref_path is None:
        return False
    root_marker, root_parts = ref_root
    path_marker, path_parts = ref_path
    if root_marker == "/" and not root_parts:
        # Root "/" contains every absolute path, including the "//" form.
        return True
    if root_marker != path_marker:
        return False
    return path_parts[: len(root_parts)] == root_parts


def reference_render(normalized: tuple[str, tuple[str, ...]]) -> str:
    marker, parts = normalized
    if not parts:
        return marker
    return marker + "/".join(parts)


def random_path(gen: Gen, *, absolute: bool | None = None) -> str:
    count = gen.integer(0, 5)
    parts = [gen.choice(SEGMENTS) for _ in range(count)]
    body = "/".join(parts)
    if absolute is None:
        absolute = gen.boolean(0.6)
    if absolute:
        prefix = gen.choice(("/", "/", "/", "//", "///"))
        return prefix + body
    return body


class PathPolicyStringLayerProperties(unittest.TestCase):
    def test_decision_matches_lexical_reference(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            root = gen.choice(ROOTS)
            path = random_path(gen, absolute=True)
            expected = reference_contains(root, path)
            try:
                result = assert_under_root(path, root)
            except PathPolicyError:
                self.assertFalse(expected, f"reference accepts {path!r} under {root!r} but policy rejected")
                return
            self.assertTrue(expected, f"policy accepted {path!r} under {root!r} but reference rejects")
            self.assertEqual(result, reference_render(reference_normalize(path)))

        run_cases(1500, body, label="assert_under_root vs reference")

    def test_accepted_paths_are_normalized_absolute_and_under_root(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            root = gen.choice(ROOTS)
            cwd_rel = random_path(gen, absolute=False)
            cwd = posixpath.normpath(posixpath.join(root, cwd_rel)) if gen.boolean(0.8) else random_path(gen, absolute=True)
            rel_or_abs = random_path(gen)
            try:
                result = join_under_root(root, cwd, rel_or_abs)
            except PathPolicyError:
                return
            normalized_root = posixpath.normpath(root)
            self.assertTrue(result.startswith("/"), result)
            self.assertEqual(result, posixpath.normpath(result), "accepted path must be normalized")
            self.assertNotIn("..", result.split("/"), "accepted path must not contain '..' components")
            self.assertNotIn(".", result.split("/")[1:], "accepted path must not contain '.' components")
            self.assertTrue(
                result == normalized_root or result.startswith(normalized_root.rstrip("/") + "/"),
                f"{result!r} escapes {normalized_root!r}",
            )
            # Idempotence and determinism: re-checking an accepted path is a no-op.
            self.assertEqual(assert_under_root(result, root), result)
            self.assertEqual(join_under_root(root, cwd, result), result)
            self.assertEqual(join_under_root(root, cwd, rel_or_abs), result)
            if not rel_or_abs.startswith("/"):
                self.assertEqual(result, posixpath.normpath(posixpath.join(cwd, rel_or_abs)))

        run_cases(1500, body, label="join_under_root invariants")

    def test_relative_traversal_depth_is_exactly_the_cwd_depth(self) -> None:
        root = "/vllm-workspace"

        def body(gen: Gen, _index: int) -> None:
            depth = gen.integer(0, 4)
            cwd = root + "".join(f"/d{i}" for i in range(depth))
            ups = gen.integer(0, 7)
            tail = gen.choice(("", "/x.py", "/sub/y.py"))
            rel = "/".join([".."] * ups) + tail if ups else ("x.py" if not tail else tail.lstrip("/"))
            if ups <= depth:
                result = join_under_root(root, cwd, rel)
                self.assertTrue(result == root or result.startswith(root + "/"))
            else:
                with self.assertRaises(PathPolicyError):
                    join_under_root(root, cwd, rel)

        run_cases(300, body, label="traversal depth boundary")

    def test_sibling_prefix_roots_are_never_confused(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            base = "/" + gen.word(1, 10)
            suffix = gen.text("abc-_.0", 1, 3)
            sibling = base + suffix
            path = sibling + "/" + gen.word(1, 6)
            with self.assertRaises(PathPolicyError, msg=f"{path!r} accepted under {base!r}"):
                assert_under_root(path, base)
            self.assertEqual(assert_under_root(base + "/" + gen.word(1, 6), base)[: len(base) + 1], base + "/")

        run_cases(300, body, label="sibling prefix")

    def test_degenerate_roots_are_rejected_not_widened(self) -> None:
        for root in ("", ".", "..", "relative/root", "vllm-workspace", "~", "./x"):
            with self.subTest(root=root):
                with self.assertRaises(PathPolicyError):
                    assert_under_root("/vllm-workspace/x", root)
                with self.assertRaises(PathPolicyError):
                    join_under_root(root, "/vllm-workspace", "x")

    def test_unicode_normalization_forms_do_not_alias_the_root(self) -> None:
        composed = "/work-" + "é"
        decomposed = "/work-" + unicodedata.normalize("NFD", "é")
        self.assertNotEqual(composed, decomposed)
        with self.assertRaises(PathPolicyError):
            assert_under_root(decomposed + "/x", composed)
        with self.assertRaises(PathPolicyError):
            assert_under_root(composed + "/x", decomposed)
        self.assertEqual(assert_under_root(composed + "/x", composed), composed + "/x")

    def test_adversarial_corpus_is_rejected_under_workspace_root(self) -> None:
        root = "/vllm-workspace"
        corpus = [
            "/vllm-workspace/../etc/passwd",
            "/vllm-workspace/./../etc/passwd",
            "/vllm-workspace/a/../../etc/passwd",
            "/vllm-workspace2/x",
            "/vllm-workspace-old/x",
            "/VLLM-WORKSPACE/x",
            "//vllm-workspace/x",
            "/etc/vllm-workspace/x",
            "/vllm-workspace/x\x00/../../etc/passwd",
            "/vllm-workspace/" + "../" * 20 + "etc/passwd",
            "/",
            "/vllm-workspace/..",
            "/vllm-workspace/../",
        ]
        for path in corpus:
            with self.subTest(path=path):
                with self.assertRaises(PathPolicyError):
                    assert_under_root(path, root)

    def test_literal_lookalikes_stay_literal_and_under_root(self) -> None:
        # No URL/percent/backslash decoding happens: these are ordinary file
        # names, so they are accepted *and* remain under root as literals.
        root = "/vllm-workspace"
        for name in ("..%2f..%2fetc", "..\\..\\etc", "...", "..hidden", "-rf", "$HOME", "~"):
            with self.subTest(name=name):
                result = join_under_root(root, root, name)
                self.assertEqual(result, root + "/" + name)

    def test_non_string_inputs_raise_policy_error(self) -> None:
        for value in (None, 12, b"/x", ["/x"]):
            with self.subTest(value=value):
                with self.assertRaises(PathPolicyError):
                    normalize_remote_path(value)  # type: ignore[arg-type]
                with self.assertRaises(PathPolicyError):
                    assert_under_root(value, "/vllm-workspace")  # type: ignore[arg-type]

    @unittest.expectedFailure
    def test_known_defect_join_under_root_leaks_attribute_error_for_non_string(self) -> None:
        """KNOWN DEFECT (low): ``join_under_root`` calls ``rel_or_abs.startswith``
        before ``normalize_remote_path`` validates the type, so a ``None`` or
        integer path raises ``AttributeError`` instead of ``PathPolicyError``.
        MCP arguments are ``str()``-coerced today, so only direct callers hit it.
        Evidence: ``join_under_root('/r', '/r', None)`` -> AttributeError."""
        with self.assertRaises(PathPolicyError):
            join_under_root("/r", "/r", None)  # type: ignore[arg-type]

    def test_fingerprint_is_stable_under_normalization_and_collision_free_on_corpus(self) -> None:
        seen: dict[str, str] = {}

        def body(gen: Gen, _index: int) -> None:
            path = random_path(gen, absolute=True)
            try:
                normalized = normalize_remote_path(path)
            except PathPolicyError:
                return
            fingerprint = path_fingerprint(path)
            self.assertEqual(fingerprint, path_fingerprint(normalized))
            self.assertRegex(fingerprint, r"^[0-9a-f]{24}$")
            previous = seen.setdefault(fingerprint, normalized)
            self.assertEqual(previous, normalized, "fingerprint collision between distinct normalized paths")

        run_cases(1000, body, label="path_fingerprint")


class RemoteExecutorEscapeProperties(unittest.TestCase):
    """The remote-side resolver must never touch or reveal anything outside root."""

    def _layout(self, gen: Gen) -> tuple[Path, Path, Path]:
        base = Path(self._tmp.name).resolve()
        index = gen.integer(0, 10**9)
        root = base / f"root-{index}"
        outside = base / f"outside-{index}"
        (root / "pkg").mkdir(parents=True)
        (outside / "dir").mkdir(parents=True)
        (root / "pkg" / "inside.py").write_text("inside = 1\n", encoding="utf-8")
        (root / "legit.py").write_text("legit = 1\n", encoding="utf-8")
        (outside / "secret.txt").write_text("outside-secret\n", encoding="utf-8")
        (outside / "dir" / "victim.py").write_text("victim = 1\n", encoding="utf-8")
        (root / "file_link").symlink_to(outside / "secret.txt")
        (root / "dir_link").symlink_to(outside / "dir")
        (root / "dangling").symlink_to(outside / "missing.txt")
        (root / "inside_link").symlink_to(root / "legit.py")
        (root / "pkg" / "up_link").symlink_to(root / "pkg" / ".." / "legit.py")
        return root, outside, base

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _escape_targets(self, gen: Gen, root: Path, outside: Path) -> list[str]:
        name = gen.word(1, 6)
        return [
            "file_link",
            "dangling",
            f"dir_link/{name}.py",
            "dir_link/victim.py",
            f"dir_link/nested/{name}.py",
            f"../{outside.name}/{name}.py",
            f"../{outside.name}/secret.txt",
            str(outside / f"{name}.py"),
            str(outside / "secret.txt"),
            str(root / "pkg" / ".." / ".." / outside.name / "secret.txt"),
        ]

    def test_write_edit_and_patch_never_touch_outside_root(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            root, outside, _base = self._layout(gen)
            before_outside = snapshot_tree(outside)
            target = gen.choice(self._escape_targets(gen, root, outside))
            content = gen.text(MULTIBYTE + "abc\n", 0, 12)
            op_kind = gen.choice(("write", "edit", "multi_edit", "patch-add", "patch-update", "patch-delete", "patch-move"))
            if op_kind in {"write", "edit", "multi_edit"}:
                payload = {
                    "op": op_kind,
                    "root": str(root),
                    "cwd": str(root),
                    "file_path": target,
                    "content": content,
                    "overwrite": True,
                    "create_dirs": gen.boolean(),
                    "old_string": "victim",
                    "new_string": content or "x",
                    "edits": [{"old_string": "victim", "new_string": content or "x"}],
                }
                data = run_remote_script(file_ops.REMOTE_FILE_PY, payload)
                self.assertNotIn(data["status"], {"written", "edited"}, f"{op_kind} {target!r} succeeded: {data}")
            else:
                if op_kind == "patch-add":
                    ops = [{"kind": "add", "path": target, "content": content}]
                elif op_kind == "patch-update":
                    ops = [{"kind": "update", "path": target, "hunks": [{"old": "victim", "new": content or "x"}]}]
                elif op_kind == "patch-delete":
                    ops = [{"kind": "delete", "path": target}]
                else:
                    ops = [{"kind": "update", "path": "legit.py", "move_to": target, "hunks": []}]
                data = run_remote_script(patch_ops.REMOTE_CODEX_PATCH_PY, {"root": str(root), "cwd": str(root), "ops": ops})
                self.assertNotEqual(data["status"], "applied", f"{op_kind} {target!r} applied: {data}")
                self.assertTrue((root / "legit.py").exists(), "failed move must leave the source in place")
            self.assertEqual(snapshot_tree(outside), before_outside, f"{op_kind} {target!r} changed a path outside root")

        run_cases(120, body, label="remote executor escape")

    def test_read_never_reveals_content_outside_root(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            root, outside, _base = self._layout(gen)
            target = gen.choice(["file_link", "dir_link/victim.py", str(outside / "secret.txt"), f"../{outside.name}/secret.txt"])
            payload = {
                "op": "read",
                "root": str(root),
                "cwd": str(root),
                "file_path": target,
                "allow_symlink": gen.boolean(),
                "offset": 1,
                "limit": 50,
            }
            data = run_remote_script(file_ops.REMOTE_FILE_PY, payload)
            self.assertEqual(data["status"], "path_outside_root", data)
            self.assertNotIn("outside-secret", str(data))
            self.assertNotIn("victim = 1", str(data))

        run_cases(40, body, label="remote read escape")

    def test_symlinks_inside_root_are_refused_for_writes_but_targets_are_writable(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            root, _outside, _base = self._layout(gen)
            link = gen.choice(("inside_link", "pkg/up_link"))
            data = run_remote_script(
                file_ops.REMOTE_FILE_PY,
                {"op": "write", "root": str(root), "cwd": str(root), "file_path": link, "content": "x\n", "overwrite": True},
            )
            self.assertEqual(data["status"], "symlink_not_allowed", data)
            self.assertEqual((root / "legit.py").read_text(encoding="utf-8"), "legit = 1\n")
            direct = run_remote_script(
                file_ops.REMOTE_FILE_PY,
                {"op": "write", "root": str(root), "cwd": str(root), "file_path": "legit.py", "content": "legit = 2\n", "overwrite": True},
            )
            self.assertEqual(direct["status"], "written", direct)
            self.assertEqual((root / "legit.py").read_text(encoding="utf-8"), "legit = 2\n")

        run_cases(20, body, label="inside symlink writes")


if __name__ == "__main__":
    unittest.main()
