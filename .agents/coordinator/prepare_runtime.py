#!/usr/bin/env python3
"""Attest/cache an already built runtime, inside an owned idle Linux container.

Use existing machine-management/parity installers BEFORE this command. No
package installation, container creation, card allocation or model loading.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / ".agents/lib"))
from vaws_runtime_profile import capture, file_digest, profile_key, publish, restore, verify
from vaws_build_inputs import runtime_build_inputs


def attest(root: Path, spec: dict):
    profile = spec["profile"]
    key = profile_key(profile)
    for name in ("vllm", "vllm-ascend"):
        dirty = subprocess.check_output(["git", "-C", str(root / name), "status", "--porcelain", "--untracked-files=all"], text=True)
        if dirty.strip():
            raise ValueError("attest a clean materialized parity snapshot, including child submodules")
    inputs = runtime_build_inputs(root, profile, key)
    evidence_dir = root / ".vaws-runtime/profile-evidence"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    # Preserve the prepared image environment when overlaying launch settings.
    environment = os.environ.copy()
    for name, value in profile["launch_env"].items():
        environment[name] = value + (":" + environment[name] if name in {"PATH", "PYTHONPATH", "LD_LIBRARY_PATH"} and environment.get(name) else "")
    smoke = subprocess.run([sys.executable, "-c", "import torch_npu, vllm, vllm_ascend, acl"], env=environment,
                           capture_output=True, text=True, timeout=60)
    (evidence_dir / "smoke.json").write_text(json.dumps({"passed": smoke.returncode == 0,
                                                       "build_inputs": inputs, "profile_key": key,
                                                       "stderr": smoke.stderr[-8000:]}, indent=2))
    if smoke.returncode:
        raise ValueError("import smoke failed; inspect .vaws-runtime/profile-evidence/smoke.json")
    for name in ("cann", "driver"):
        row = profile["system_files"][name]
        if file_digest(Path(row["path"])) != row["sha256"]:
            raise ValueError("actual environment differs from requested profile")
        (evidence_dir / (name + ".json")).write_text(json.dumps(row, sort_keys=True))
    evidence = {name: ".vaws-runtime/profile-evidence/" + name + ".json" for name in ("cann", "driver", "smoke")}
    manifest = capture(root, profile, inputs, spec["files"], evidence)
    verify(root, manifest)
    # New attestations replace the marker only after all checks have passed.
    marker = root / ".vaws-runtime/ready-profile.json"
    temp = marker.with_suffix(".tmp")
    temp.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    os.replace(temp, marker)
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("attest", "publish", "restore"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--spec", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--build-key")
    parser.add_argument("--owned-workers-stopped", action="store_true")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("preparation runs inside the owned Linux container")
    if not args.owned_workers_stopped:
        parser.error("first stop and verify only your workers, then pass --owned-workers-stopped")
    root = args.root.resolve()
    if args.action == "attest":
        if args.spec is None:
            parser.error("attest requires --spec")
        result = attest(root, json.loads(args.spec.read_text()))
    elif args.action == "publish":
        if args.cache is None:
            parser.error("publish requires --cache")
        manifest = json.loads((root / ".vaws-runtime/ready-profile.json").read_text())
        result = {"bundle": str(publish(root, args.cache, manifest)), "build_key": manifest["build_key"]}
    else:
        if args.cache is None or not args.build_key:
            parser.error("restore requires --cache and --build-key")
        if len(args.build_key) != 64 or any(c not in "0123456789abcdef" for c in args.build_key):
            parser.error("build key must be a SHA256 digest")
        restore(root, args.cache / args.build_key, args.build_key)
        result = {"status": "restored", "build_key": args.build_key}
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
