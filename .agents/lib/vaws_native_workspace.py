"""Copy native editing state before client startup.

Git owns objects and indexes. This module owns only a bounded copy operation;
it does not allocate tasks, infer session identity, or manage workspace leases.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from pathlib import Path


class WorkspaceCopyError(RuntimeError):
    pass


def git(root: Path, *args: str, data: bytes | None = None, env=None) -> bytes:
    result = subprocess.run(
        ["git", "--no-optional-locks", "-C", str(root), *args], input=data, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=env, timeout=120, check=False,
    )
    if result.returncode:
        raise WorkspaceCopyError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def _paths(raw: bytes) -> list[str]:
    return [os.fsdecode(item) for item in raw.split(b"\0") if item]


def _private(name: str) -> bool:
    # These copies are portable to case-insensitive Windows/macOS filesystems.
    return name.split("/", 1)[0].rstrip(" .").casefold() == ".vaws-local"


def _file_state(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        # Windows directory links have a distinct reparse-point type, including
        # dangling links. Do not infer it by following the target. POSIX has no
        # file/directory distinction in a symbolic link itself.
        directory = bool(getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_DIRECTORY) if os.name == "nt" else False
        return ["link", os.readlink(path), directory]
    if not stat.S_ISREG(info.st_mode):
        raise WorkspaceCopyError(f"unsupported file type: {path}")
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return ["file", stat.S_IMODE(info.st_mode), info.st_size, digest.hexdigest()]


def _capture(source: Path) -> dict:
    head = git(source, "rev-parse", "HEAD").decode().strip()
    flags = git(source, "ls-files", "-v", "-z").split(b"\0")
    if any(row and (row[:1].islower() or row[:1] == b"S") for row in flags):
        raise WorkspaceCopyError("sparse/assume-unchanged index entries require an ordinary checkout before copying")
    staged = git(source, "ls-files", "--stage", "-z")
    if any(row and row.split(b"\t", 1)[0].split()[-1] != b"0" for row in staged.split(b"\0")):
        raise WorkspaceCopyError("resolve the unmerged Git index before creating a workspace")
    modules = []
    for row in staged.split(b"\0"):
        if row.startswith(b"160000 "):
            modules.append(os.fsdecode(row.split(b"\t", 1)[1]))
    tracked = _paths(git(source, "ls-files", "-z"))
    if any(_private(name) for name in tracked):
        raise WorkspaceCopyError("tracked .vaws-local state must be removed from the index before creating a workspace")
    untracked = _paths(git(source, "ls-files", "--others", "--exclude-standard", "-z"))
    names = sorted(name for name in set(tracked + untracked) - set(modules) if not _private(name))
    files = {name: _file_state(source / name) for name in names}
    index_path = Path(os.fsdecode(git(source, "rev-parse", "--git-path", "index").strip()))
    if not index_path.is_absolute():
        index_path = source / index_path
    # write-tree may update the index's cache extension. Keep the real index
    # byte-for-byte unchanged and let Git interpret split indexes in its own dir.
    temporary = index_path.with_name("vaws-copy-index-" + uuid.uuid4().hex)
    try:
        shutil.copyfile(index_path, temporary)
        environment = {**os.environ, "GIT_INDEX_FILE": str(temporary)}
        tree = git(source, "write-tree", env=environment).decode().strip()
    finally:
        temporary.unlink(missing_ok=True)
    children = {}
    for name in modules:
        path = source / name
        if not (path / ".git").exists():
            # A normal non-recursive clone has empty, uninitialized gitlinks.
            # Keep that state without fetching source the task does not need.
            # Also distinguish a locally deleted directory from an empty one.
            if path.is_symlink() or (path.exists() and (not path.is_dir() or any(path.iterdir()))):
                raise WorkspaceCopyError(f"uninitialized submodule {name!r} contains non-Git content that cannot be copied as an empty gitlink")
            children[name] = {"uninitialized": True, "directory": path.is_dir()}
        else:
            children[name] = _capture(path)
    defaults = {"core.autocrlf": "false", "core.eol": "native", "core.safecrlf": "false",
                "core.filemode": "true", "core.ignorecase": "false", "core.symlinks": "true"}
    configuration = {key: git(source, "config", "--default", default, "--get", key).decode().strip()
                     for key, default in defaults.items()}
    # Freeze the effective end-of-line policy, rather than inheriting a different
    # Windows/macOS/WSL user's Git config. "native" itself is platform-relative.
    if configuration["core.eol"] == "native":
        configuration["core.eol"] = "crlf" if os.name == "nt" else "lf"
    status = git(source, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    status = b"\0".join(row for row in status.split(b"\0")
                         if not (row.startswith(b"?? ") and _private(os.fsdecode(row[3:]))))
    return {"head": head, "tree": tree, "tracked": tracked, "files": files, "modules": children,
            "configuration": configuration, "status": status.hex(),
            "staged_diff": git(source, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--cached").hex(),
            "working_diff": git(source, "diff", "--no-ext-diff", "--no-textconv", "--binary").hex()}


def _copy_repository(source: Path, destination: Path, snapshot: dict, *, linked: bool = False) -> None:
    if linked:
        git(source, "worktree", "add", "--detach", "--no-checkout", str(destination), snapshot["head"])
        # Ordinary linked trees already share the source's repository config.
        # If per-worktree config is enabled, preserve only the captured file
        # interpretation settings in this new tree, without editing the source.
        if git(source, "config", "--type=bool", "--default", "false", "--get", "extensions.worktreeConfig").strip() == b"true":
            for key, value in snapshot["configuration"].items():
                git(destination, "config", "--worktree", key, value)
        git(destination, "read-tree", snapshot["tree"])
        _copy_contents(source, destination, snapshot, linked=True)
        return
    # Local clone copies/hardlinks objects, with independent refs and .git dirs.
    # Unlike linked-worktree absolute gitdir pointers, these are readable by
    # native Windows Git and WSL Git on the same mounted filesystem.
    git(source, "clone", "--local", "--no-checkout", "--", str(source), str(destination))
    if (destination / ".git/objects/info/alternates").exists():
        raise WorkspaceCopyError("source uses object alternates; create a self-contained source checkout first")
    for key, value in snapshot["configuration"].items():
        git(destination, "config", key, value)
    git(destination, "update-ref", "HEAD", snapshot["head"])
    git(destination, "read-tree", snapshot["tree"])
    exclude = destination / ".git/info/exclude"
    with exclude.open("a", encoding="utf-8") as stream:
        stream.write("\n.vaws-local/\n")
    # Preserve upstream remotes instead of making the source working copy a
    # development remote. Credentials, if any, are never included in receipts.
    for remote in git(destination, "remote").decode().splitlines():
        git(destination, "remote", "remove", remote)
    for remote in git(source, "remote").decode().splitlines():
        urls = git(source, "remote", "get-url", "--all", remote).decode().splitlines()
        if urls:
            git(destination, "remote", "add", remote, urls[0])
            for url in urls[1:]:
                git(destination, "remote", "set-url", "--add", remote, url)
            push_urls = git(source, "remote", "get-url", "--push", "--all", remote).decode().splitlines()
            if push_urls != urls:
                for url in push_urls:
                    git(destination, "remote", "set-url", "--add", "--push", remote, url)
    _copy_contents(source, destination, snapshot)


def _copy_contents(source: Path, destination: Path, snapshot: dict, *, linked: bool = False) -> None:
    for name, state in snapshot["files"].items():
        if state is None or state[0] == "link":
            continue
        src, dst = source / name, destination / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for name, child in snapshot["modules"].items():
        if child.get("uninitialized"):
            if child["directory"]:
                (destination / name).mkdir(parents=True)
        else:
            _copy_repository(source / name, destination / name, child, linked=linked)
    # Targets, including submodules, exist before links are created. Explicit
    # Windows link types also preserve directory and dangling-directory links.
    for name, state in snapshot["files"].items():
        if state is not None and state[0] == "link":
            dst = destination / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(state[1], target_is_directory=state[2])
    actual_tracked = set(_paths(git(destination, "ls-files", "-z")))
    intent = sorted(set(snapshot["tracked"]) - actual_tracked)
    if intent:
        missing = [destination / name for name in intent if snapshot["files"][name] is None]
        for path in missing:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        try:
            git(destination, "add", "--intent-to-add", "--", *intent)
        finally:
            for path in missing:
                path.unlink(missing_ok=True)


_WINDOWS_COPY = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from vaws_native_workspace import create_workspace
print(json.dumps(create_workspace(Path(sys.argv[2]), Path(sys.argv[3])), ensure_ascii=True))
"""

