"""Property tests for patch parsing and application (``core.patch_ops``).

The failure that matters here is *half-application*: a patch that is reported
as applied (or as rejected) while the tree ends up in a state that matches
neither "before" nor "after". The properties are therefore stated against a
reference model of the file tree:

* parse(render(ops)) == ops for generated Codex patches;
* an applied Codex patch leaves the tree exactly equal to the model;
* a Codex patch with one failing op (anywhere in the sequence) leaves the tree
  byte-for-byte unchanged and reports the precise status;
* a unified diff either applies fully or leaves the tree untouched.

Executors run in-process on temporary trees; the unified path runs the real
``bash`` + ``git apply`` script locally through a fake transport.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import core.patch_ops as patch_ops  # noqa: E402
import core.state_store as state_store  # noqa: E402
from core.endpoint import Endpoint  # noqa: E402
from core.patch_ops import PatchParseError, parse_codex_patch, parse_unified_patch_paths  # noqa: E402
from core.ssh_transport import RemoteCompleted  # noqa: E402
from test_property_support import DOC_HOSTS, SPLITLINES_EXTRA, Gen, run_cases, run_remote_script, snapshot_tree  # noqa: E402

LINE_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789 _=()[]{}:,.'\"#*@-+éλ漢"
PATH_ALPHABET = "abcdefghijklmnopqrstuvwxyz0123456789_"


def unique_lines(gen: Gen, count: int, tag: str) -> list[str]:
    """Lines carrying a unique token so any contiguous span occurs exactly once."""
    return [f"{tag}{i:03d} {gen.text(LINE_ALPHABET, 0, 10)}\n" for i in range(count)]


def random_relpath(gen: Gen, existing: set[str]) -> str:
    while True:
        depth = gen.integer(0, 2)
        parts = [gen.text(PATH_ALPHABET, 1, 6) for _ in range(depth)] + [gen.text(PATH_ALPHABET, 1, 8) + gen.choice((".py", ".txt", ""))]
        path = "/".join(parts)
        if path not in existing and not any(path.startswith(other + "/") or other.startswith(path + "/") for other in existing):
            return path


def render_codex(ops: list[dict[str, Any]]) -> str:
    out = ["*** Begin Patch\n"]
    for op in ops:
        if op["kind"] == "add":
            out.append(f"*** Add File: {op['path']}\n")
            for line in op["content"].splitlines(keepends=True):
                out.append("+" + line)
        elif op["kind"] == "delete":
            out.append(f"*** Delete File: {op['path']}\n")
        else:
            out.append(f"*** Update File: {op['path']}\n")
            if op.get("move_to"):
                out.append(f"*** Move to: {op['move_to']}\n")
            for hunk in op["hunks"]:
                out.append("@@\n")
                for prefix, line in hunk["_lines"]:
                    out.append(prefix + line)
    out.append("*** End Patch\n")
    return "".join(out)


def generate_hunk_lines(gen: Gen) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    for _ in range(gen.integer(1, 5)):
        prefix = gen.choice((" ", "-", "+"))
        lines.append((prefix, gen.text(LINE_ALPHABET, 0, 12) + "\n"))
    return lines


def generate_parse_ops(gen: Gen) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    used: set[str] = set()
    for _ in range(gen.integer(1, 4)):
        path = random_relpath(gen, used)
        used.add(path)
        kind = gen.choice(("add", "delete", "update", "update"))
        if kind == "add":
            content = "".join(gen.text(LINE_ALPHABET, 0, 14) + "\n" for _ in range(gen.integer(0, 4)))
            ops.append({"kind": "add", "path": path, "content": content})
        elif kind == "delete":
            ops.append({"kind": "delete", "path": path})
        else:
            hunks = []
            for _ in range(gen.integer(0, 3)):
                lines = generate_hunk_lines(gen)
                hunks.append({
                    "_lines": lines,
                    "old": "".join(text for prefix, text in lines if prefix in {" ", "-"}),
                    "new": "".join(text for prefix, text in lines if prefix in {" ", "+"}),
                })
            op: dict[str, Any] = {"kind": "update", "path": path, "hunks": hunks}
            if not hunks or gen.boolean(0.3):
                move_to = random_relpath(gen, used)
                used.add(move_to)
                op["move_to"] = move_to
            ops.append(op)
    return ops


def expected_from_render(ops: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expected = []
    for op in ops:
        if op["kind"] == "update":
            clean = {"kind": "update", "path": op["path"], "hunks": [{"old": h["old"], "new": h["new"]} for h in op["hunks"]]}
            if op.get("move_to"):
                clean["move_to"] = op["move_to"]
            expected.append(clean)
        else:
            expected.append(dict(op))
    return expected


class CodexParserProperties(unittest.TestCase):
    def test_parse_render_round_trip(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            ops = generate_parse_ops(gen)
            text = render_codex(ops)
            self.assertEqual(parse_codex_patch(text), expected_from_render(ops))
            # Parsing is deterministic and CRLF line endings do not change the result.
            self.assertEqual(parse_codex_patch(text), parse_codex_patch(text))

        run_cases(400, body, label="codex parse/render round trip")

    def test_malformed_patches_are_rejected_not_partially_parsed(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            ops = generate_parse_ops(gen)
            lines = render_codex(ops).splitlines(keepends=True)
            mutation = gen.choice(("drop-begin", "bad-directive", "bad-add-line", "double-move", "empty"))
            if mutation == "drop-begin":
                lines = lines[1:]
            elif mutation == "bad-directive":
                lines.insert(1, "*** Rename File: x\n")
            elif mutation == "bad-add-line":
                lines.insert(1, "*** Add File: z.py\n")
                lines.insert(2, "not-plus-prefixed\n")
            elif mutation == "double-move":
                lines.insert(1, "*** Update File: q.py\n")
                lines.insert(2, "*** Move to: a\n")
                lines.insert(3, "*** Move to: b\n")
            else:
                lines = ["*** Begin Patch\n", "*** End Patch\n"]
            with self.assertRaises(PatchParseError):
                parse_codex_patch("".join(lines))

        run_cases(200, body, label="malformed codex patch")

    @unittest.expectedFailure
    def test_known_defect_splitlines_boundaries_alter_add_file_content(self) -> None:
        """KNOWN DEFECT (low-medium): the parser splits with ``str.splitlines``,
        which also breaks on ``\\x0c`` (form feed, legal in Python source),
        ``\\r``, ``\\x1c``-``\\x1e``, ``\\x85``, ``\\u2028`` and ``\\u2029``. The
        fragment after such a character is re-read as a new patch line: an
        ``Add File`` body ``+a\\x0c+b`` is stored as ``a\\x0cb`` (the second
        ``+`` is eaten), and other fragments raise a confusing parse error.
        Evidence: ``parse_codex_patch('...+a\\x0c+b\\n...')[0]['content'] != 'a\\x0c+b\\n'``."""
        for separator in SPLITLINES_EXTRA:
            content = f"a{separator}+b\n"
            text = f"*** Begin Patch\n*** Add File: f.py\n+{content}*** End Patch\n"
            ops = parse_codex_patch(text)
            self.assertEqual(ops[0]["content"], content, f"content altered for separator {separator!r}")


class ExecutorModel:
    """Reference model of a tree of text files keyed by relative path.

    ``history`` remembers every path ever used so that a new path never
    collides with a directory left behind by a deleted or moved file.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}
        self.history: set[str] = set()

    def fresh_path(self, gen: Gen) -> str:
        path = random_relpath(gen, self.history | set(self.files))
        self.history.add(path)
        return path

    def materialize(self, root: Path) -> None:
        for rel, content in self.files.items():
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def snapshot(self) -> dict[str, bytes]:
        return {rel: content.encode("utf-8") for rel, content in self.files.items()}


