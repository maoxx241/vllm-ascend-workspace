#!/usr/bin/env python3
"""Property tests for the managed artifact path in ``vaws_remote_toolbox``.

``.agents/scripts/remote_artifact_{manifest,pull,push}.py`` are thin wrappers
over ``vaws_remote_toolbox``; the properties therefore target the library:

* the local manifest is deterministic, agrees with an independent reference,
  and changes for every single-byte flip, truncation, extension, added,
  removed or renamed file;
* ``artifact_pull`` never lands a file whose bytes disagree with the manifest,
  and refuses manifest relpaths that would escape the local directory (the
  latter is recorded as a known defect: the ``.remote-dev`` port guards it,
  this path does not).
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import vaws_remote_toolbox as toolbox  # noqa: E402
from vaws_remote_toolbox import RemoteTarget, SshEndpoint  # noqa: E402
from test_property_support import DOC_HOSTS, MULTIBYTE, Gen, run_cases  # noqa: E402

NAME_ALPHABET = "abcxyz019_-" + MULTIBYTE


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry_name(gen: Gen, max_len: int = 8) -> str:
    return gen.text(NAME_ALPHABET, 1, max_len)


def build_tree(gen: Gen, root: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    dirs = [""] + ["/".join(entry_name(gen, 6) for _ in range(gen.integer(1, 2))) for _ in range(gen.integer(0, 2))]
    for _ in range(gen.integer(1, 6)):
        directory = gen.choice(dirs)
        rel = f"{directory}/{entry_name(gen)}" if directory else entry_name(gen)
        if rel in files or any(other.startswith(rel + "/") or rel.startswith(other + "/") for other in files):
            continue
        data = gen.one_of(lambda: b"", lambda: gen.raw_bytes(1, 64), lambda: gen.raw_bytes(1000, 2500))
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(data)
        files[rel] = data
    return files


def view(manifest: dict[str, Any]) -> dict[str, tuple[str, int]]:
    return {item["relpath"]: (item["sha256"], item["size"]) for item in manifest["files"]}


def make_target(base: Path) -> RemoteTarget:
    return RemoteTarget(
        mode="session",
        alias="machine-a",
        target_id="sess-abc",
        workspace_id="sess-abc",
        workspace_root=base,
        runtime_root="/vllm-workspace",
        container_name="vaws-test",
        container_image="image",
        container_endpoint=SshEndpoint(DOC_HOSTS[0], 46000),
        host_endpoint=SshEndpoint(DOC_HOSTS[0], 22),
        state_repo_root=base,
        record={},
        session_id="sess-abc",
        session_file=base / "session.json",
        session={"session_id": "sess-abc"},
        leased_devices=[],
    )


class LocalManifestProperties(unittest.TestCase):
    def test_local_manifest_is_deterministic_and_sensitive_to_every_mutation(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                tree = Path(tmp).resolve() / "out"
                tree.mkdir()
                files = build_tree(gen, tree)
                first = toolbox._local_manifest(tree)
                self.assertEqual(view(first), {rel: (sha(d), len(d)) for rel, d in files.items()})
                self.assertEqual(view(toolbox._local_manifest(tree)), view(first))
                relpaths = [i["relpath"] for i in first["files"]]
                self.assertEqual(relpaths, sorted(relpaths, key=lambda rel: Path(rel).parts), "entries are ordered by path components")
                rel = gen.choice(sorted(files))
                data = files[rel]
                kind = gen.choice(["append", "add", "remove", "rename"] + (["flip", "truncate"] if data else []))
                path = tree / rel
                if kind == "flip":
                    i = gen.integer(0, len(data) - 1)
                    path.write_bytes(data[:i] + bytes([data[i] ^ 0x80]) + data[i + 1:])
                elif kind == "truncate":
                    path.write_bytes(data[: gen.integer(0, len(data) - 1)])
                elif kind == "append":
                    path.write_bytes(data + b"!")
                elif kind == "add":
                    (tree / (entry_name(gen) + ".new")).write_bytes(b"new")
                elif kind == "remove":
                    path.unlink()
                else:
                    path.rename(tree / (rel + ".moved"))
                second = toolbox._local_manifest(tree)
                self.assertNotEqual(view(second), view(first), f"manifest did not notice {kind}")
                if kind in {"flip", "truncate", "append"}:
                    self.assertEqual(set(view(second)), set(view(first)))
                    self.assertNotEqual(view(second)[rel], view(first)[rel])
                else:
                    self.assertNotEqual(set(view(second)), set(view(first)))

        run_cases(150, body, label="toolbox local manifest")

    def test_symlinks_are_rejected_wherever_they_appear(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            with tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp).resolve()
                tree = base / "out"
                tree.mkdir()
                files = build_tree(gen, tree)
                (base / "secret").write_bytes(b"secret")
                link = tree / gen.choice([str(Path(rel).parent) for rel in files]) / "link"
                link.symlink_to(gen.choice((base / "secret", tree / gen.choice(sorted(files)))))
                with self.assertRaises(toolbox.RemoteToolboxError):
                    toolbox._local_manifest(tree)
                with self.assertRaises(toolbox.RemoteToolboxError):
                    toolbox._local_manifest(link)

        run_cases(30, body, label="toolbox manifest symlinks")


class PullHarness:
    def __init__(self, test: unittest.TestCase) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        test.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.remote_tree = self.base / "remote"
        self.remote_tree.mkdir()
        self.local_dir = self.base / "local"
        self.corrupt: dict[str, Any] = {}
        self.target = make_target(self.base)
        patchers = [
            mock.patch.object(toolbox, "remote_manifest", side_effect=self._manifest),
            mock.patch.object(toolbox, "_remote_tar_available", return_value=False),
            mock.patch.object(toolbox, "ssh_exec_bytes", side_effect=self._cat),
        ]
        for patcher in patchers:
            patcher.start()
            test.addCleanup(patcher.stop)

        self.frozen: dict[str, Any] | None = None

    def freeze_manifest(self) -> None:
        """Snapshot the manifest now, so later disk mutations model remote drift."""
        self.frozen = self._manifest(self.target, str(self.remote_tree))

    def _manifest(self, _target: RemoteTarget, remote_path: str, timeout: float | None = None) -> dict[str, Any]:
        if self.frozen is not None:
            return self.frozen
        local = toolbox._local_manifest(Path(remote_path))
        files = [{"relpath": i["relpath"], "path": i["path"], "size": i["size"], "sha256": i["sha256"]} for i in local["files"]]
        return {"artifacts": {"manifest": {"status": "ok", "remote_path": remote_path, "is_dir": Path(remote_path).is_dir(), "file_count": len(files), "files": files}}}

    def _cat(self, _endpoint: Any, command: str, timeout: float | None = None) -> subprocess.CompletedProcess[bytes]:
        path = Path(shlex.split(command)[1])
        if not path.exists():
            return subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"missing")
        data = path.read_bytes()
        mutate = self.corrupt.get(str(path))
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=mutate(data) if mutate else data, stderr=b"")


class PullProperties(unittest.TestCase):
    def test_pull_never_lands_bytes_that_disagree_with_the_manifest(self) -> None:
        def body(gen: Gen, _index: int) -> None:
            harness = PullHarness(self)
            files = build_tree(gen, harness.remote_tree)
            harness.freeze_manifest()
            expected = {rel: sha(d) for rel, d in files.items()}
            fault = gen.choice(("none", "corrupt", "missing"))
            victim = gen.choice(sorted(files))
            if fault == "corrupt":
                harness.corrupt[str(harness.remote_tree / victim)] = gen.choice((lambda d: d + b"\n", lambda d: d[:-1] if d else b"x", lambda d: b"" if d else b"\x00"))
            elif fault == "missing":
                (harness.remote_tree / victim).unlink()
            result = toolbox.artifact_pull(harness.target, remote_path=str(harness.remote_tree), local_dir=harness.local_dir)
            landed = {str(p.relative_to(harness.local_dir)): p for p in harness.local_dir.rglob("*") if p.is_file() and p.name != "manifest.json"}
            self.assertFalse([n for n in landed if n.endswith(".tmp")])
            for rel, path in landed.items():
                self.assertIn(rel, expected)
                self.assertEqual(sha(path.read_bytes()), expected[rel], f"{rel} landed with bytes that disagree with the manifest")
            if fault == "none":
                self.assertEqual(result["status"], "ok", result)
                self.assertEqual(set(landed), set(expected))
                again = toolbox.artifact_pull(harness.target, remote_path=str(harness.remote_tree), local_dir=harness.local_dir)
                self.assertEqual(again["artifacts"]["pulled"], [])
                self.assertEqual(len(again["artifacts"]["skipped"]), len(expected))
            else:
                self.assertEqual(result["status"], "failed", result)
                self.assertNotIn(victim, landed)
                if fault == "corrupt":
                    self.assertIn("hash mismatch", result["error"])

        run_cases(80, body, label="toolbox pull integrity")

    @unittest.expectedFailure
    def test_known_defect_manifest_relpath_traversal_escapes_the_local_dir(self) -> None:
        """KNOWN DEFECT (medium): ``artifact_pull`` / ``_artifact_pull_single``
        build ``local_dir / relpath`` straight from the remote manifest with no
        ``..``/absolute check, unlike ``.remote-dev/core/artifact_ops.py`` which
        has ``_safe_local_artifact_path``. A manifest entry ``../escaped.txt``
        is written *outside* ``local_dir`` and the pull reports ``ok``. The
        manifest normally comes from our own remote script, so this needs a
        hostile or substituted remote — a defense-in-depth gap, not a remote
        escape. Evidence: ``escaped.txt`` exists beside ``local_dir``."""
        harness = PullHarness(self)
        data = b"escaped\n"
        manifest = {"status": "ok", "is_dir": True, "remote_path": str(harness.remote_tree), "files": [
            {"relpath": "../escaped.txt", "path": str(harness.remote_tree / "x"), "size": len(data), "sha256": sha(data)},
        ]}
        (harness.remote_tree / "x").write_bytes(data)
        toolbox.remote_manifest.side_effect = lambda *_a, **_k: {"artifacts": {"manifest": manifest}}  # type: ignore[attr-defined]
        toolbox.ssh_exec_bytes.side_effect = lambda *_a, **_k: subprocess.CompletedProcess(args=[], returncode=0, stdout=data, stderr=b"")  # type: ignore[attr-defined]
        result = toolbox.artifact_pull(harness.target, remote_path=str(harness.remote_tree), local_dir=harness.local_dir)
        self.assertFalse((harness.base / "escaped.txt").exists(), "file written outside local_dir")
        self.assertNotEqual(result["status"], "ok")


if __name__ == "__main__":
    unittest.main()
