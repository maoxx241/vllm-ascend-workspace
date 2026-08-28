"""Immutable environment and complete native-bundle attestations (stdlib only).

Preparation runs inside an owned, idle container, outside the checkout path.
This module never installs packages, creates containers, or loads a model.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import sysconfig
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

PROFILE_FIELDS = ("image_digest", "soc", "driver", "cann", "python_abi",
                  "torch", "torch_npu", "vllm", "vllm_ascend", "compiler")
PACKAGES = {"torch": "torch", "torch_npu": "torch-npu", "vllm": "vllm",
            "vllm_ascend": "vllm-ascend"}


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def checked_file(root: Path, relative: str) -> Path:
    name = PurePosixPath(relative)
    if name.is_absolute() or not name.parts or ".." in name.parts:
        raise ValueError(f"unsafe bundle path: {relative}")
    path = root / relative
    # Symlinked outputs are not a portable complete bundle. Enumerate their
    # real files during preparation rather than retaining external references.
    if any((root / Path(*name.parts[:i])).is_symlink() for i in range(1, len(name.parts) + 1)):
        raise ValueError(f"symlink in bundle path: {relative}")
    if not path.is_file():
        raise ValueError(f"missing required artifact: {relative}")
    if path.name == "CMakeCache.txt":
        raise ValueError("CMakeCache.txt is not a reusable runtime artifact")
    return path


def profile_key(profile: dict[str, Any]) -> str:
    if any(not isinstance(profile.get(key), str) or not profile[key].strip() for key in PROFILE_FIELDS):
        raise ValueError("profile requires exact versions: " + ", ".join(PROFILE_FIELDS))
    for key in ("build_env", "launch_env"):
        if not isinstance(profile.get(key), dict):
            raise ValueError(f"profile requires {key}")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in profile[key].items()):
            raise ValueError(f"{key} must contain string values")
        if any(any(word in k.upper() for word in ("TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")) for k in profile[key]):
            raise ValueError("profile environment must not contain secrets")
    if not profile.get("compatibility_evidence"):
        raise ValueError("profile requires an operator-reviewed compatibility evidence reference")
    return digest(profile)


def build_key(profile: dict[str, Any], inputs: dict[str, Any]) -> str:
    if not inputs.get("vllm") or not inputs.get("vllm-ascend"):
        raise ValueError("native input fingerprints for both repositories are required")
    return digest({"profile": profile_key(profile), "inputs": inputs})


def capture(root: Path, profile: dict[str, Any], inputs: dict[str, Any],
            files: dict[str, str], evidence: dict[str, str]) -> dict[str, Any]:
    """Describe explicitly enumerated outputs and compatibility/version evidence.

    Roles must include libraries AND metadata. Operators must enumerate every
    vendor configuration/binary required by their particular build.
    """
    if not {"library", "metadata"}.issubset(set(files.values())):
        raise ValueError("a complete bundle requires library and metadata roles")
    if not {"cann", "driver", "smoke"}.issubset(evidence):
        raise ValueError("CANN, driver and successful import-smoke evidence files are required")
    root = root.resolve()
    return {
        "schema_version": 1, "profile": profile, "profile_key": profile_key(profile),
        "build_inputs": inputs, "build_key": build_key(profile, inputs),
        # Relocation is deliberately not assumed for editable installs/operators.
        "runtime_root": str(root),
        "files": {name: {"sha256": file_digest(checked_file(root, name)), "role": role}
                  for name, role in sorted(files.items())},
        "evidence": {role: {"path": name, "sha256": file_digest(checked_file(root, name))}
                     for role, name in sorted(evidence.items())},
    }


def verify(root: Path, manifest: dict[str, Any], *, check_environment: bool = True) -> None:
    if manifest.get("schema_version") != 1:
        raise ValueError("unsupported runtime manifest")
    if profile_key(manifest["profile"]) != manifest["profile_key"] or build_key(manifest["profile"], manifest["build_inputs"]) != manifest["build_key"]:
        raise ValueError("profile/build identity mismatch")
    if not {"library", "metadata"}.issubset({row["role"] for row in manifest["files"].values()}):
        raise ValueError("incomplete native bundle")
    if not {"cann", "driver", "smoke"}.issubset(manifest["evidence"]):
        raise ValueError("missing environment evidence")
    for name, row in manifest["files"].items():
        if file_digest(checked_file(root, name)) != row["sha256"]:
            raise ValueError(f"artifact hash mismatch: {name}")
    for row in manifest["evidence"].values():
        if file_digest(checked_file(root, row["path"])) != row["sha256"]:
            raise ValueError("environment evidence changed")
    if check_environment:
        if str(root.resolve()) != manifest["runtime_root"]:
            raise ValueError("runtime relocation needs separate validation")
        profile = manifest["profile"]
        if sysconfig.get_config_var("SOABI") != profile["python_abi"]:
            raise ValueError("Python ABI changed")
        for key, package in PACKAGES.items():
            if importlib.metadata.version(package) != profile[key]:
                raise ValueError(f"installed package changed: {package}")


def publish(root: Path, cache: Path, manifest: dict[str, Any]) -> Path:
    """Atomically publish a complete, immutable bundle; no partial cache hits."""
    verify(root, manifest)
    cache.mkdir(parents=True, exist_ok=True)
    destination = cache / manifest["build_key"]
    if destination.exists():
        existing = json.loads((destination / "manifest.json").read_text())
        if existing != manifest:
            raise ValueError("same build inputs produced different artifacts; inspect reproducibility")
        verify(destination, existing, check_environment=False)
        return destination
    temp = Path(tempfile.mkdtemp(prefix=".publish-", dir=cache))
    try:
        names = set(manifest["files"]) | {row["path"] for row in manifest["evidence"].values()}
        if "manifest.json" in names:
            raise ValueError("reserved bundle path: manifest.json")
        for name in names:
            target = temp / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(checked_file(root, name), target)
        (temp / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        verify(temp, manifest, check_environment=False)
        os.rename(temp, destination)
        return destination
    finally:
        if temp.exists():
            shutil.rmtree(temp)


def restore(root: Path, bundle: Path, expected_build_key: str) -> None:
    """Preparation-only restoration, after owner proves all its workers stopped."""
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest["build_key"] != expected_build_key or str(root.resolve()) != manifest["runtime_root"]:
        raise ValueError("cache miss: build identity or installation path differs")
    verify(bundle, manifest, check_environment=False)
    for name in manifest["files"]:
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # Reject symlinked destinations, including existing parent directories.
        for parent in [target, *target.parents]:
            if parent == root:
                break
            if parent.is_symlink():
                raise ValueError("unsafe artifact restore destination")
        shutil.copy2(bundle / name, target)
    verify(root, manifest)