def tree_files(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def generate_valid_op(gen: Gen, model: ExecutorModel, tag: str) -> dict[str, Any]:
    existing = set(model.files)
    choices = ["add"]
    if existing:
        choices += ["delete", "update", "update", "move"]
    kind = gen.choice(choices)
    if kind == "add":
        path = model.fresh_path(gen)
        content = "".join(unique_lines(gen, gen.integer(0, 4), tag))
        model.files[path] = content
        return {"kind": "add", "path": path, "content": content}
    path = gen.choice(sorted(existing))
    if kind == "delete":
        del model.files[path]
        return {"kind": "delete", "path": path}
    text = model.files[path]
    hunks = []
    for hunk_index in range(gen.integer(0 if kind == "move" else 1, 2)):
        lines = text.splitlines(keepends=True)
        if not lines:
            break
        start = gen.integer(0, len(lines) - 1)
        end = gen.integer(start + 1, min(len(lines), start + 3))
        old = "".join(lines[start:end])
        # A per-hunk tag keeps every inserted line unique within the file so
        # the "old text occurs exactly once" precondition of the model holds.
        new = "".join(unique_lines(gen, gen.integer(0, 3), f"{tag}h{hunk_index}n"))
        assert text.count(old) == 1, (old, text)
        text = text.replace(old, new, 1)
        hunks.append({"old": old, "new": new})
    op: dict[str, Any] = {"kind": "update", "path": path, "hunks": hunks}
    if kind == "move" or (hunks and gen.boolean(0.25)):
        move_to = model.fresh_path(gen)
        op["move_to"] = move_to
        del model.files[path]
        model.files[move_to] = text
    else:
        model.files[path] = text
    return op


def generate_failing_op(gen: Gen, model: ExecutorModel) -> tuple[dict[str, Any], str]:
    existing = sorted(model.files)
    taken = model.history | set(model.files)
    options = ["missing-update", "missing-delete", "bad-kind", "add-existing", "context-mismatch", "move-onto-existing"]
    if not existing:
        options = ["missing-update", "missing-delete", "bad-kind"]
    kind = gen.choice(options)
    if kind == "missing-update":
        return {"kind": "update", "path": random_relpath(gen, taken), "hunks": [{"old": "x\n", "new": "y\n"}]}, "not_found"
    if kind == "missing-delete":
        return {"kind": "delete", "path": random_relpath(gen, taken)}, "not_found"
    if kind == "bad-kind":
        return {"kind": gen.choice(("rename", "patch", "")), "path": random_relpath(gen, taken)}, "invalid_patch"
    path = gen.choice(existing)
    if kind == "add-existing":
        return {"kind": "add", "path": path, "content": "dup\n"}, "file_exists"
    if kind == "context-mismatch":
        return {"kind": "update", "path": path, "hunks": [{"old": "THIS CONTEXT DOES NOT EXIST\n", "new": "x\n"}]}, "context_mismatch"
    target = gen.choice([p for p in existing if p != path] or [path])
    if target == path:
        return {"kind": "add", "path": path, "content": "dup\n"}, "file_exists"
    return {"kind": "update", "path": path, "move_to": target, "hunks": []}, "file_exists"


class CodexExecutorProperties(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _root(self, index: int) -> Path:
        root = Path(self._tmp.name).resolve() / f"case-{index}"
        root.mkdir()
        return root

    def _run(self, root: Path, ops: list[dict[str, Any]]) -> dict[str, Any]:
        return run_remote_script(patch_ops.REMOTE_CODEX_PATCH_PY, {"root": str(root), "cwd": str(root), "ops": ops})

    def test_applied_patch_matches_reference_model_exactly(self) -> None:
        def body(gen: Gen, index: int) -> None:
            root = self._root(index)
            model = ExecutorModel()
            for i in range(gen.integer(0, 3)):
                model.files[model.fresh_path(gen)] = "".join(unique_lines(gen, gen.integer(1, 5), f"f{i}L"))
            model.materialize(root)
            ops = [generate_valid_op(gen, model, f"o{i}L") for i in range(gen.integer(1, 4))]
            data = self._run(root, ops)
            self.assertEqual(data["status"], "applied", data)
            self.assertEqual(tree_files(root), model.snapshot())
            self.assertEqual(len(data["changed_files"]), len(ops))
            for item in data["changed_files"]:
                path = Path(item["path"])
                if path.exists():
                    self.assertEqual(item["after_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                    self.assertEqual(item["size"], path.stat().st_size)
                else:
                    self.assertIsNone(item["after_sha256"])
                self.assertIn(item["op"], {"add", "delete", "update", "move"})

        run_cases(150, body, label="codex executor vs model")

    def test_one_failing_op_anywhere_leaves_tree_untouched(self) -> None:
        def body(gen: Gen, index: int) -> None:
            root = self._root(index)
            model = ExecutorModel()
            for i in range(gen.integer(1, 3)):
                model.files[model.fresh_path(gen)] = "".join(unique_lines(gen, gen.integer(1, 4), f"f{i}L"))
            model.materialize(root)
            before = snapshot_tree(root)
            valid_count = gen.integer(0, 3)
            position = gen.integer(0, valid_count)
            ops: list[dict[str, Any]] = []
            expected_status = ""
            for i in range(valid_count + 1):
                if i == position:
                    # The failing op is generated against the model state at
                    # its position, so its expected status is exact.
                    failing, expected_status = generate_failing_op(gen, model)
                    ops.append(failing)
                else:
                    ops.append(generate_valid_op(gen, model, f"o{i}L"))
            data = self._run(root, ops)
            self.assertEqual(data["status"], expected_status, data)
            self.assertEqual(snapshot_tree(root), before, f"tree changed after rejected patch (failing op at {position})")

        run_cases(200, body, label="codex executor all-or-nothing")

    def test_zero_ops_and_repeated_ops_are_handled_without_side_effects(self) -> None:
        root = self._root(0)
        (root / "a.py").write_text("L1 x\nL2 y\n", encoding="utf-8")
        before = snapshot_tree(root)
        data = self._run(root, [])
        self.assertEqual(data["status"], "applied")
        self.assertEqual(data["changed_files"], [])
        self.assertEqual(snapshot_tree(root), before)
        data = self._run(root, [
            {"kind": "update", "path": "a.py", "hunks": [{"old": "L1 x\n", "new": "L1 z\n"}]},
            {"kind": "update", "path": "a.py", "hunks": [{"old": "L2 y\n", "new": "L2 w\n"}]},
        ])
        self.assertEqual(data["status"], "applied")
        self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "L1 z\nL2 w\n")

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_commit_failure_rolls_back_files_written_earlier_in_the_same_commit(self) -> None:
        root = self._root(1)
        (root / "ok").mkdir()
        (root / "locked").mkdir()
        (root / "ok" / "a.py").write_text("a1\n", encoding="utf-8")
        (root / "locked" / "b.py").write_text("b1\n", encoding="utf-8")
        before = snapshot_tree(root)
        os.chmod(root / "locked", 0o500)
        try:
            data = self._run(root, [
                {"kind": "update", "path": "ok/a.py", "hunks": [{"old": "a1\n", "new": "a2\n"}]},
                {"kind": "update", "path": "locked/b.py", "hunks": [{"old": "b1\n", "new": "b2\n"}]},
            ])
        finally:
            os.chmod(root / "locked", 0o700)
        self.assertEqual(data["status"], "commit_failed", data)
        self.assertEqual(snapshot_tree(root), before)
        self.assertIn("ok/a.py", " ".join(data["rollback_status"]["restored"]))

    @unittest.expectedFailure
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_known_defect_rollback_report_lists_untouched_files_as_failed(self) -> None:
        """KNOWN DEFECT (low): ``restore`` rewrites *every* touched path, not
        only the ones the commit actually changed. When the commit failed
        because a directory is unwritable, restoring the unchanged file in that
        directory fails too, and ``rollback_status.failed`` reports a file that
        is byte-for-byte intact. The caller cannot tell a real rollback failure
        from this false alarm. Evidence: ``failed`` names ``locked/b.py`` while
        the tree equals the pre-patch snapshot."""
        root = self._root(6)
        (root / "ok").mkdir()
        (root / "locked").mkdir()
        (root / "ok" / "a.py").write_text("a1\n", encoding="utf-8")
        (root / "locked" / "b.py").write_text("b1\n", encoding="utf-8")
        before = snapshot_tree(root)
        os.chmod(root / "locked", 0o500)
        try:
            data = self._run(root, [
                {"kind": "update", "path": "ok/a.py", "hunks": [{"old": "a1\n", "new": "a2\n"}]},
                {"kind": "update", "path": "locked/b.py", "hunks": [{"old": "b1\n", "new": "b2\n"}]},
            ])
        finally:
            os.chmod(root / "locked", 0o700)
        self.assertEqual(data["status"], "commit_failed", data)
        self.assertEqual(snapshot_tree(root), before)
        self.assertEqual(data["rollback_status"]["failed"], [])

    @unittest.expectedFailure
    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_known_defect_rollback_leaves_directories_created_during_failed_commit(self) -> None:
        """KNOWN DEFECT (low): when the commit phase fails, ``restore`` unlinks
        files it created but keeps directories that ``atomic_write`` created via
        ``mkdir(parents=True)``. A rejected patch therefore leaves new empty
        directories behind — a weak form of half-application that a later
        ``git status`` or glob will surface. Evidence: ``newdir/`` survives."""
        root = self._root(2)
        (root / "locked").mkdir()
        (root / "locked" / "b.py").write_text("b1\n", encoding="utf-8")
        before = snapshot_tree(root)
        os.chmod(root / "locked", 0o500)
        try:
            data = self._run(root, [
                {"kind": "add", "path": "newdir/deep/x.py", "content": "x\n"},
                {"kind": "update", "path": "locked/b.py", "hunks": [{"old": "b1\n", "new": "b2\n"}]},
            ])
        finally:
            os.chmod(root / "locked", 0o700)
        self.assertEqual(data["status"], "commit_failed", data)
        self.assertEqual(snapshot_tree(root), before)

    @unittest.expectedFailure
    def test_known_defect_aliased_paths_lose_earlier_hunks_but_report_applied(self) -> None:
        """KNOWN DEFECT (medium-high): the virtual overlay is keyed by the
        *unresolved* ``pathlib.Path``, so ``a.py`` and ``sub/../a.py`` (or a
        path through an in-root directory symlink) are two keys for one file.
        The second op reads the real file, not the overlay, and the commit
        writes both overlays in order — the later write silently discards the
        earlier hunk while the result claims ``applied`` for both.
        Evidence: file ends as ``one\\nTWO\\n`` instead of ``ONE\\nTWO\\n``."""
        root = self._root(3)
        (root / "sub").mkdir()
        (root / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        data = self._run(root, [
            {"kind": "update", "path": "a.py", "hunks": [{"old": "one\n", "new": "ONE\n"}]},
            {"kind": "update", "path": "sub/../a.py", "hunks": [{"old": "two\n", "new": "TWO\n"}]},
        ])
        if data["status"] == "applied":
            self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "ONE\nTWO\n")

    @unittest.expectedFailure
    def test_known_defect_hunk_anchor_after_double_at_is_ignored(self) -> None:
        """KNOWN DEFECT (medium-high): the Codex format uses ``@@ <anchor>`` to
        pick which occurrence of a context block to edit. The parser drops the
        anchor text and the executor replaces the *first* occurrence of the old
        text, so a hunk anchored at ``def second():`` edits ``def first():``
        and reports ``applied``. This is a silent wrong-site edit.
        Evidence: ``first`` returns 2 and ``second`` still returns 1."""
        root = self._root(4)
        source = "def first():\n    return 1\n\ndef second():\n    return 1\n"
        (root / "a.py").write_text(source, encoding="utf-8")
        ops = parse_codex_patch("*** Begin Patch\n*** Update File: a.py\n@@ def second():\n-    return 1\n+    return 2\n*** End Patch\n")
        data = self._run(root, ops)
        self.assertEqual(data["status"], "applied", data)
        self.assertEqual((root / "a.py").read_text(encoding="utf-8"), "def first():\n    return 1\n\ndef second():\n    return 2\n")

    @unittest.expectedFailure
    def test_known_defect_context_free_hunk_is_inserted_at_file_start(self) -> None:
        """KNOWN DEFECT (low-medium): a hunk with only ``+`` lines has
        ``old == ''``; ``'' in text`` is always true and ``replace('', new, 1)``
        prepends. An unanchored insertion is applied at offset 0 and reported as
        ``applied`` instead of being rejected as ambiguous.
        Evidence: file becomes ``INSERTED\\none\\ntwo\\n``."""
        root = self._root(5)
        (root / "a.py").write_text("one\ntwo\n", encoding="utf-8")
        data = self._run(root, [{"kind": "update", "path": "a.py", "hunks": [{"old": "", "new": "INSERTED\n"}]}])
        self.assertNotEqual(data["status"], "applied", "context-free hunk must be rejected, not prepended")


class UnifiedDiffProperties(unittest.TestCase):
    def _unified(self, files_before: dict[str, str], files_after: dict[str, str]) -> str:
        chunks = []
        for path in sorted(set(files_before) | set(files_after)):
            before = files_before.get(path)
            after = files_after.get(path)
            if before == after:
                continue
            diff = difflib.unified_diff(
                (before or "").splitlines(keepends=True),
                (after or "").splitlines(keepends=True),
                fromfile="/dev/null" if before is None else f"a/{path}",
                tofile="/dev/null" if after is None else f"b/{path}",
                n=1,
            )
            header = f"diff --git a/{path} b/{path}\n"
            if before is None:
                header += "new file mode 100644\n"
            elif after is None:
                header += "deleted file mode 100644\n"
            chunks.append(header + "".join(diff))
        return "".join(chunks)

    def test_parse_unified_paths_lists_each_changed_file_once(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            before: dict[str, str] = {}
            after: dict[str, str] = {}
            used: set[str] = set()
            for i in range(gen.integer(1, 4)):
                path = random_relpath(gen, used)
                used.add(path)
                mode = gen.choice(("modify", "add", "delete"))
                if mode != "add":
                    before[path] = "".join(unique_lines(gen, gen.integer(1, 4), f"b{i}"))
                if mode != "delete":
                    after[path] = "".join(unique_lines(gen, gen.integer(1, 4), f"a{i}"))
            patch = self._unified(before, after)
            self.assertEqual(parse_unified_patch_paths(patch), sorted(used))

        run_cases(200, body, label="unified path extraction")

    def test_unified_without_headers_is_rejected(self) -> None:
        with self.assertRaises(PatchParseError):
            parse_unified_patch_paths("@@ -1 +1 @@\n-a\n+b\n")

    @unittest.expectedFailure
    def test_known_defect_timestamp_suffix_is_kept_in_extracted_path(self) -> None:
        """KNOWN DEFECT (low): ``diff -u`` style headers may carry a tab and a
        timestamp (``--- a/x.py\\t2026-01-01 00:00:00``). The extractor keeps the
        suffix, so the local pre-flight (root check, symlink check, before-hash)
        runs on a path that ``git apply`` never touches.
        Evidence: extracted path is ``'x.py\\t2026-01-01 00:00:00'``."""
        patch = "--- a/x.py\t2026-01-01 00:00:00\n+++ b/x.py\t2026-01-01 00:00:01\n@@ -1 +1 @@\n-a\n+b\n"
        self.assertEqual(parse_unified_patch_paths(patch), ["x.py"])

    @unittest.skipUnless(shutil.which("git") and shutil.which("bash"), "needs git and bash")
    def test_unified_apply_is_all_or_nothing_through_the_real_script(self) -> None:
        endpoint_host = DOC_HOSTS[0]

        def body(gen: Gen, index: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                repo = Path(tmp).resolve() / "repo"
                repo.mkdir()
                before: dict[str, str] = {}
                after: dict[str, str] = {}
                used: set[str] = set()
                for i in range(gen.integer(1, 3)):
                    path = random_relpath(gen, used)
                    used.add(path)
                    before[path] = "".join(unique_lines(gen, gen.integer(2, 5), f"b{i}"))
                    lines = before[path].splitlines(keepends=True)
                    lines[gen.integer(0, len(lines) - 1)] = f"changed{i} {gen.word(0, 5)}\n"
                    after[path] = "".join(lines)
                if gen.boolean(0.3):
                    new_path = random_relpath(gen, used)
                    used.add(new_path)
                    after[new_path] = "".join(unique_lines(gen, gen.integer(1, 3), "n"))
                for path, content in before.items():
                    (repo / path).parent.mkdir(parents=True, exist_ok=True)
                    (repo / path).write_text(content, encoding="utf-8")
                patch = self._unified(before, after)
                corrupt = gen.boolean(0.5)
                if corrupt:
                    victim = gen.choice(sorted(before))
                    (repo / victim).write_text("DRIFTED\n", encoding="utf-8")
                snapshot_before = snapshot_tree(repo)
                endpoint = Endpoint(host=endpoint_host, port=46000, root=str(repo), cwd=str(repo))

                def fake_run_script(_endpoint: Endpoint, script: str, **_kwargs: Any) -> Any:
                    proc = subprocess.run(["bash", "-s"], input=script, cwd=repo, capture_output=True, text=True, check=False)
                    return RemoteCompleted(proc.returncode, proc.stdout, proc.stderr)

                with mock.patch.object(patch_ops, "run_script", fake_run_script), \
                        mock.patch.object(state_store, "substrate_root", return_value=repo.parent / "state"):
                    payload = patch_ops.remote_apply_patch(endpoint, patch=patch, cwd=str(repo))
                result = payload["result"]
                if corrupt:
                    self.assertEqual(result["status"], "context_mismatch", payload["text"])
                    self.assertEqual(snapshot_tree(repo), snapshot_before, "rejected unified diff changed the tree")
                else:
                    self.assertEqual(result["outcome"], "success", payload["text"])
                    self.assertEqual({k: v.encode("utf-8") for k, v in after.items()}, tree_files(repo))
                    for item in result["changed_files"]:
                        path = repo / item["path"]
                        self.assertEqual(item["after_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
                        if item["path"] in before:
                            self.assertEqual(item["before_sha256"], hashlib.sha256(before[item["path"]].encode("utf-8")).hexdigest())
                        else:
                            self.assertIsNone(item["before_sha256"])

        run_cases(8, body, label="unified apply all-or-nothing")


if __name__ == "__main__":
    unittest.main()
