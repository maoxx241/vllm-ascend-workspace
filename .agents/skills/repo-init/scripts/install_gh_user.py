#!/usr/bin/env python3
"""Install GitHub CLI into a user-writable prefix without sudo/admin.

Supported platforms:
  - macOS
  - Linux
Use the companion PowerShell script on Windows.
"""

from __future__ import annotations

import io
import json
import os
import pathlib
import platform
import re
import shutil
import stat
import sys
import tarfile
import tempfile
import urllib.request
import zipfile


API_URL = "https://api.github.com/repos/cli/cli/releases/latest"


def fail(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def detect_target() -> tuple[str, str, str]:
    system = platform.system()
    machine = platform.machine().lower()

    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    arch = arch_map.get(machine)
    if not arch:
        fail(f"unsupported architecture: {machine}")

    if system == "Darwin":
        return "macOS", arch, "zip"
    if system == "Linux":
        return "linux", arch, "tar.gz"

    fail("this fallback installer supports only macOS and Linux")


def latest_release() -> dict:
    req = urllib.request.Request(
        API_URL,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "repo-init-fallback"},
    )
    with urllib.request.urlopen(req) as resp:
        return json.load(resp)


def select_asset(release: dict, os_token: str, arch: str, ext: str) -> dict:
    pattern = re.compile(rf"^gh_(?P<version>[^_]+)_{re.escape(os_token)}_{re.escape(arch)}\.{re.escape(ext)}$")
    for asset in release.get("assets", []):
        name = asset.get("name", "")
        if pattern.match(name):
            return asset
    fail(f"could not find a matching release asset for {os_token}/{arch}.{ext}")


def ensure_dir(path: pathlib.Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def install_zip(archive_path: pathlib.Path, target_bin: pathlib.Path) -> None:
    with zipfile.ZipFile(archive_path) as zf:
        members = [name for name in zf.namelist() if name.endswith("/bin/gh")]
        if not members:
            fail("zip archive does not contain bin/gh")
        member = members[0]
        with zf.open(member) as src, open(target_bin, "wb") as dst:
            shutil.copyfileobj(src, dst)


def install_tar(archive_path: pathlib.Path, target_bin: pathlib.Path) -> None:
    with tarfile.open(archive_path, "r:gz") as tf:
        members = [m for m in tf.getmembers() if m.name.endswith("/bin/gh")]
        if not members:
            fail("tar archive does not contain bin/gh")
        member = members[0]
        src = tf.extractfile(member)
        if src is None:
            fail("failed to read gh binary from tar archive")
        with src, open(target_bin, "wb") as dst:
            shutil.copyfileobj(src, dst)


def main() -> None:
    os_token, arch, ext = detect_target()
    release = latest_release()
    asset = select_asset(release, os_token, arch, ext)

    home = pathlib.Path.home()
    install_root = home / ".local" / "gh" / release["tag_name"]
    install_bin_dir = install_root / "bin"
    link_bin_dir = home / ".local" / "bin"
    ensure_dir(install_bin_dir)
    ensure_dir(link_bin_dir)

    target_bin = install_bin_dir / "gh"
    link_bin = link_bin_dir / "gh"

    with tempfile.TemporaryDirectory(prefix="repo-init-gh-") as tmp_dir:
        archive_path = pathlib.Path(tmp_dir) / asset["name"]
        print(f"Downloading {asset['name']} ...")
        urllib.request.urlretrieve(asset["browser_download_url"], archive_path)

        if ext == "zip":
            install_zip(archive_path, target_bin)
        else:
            install_tar(archive_path, target_bin)

    mode = target_bin.stat().st_mode
    target_bin.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    if link_bin.exists() or link_bin.is_symlink():
        link_bin.unlink()
    try:
        link_bin.symlink_to(target_bin)
    except OSError:
        shutil.copy2(target_bin, link_bin)

    print(f"Installed gh to {target_bin}")
    print(f"User-facing command path: {link_bin}")

    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if str(link_bin_dir) not in path_entries:
        print("")
        print("Add this directory to PATH if needed:")
        print(f"  export PATH=\"{link_bin_dir}:$PATH\"")

    print("")
    print("Verify with:")
    print("  gh --version")
    print("  gh auth status --hostname github.com")


if __name__ == "__main__":
    main()
