"""Scaffold ServiceConfig: packaged shared + this repo's project/candidate."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any, Mapping

from vaws_knowledge.redact import REDACTION_PROFILE
from vaws_knowledge.server.layers import ServiceConfig, load_config

SOURCE_REPO = "vllm-ascend-workspace/vaws-knowledge"
PROJECT_ROOT_RELATIVE = ".agents/knowledge"
CANDIDATE_ROOT_RELATIVE = ".vaws-local/knowledge/candidate"
SHARED_AVAILABLE = "available"
SHARED_ABSENT = "absent"
SHARED_REMEDY = "uv sync"


def infer_repo_root(knowledge_dir: Path, fallback: Path) -> Path:
    resolved = knowledge_dir.resolve()
    if resolved.parent.name == ".agents":
        return resolved.parent.parent
    return fallback


def origin_repo_from_url(url: str) -> str:
    text = url.strip()
    text = re.sub(r"\.git$", "", text)
    if text.startswith("git@") and ":" in text:
        return text.split(":", 1)[1]
    parts = [part for part in re.split(r"[/:]", text) if part]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return text or "local/unpublished"


def origin_repo_from_git(repo_root: Path) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return "local/unpublished"
    return origin_repo_from_url(proc.stdout.strip())


def knowledge_identity(repo_root: Path) -> dict[str, str]:
    return {
        "contributor": "anonymous",
        "origin_repo": origin_repo_from_git(repo_root),
        "redaction_profile": REDACTION_PROFILE,
    }


def knowledge_server_env(repo_root: Path) -> dict[str, str]:
    """Relative env paths, matching tracked mcp.json style."""

    identity = knowledge_identity(repo_root)
    return {
        "VAWS_KNOWLEDGE_PROJECT_ROOTS": PROJECT_ROOT_RELATIVE,
        "VAWS_KNOWLEDGE_CANDIDATE_ROOT": CANDIDATE_ROOT_RELATIVE,
        "VAWS_KNOWLEDGE_ORIGIN_REPO": identity["origin_repo"],
        "VAWS_KNOWLEDGE_REDACTION_PROFILE": identity["redaction_profile"],
    }


def probe_shared() -> dict[str, Any]:
    try:
        from vaws_knowledge import corpus as packaged
    except ImportError:
        return {
            "status": SHARED_ABSENT,
            "path": None,
            "detail": "vaws-knowledge is not installed",
            "remedy": SHARED_REMEDY,
            "problems": [],
            "documents": [],
            "source_repo": SOURCE_REPO,
            "source_ref": None,
        }
    root = packaged.corpus_root()
    source_ref = packaged.installed_commit()
    if not root.is_dir():
        return {
            "status": SHARED_ABSENT,
            "path": str(root),
            "detail": "installed vaws-knowledge has no corpus",
            "remedy": SHARED_REMEDY,
            "problems": [],
            "documents": [],
            "source_repo": SOURCE_REPO,
            "source_ref": source_ref,
        }
    files = list(packaged.iter_entry_files())
    return {
        "status": SHARED_AVAILABLE,
        "path": str(root),
        "detail": "shared layer is the installed vaws-knowledge corpus",
        "problems": [],
        "documents": [path.name for path in files],
        "source_repo": SOURCE_REPO,
        "source_ref": source_ref,
    }


def service_config(
    repo_root: Path,
    *,
    project_root: Path | None = None,
    candidate_root: Path | None = None,
) -> ServiceConfig:
    project = project_root or (repo_root / PROJECT_ROOT_RELATIVE)
    candidate = candidate_root or (repo_root / CANDIDATE_ROOT_RELATIVE)
    candidate.mkdir(parents=True, exist_ok=True)
    return load_config(
        {
            "layers": {
                "project": {"roots": [str(project)]},
                "candidate": {"root": str(candidate)},
            },
            "identity": knowledge_identity(repo_root),
        },
        env={},
        base_dir=repo_root,
    )


def scope_from_coordinate(coordinate: Mapping[str, str]) -> dict[str, Any]:
    from vaws_knowledge_v1 import COORDINATE_DIMENSIONS, COORDINATE_UNKNOWN

    scope: dict[str, Any] = {}
    for name in COORDINATE_DIMENSIONS:
        value = str(coordinate.get(name) or COORDINATE_UNKNOWN).strip() or COORDINATE_UNKNOWN
        scope[name] = {"values": [value]}
    return scope


def commons_entry(payload: Mapping[str, Any], coordinate: Mapping[str, str]) -> dict[str, Any]:
    slug = str(payload.get("entry_id") or payload.get("slug") or "captured-entry")
    rule = {
        "summary": str(payload.get("summary") or slug),
        "symptom": str(payload.get("symptom") or payload.get("summary") or slug),
        "root_cause": str(payload.get("root_cause") or "recorded at capture; mechanism not yet refined"),
        "resolution": str(payload.get("resolution") or "recorded at capture"),
    }
    if payload.get("avoidance"):
        rule["avoidance"] = str(payload["avoidance"])
    fingerprints = payload.get("fingerprints")
    if isinstance(fingerprints, list):
        rule["fingerprints"] = [str(item) for item in fingerprints if str(item).strip()]
    return {
        "slug": slug,
        "kind": str(payload.get("kind") or "known-failure-signatures"),
        "rule": rule,
        "scope": scope_from_coordinate(coordinate),
    }