_WINDOWS_LAUNCH = """
import sys
sys.path.insert(0, sys.argv[1])
from vaws_windows import owned_process
command = [sys.executable, '-I', '-X', 'utf8', '-c', sys.argv[4], *sys.argv[1:4]]
with owned_process(command, stdin=sys.stdin.buffer, stdout=sys.stdout.buffer, stderr=sys.stderr.buffer) as process:
    raise SystemExit(process.wait())
"""


def _copy_with_windows(source: Path, destination: Path, *, python: str) -> dict:
    """One bounded Windows-owned copy, callable directly by real bridge tests.

    Only mounted-drive paths are mapped. No path guessing, shell, RPC service,
    task identity or automatic replay is involved. The Windows launcher owns
    its copying child and Git descendants through the existing Job Object.
    """
    from vaws_local_owner import accessible_windows_path, managed_path

    try:
        arguments = [managed_path(value, windows=True)
                     for value in (Path(__file__).resolve().parent, source, destination)]
    except ValueError as exc:
        raise WorkspaceCopyError("Windows-owned workspace copying requires mounted-drive source, destination and helper paths") from exc
    command = [accessible_windows_path(python), "-I", "-X", "utf8", "-c", _WINDOWS_LAUNCH,
               *arguments, _WINDOWS_COPY]
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=600, check=False)
    except subprocess.TimeoutExpired as exc:
        raise WorkspaceCopyError("Windows copy timed out; destination outcome is unknown and the copy was not replayed") from exc
    except OSError as exc:
        raise WorkspaceCopyError(f"cannot start the selected Windows copy owner: {exc}") from exc
    if result.returncode:
        raise WorkspaceCopyError(result.stderr.decode("utf-8", "replace").strip() or "Windows copy owner failed")
    try:
        receipt = json.loads(result.stdout)
        if not isinstance(receipt, dict) or receipt.get("schema") != "vaws.native-workspace.v1" or receipt.get("state") != "ready":
            raise ValueError("Windows copy did not return a ready workspace")
        return {**receipt, "source": accessible_windows_path(receipt["source"]),
                "workspace": accessible_windows_path(receipt["workspace"])}
    except (ValueError, KeyError, TypeError) as exc:
        raise WorkspaceCopyError(f"invalid Windows copy response: {exc}") from exc


