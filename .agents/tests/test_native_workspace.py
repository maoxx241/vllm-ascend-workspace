from __future__ import annotations

import concurrent.futures
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from vaws_native_workspace import WorkspaceCopyError, create_workspace, git
import vaws_native_workspace as workspace


def repository(path):
    path.mkdir()
    git(path, "init", "-q")
    git(path, "config", "user.name", "Workspace Test")
    git(path, "config", "user.email", "workspace@example.invalid")
    (path / "file.txt").write_text("base\n", encoding="utf-8")
    git(path, "add", ".")
    git(path, "commit", "-qm", "base")
    return path


def state(path):
    return (git(path, "rev-parse", "HEAD"), git(path, "diff", "--binary", "--cached"),
            git(path, "diff", "--binary"), git(path, "status", "--porcelain=v1", "-z"))


def test_parallel_first_tools_have_independent_index_and_resume_cwd(tmp_path):
    source = repository(tmp_path / "source 空格")
    (source / "file.txt").write_text("staged\n", encoding="utf-8")
    git(source, "add", "file.txt")
    (source / "file.txt").write_text("working\n", encoding="utf-8")
    (source / "untracked 中文.txt").write_text("untracked\n", encoding="utf-8")
    (source / "intent.txt").write_text("intent\n", encoding="utf-8")
    git(source, "add", "-N", "intent.txt")
    before = state(source)
    index = (source / ".git/index").read_bytes()
    targets = [tmp_path / "one", tmp_path / "two"]
    with concurrent.futures.ThreadPoolExecutor() as executor:
        receipts = list(executor.map(lambda path: create_workspace(source, path), targets))
    assert all(item["state"] == "ready" for item in receipts)
    assert all(state(path) == before for path in targets)
    assert (source / ".git/index").read_bytes() == index
    code = "from pathlib import Path; import subprocess,sys; p=Path('same-name.txt'); p.write_text(sys.argv[1]); subprocess.run(['git','add',str(p)],check=True); print(Path.cwd())"
    children = [subprocess.Popen([sys.executable, "-c", code, str(i)], cwd=path,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                for i, path in enumerate(targets)]
    for child, target in zip(children, targets):
        out, err = child.communicate(timeout=20)
        assert child.returncode == 0, err
        assert Path(os.fsdecode(out.strip())).resolve() == target.resolve()
    assert state(source) == before
    assert (source / ".git/index").read_bytes() == index
    assert [(path / "same-name.txt").read_text() for path in targets] == ["0", "1"]
    # Explicit directory resume starts in the same directory and retains edits.
    result = subprocess.check_output([sys.executable, "-c", "from pathlib import Path;print(Path('same-name.txt').read_text())"], cwd=targets[0])
    assert result.strip() == b"0"


def test_submodules_deleted_files_and_binary_stage_are_preserved(tmp_path):
    child = repository(tmp_path / "child")
    source = repository(tmp_path / "source")
    git(source, "-c", "protocol.file.allow=always", "submodule", "add", str(child), "nested")
    git(source, "commit", "-qam", "add child")
    (source / "file.txt").unlink()
    (source / "nested/file.txt").write_text("child staged\n")
    git(source / "nested", "add", "file.txt")
    (source / "nested/new.bin").write_bytes(bytes(range(256)))
    git(source / "nested", "add", "new.bin")
    (source / "nested/new.bin").write_bytes(b"working\x00\xff")
    before, child_before = state(source), state(source / "nested")
    target = tmp_path / "copy"
    create_workspace(source, target)
    assert (target / "nested/.git").is_dir()
    assert state(target) == before
    assert state(target / "nested") == child_before
    assert (target / "nested/new.bin").read_bytes() == b"working\x00\xff"
    assert state(source) == before
    assert state(source / "nested") == child_before


def test_existing_destination_is_untouched_and_uninitialized_module_is_explicit(tmp_path):
    source = repository(tmp_path / "source")
    target = tmp_path / "exists"
    target.mkdir()
    (target / "keep").write_text("keep")
    with pytest.raises(WorkspaceCopyError, match="already exists"):
        create_workspace(source, target)
    assert (target / "keep").read_text() == "keep"
    git(source, "update-index", "--add", "--cacheinfo", "160000", git(source, "rev-parse", "HEAD").decode().strip(), "missing")
    with pytest.raises(WorkspaceCopyError, match="initialize submodule"):
        create_workspace(source, tmp_path / "not-created")
    assert not (tmp_path / "not-created").exists()


def test_copy_keeps_git_line_policy_and_deleted_intent_without_private_state(tmp_path, monkeypatch):
    inherited = tmp_path / "global.gitconfig"
    inherited.write_text("[core]\n    autocrlf = input\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(inherited))
    source = repository(tmp_path / "source")
    git(source, "config", "core.autocrlf", "false")
    (source / "file.txt").write_bytes(b"staged\r\n")
    git(source, "add", "file.txt")
    (source / "file.txt").write_bytes(b"working\r\n")
    (source / "intent.txt").write_bytes(b"intent\r\n")
    (source / "removed-intent.txt").write_bytes(b"removed\r\n")
    git(source, "add", "-N", "intent.txt", "removed-intent.txt")
    (source / "removed-intent.txt").unlink()
    (source / "ordinary.txt").write_bytes(b"ordinary\r\n")
    (source / ".vaws-local").mkdir()
    (source / ".vaws-local/private.json").write_text("private runtime state")

    def editing_state(path):
        return (git(path, "rev-parse", "HEAD"), git(path, "diff", "--binary", "--cached"),
                git(path, "diff", "--binary"),
                git(path, "status", "--porcelain=v1", "-z", "--", "file.txt", "intent.txt",
                    "removed-intent.txt", "ordinary.txt"))

    before, index = editing_state(source), (source / ".git/index").read_bytes()
    target = tmp_path / "copy"
    assert create_workspace(source, target)["state"] == "ready"
    assert (source / ".git/index").read_bytes() == index
    assert editing_state(source) == editing_state(target) == before
    assert not (target / "removed-intent.txt").exists()
    assert not (target / ".vaws-local/private.json").exists()
    assert (source / ".vaws-local/private.json").read_text() == "private runtime state"
    assert (target / ".git").is_dir()
    # A client with a different inherited Git configuration reads the same
    # staged/working state because the target retains the source's policy.
    inherited.write_text("[core]\n    autocrlf = true\n", encoding="utf-8")
    assert editing_state(target) == before


@pytest.mark.parametrize("failure", ["tracked_reserved", "case_variant", "concurrent_edit"])
def test_invalid_or_changing_source_never_publishes_a_ready_workspace(tmp_path, monkeypatch, failure):
    source = repository(tmp_path / "source")
    target = tmp_path / "copy"
    if failure != "concurrent_edit":
        directory = ".VAWS-LOCAL" if failure == "case_variant" else ".vaws-local"
        reserved = source / directory / "native-workspace.json"
        reserved.parent.mkdir()
        reserved.write_bytes(b'{"original":"tracked content"}\n')
        git(source, "add", str(reserved.relative_to(source)))
        git(source, "commit", "-qm", "tracked reserved state")
        before = state(source)
        with pytest.raises(WorkspaceCopyError, match="tracked .vaws-local"):
            create_workspace(source, target)
        assert not target.exists()
        assert state(source) == before
        assert reserved.read_bytes() == b'{"original":"tracked content"}\n'
    else:
        original = workspace.shutil.copy2
        changed = False

        def copy_then_edit(src, dst, *args, **kwargs):
            nonlocal changed
            result = original(src, dst, *args, **kwargs)
            if not changed:
                changed = True
                (source / "file.txt").write_bytes(b"concurrent edit\n")
            return result

        monkeypatch.setattr(workspace.shutil, "copy2", copy_then_edit)
        with pytest.raises(WorkspaceCopyError, match="source changed while copying"):
            create_workspace(source, target)
        assert not (target / ".vaws-local/native-workspace.json").exists()
        assert (source / "file.txt").read_bytes() == b"concurrent edit\n"


def test_directory_symlink_retains_directory_behavior_after_copy(tmp_path):
    source = repository(tmp_path / "source")
    directory = source / "z-directory"
    directory.mkdir()
    (directory / "payload").write_bytes(b"through directory link\n")
    link = source / "a-link"
    try:
        link.symlink_to("z-directory", target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"native symlink creation is unavailable: {exc}")
    git(source, "add", ".")
    git(source, "commit", "-qm", "directory symlink")
    before, index = state(source), (source / ".git/index").read_bytes()
    target = tmp_path / "copy"
    assert create_workspace(source, target)["state"] == "ready"
    copied = target / "a-link"
    assert copied.is_symlink()
    assert copied.is_dir()
    assert (copied / "payload").read_bytes() == b"through directory link\n"
    if os.name == "nt":
        # Windows distinguishes file and directory reparse points even when
        # readlink text and Git mode/hash are otherwise identical.
        assert copied.lstat().st_file_attributes & 0x10
    assert state(source) == state(target) == before
    assert (source / ".git/index").read_bytes() == index
