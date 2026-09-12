"""Stable per-key launch aliases; no active interpreter pointer is replaced."""
from __future__ import annotations

import os
from pathlib import Path


def link_environment(repo_root: Path, *, key: str, environment_root: Path) -> Path:
    """Give a ready environment a project-relative, permanently pinned path.

    Windows directory junctions work from native Windows and WSL, including
    across mounted drives. POSIX uses an ordinary directory symlink. Nothing
    here constructs, upgrades, deletes, or selects an environment.
    """
    if not key or any(char not in "0123456789abcdef" for char in key):
        raise ValueError("environment key must be a lowercase hexadecimal digest")
    target = environment_root.resolve(strict=True)
    alias = repo_root.resolve() / ".vaws-local/env-links" / key
    alias.parent.mkdir(parents=True, exist_ok=True)
    if os.path.lexists(alias):
        if alias.resolve(strict=True) != target:
            raise ValueError(f"environment alias already points elsewhere: {alias}")
        return alias
    try:
        if os.name == "nt":
            import _winapi
            _winapi.CreateJunction(str(target), str(alias))
        else:
            alias.symlink_to(target, target_is_directory=True)
    except FileExistsError:
        if alias.resolve(strict=True) != target:
            raise ValueError(f"concurrent environment alias points elsewhere: {alias}") from None
    if alias.resolve(strict=True) != target:
        raise ValueError(f"environment alias did not resolve to its ready environment: {alias}")
    return alias