def create_workspace(source: Path, destination: Path, *, linked: bool = False) -> dict:
    """Copy HEAD, staged/working content and ordinary untracked files.

    Ignored files are excluded. Git storage is independent by default; native
    forks can retain the source's worktree family for the root and initialized
    submodules. Uninitialized gitlinks stay uninitialized without fetching.
    Existing destinations are never replaced. A failed copy stays
    visible for diagnosis and is never published as ready.
    """
    source, destination = source.resolve(), destination.absolute()
    from vaws_local_owner import windows_mounted_workspace

    if windows_mounted_workspace(source):
        if linked:
            raise WorkspaceCopyError("create linked worktrees with the native Windows owner for this mounted workspace")
        # WSL-created NTFS symlinks can use Linux-only reparse points. Native
        # Windows owns the whole operation, including Git pointer interpretation.
        # Lookup is read-only; an unavailable environment is never synthesized.
        from vaws_environment import EnvironmentError, windows_ready

        try:
            interpreter = windows_ready(Path(__file__).resolve().parents[2])["python"]
        except EnvironmentError as exc:
            raise WorkspaceCopyError(f"Windows workspace copy owner is not ready: {exc}") from exc
        return _copy_with_windows(source, destination, python=interpreter)
    if destination.exists():
        raise WorkspaceCopyError(f"workspace destination already exists: {destination}")
    top = Path(os.fsdecode(git(source, "rev-parse", "--show-toplevel").strip())).resolve()
    if top != source:
        raise WorkspaceCopyError("source must be the repository root")
    before = _capture(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _copy_repository(source, destination, before, linked=linked)
    if _capture(source) != before:
        raise WorkspaceCopyError(f"source changed while copying; incomplete workspace kept at {destination}")
    if _capture(destination) != before:
        raise WorkspaceCopyError(f"copied Git or file state differs; incomplete workspace kept at {destination}")
    receipt = {"schema": "vaws.native-workspace.v1", "source": str(source),
               "workspace": str(destination), "head": before["head"],
               "staged_tree": before["tree"], "submodules": list(before["modules"]),
               "state": "ready", "ignored_files": "excluded"}
    record = destination / ".vaws-local/native-workspace.json"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return receipt


def create_linked_workspace(source: Path, destination: Path) -> dict:
    """Fork current editing state inside its existing native Git worktree family."""
    return create_workspace(source, destination, linked=True)
