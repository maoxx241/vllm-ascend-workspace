"""Workspace roots and optional Markdown lookup over the installed knowledge owner."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any

from vaws_knowledge.redact import REDACTION_PROFILE
from vaws_knowledge.server.layers import ServiceConfig, load_config

PROJECT_ROOT_RELATIVE = ".agents/knowledge"
CANDIDATE_ROOT_RELATIVE = ".vaws-local/knowledge/candidate"


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
    result = {
        "VAWS_KNOWLEDGE_PROJECT_ROOTS": PROJECT_ROOT_RELATIVE,
        "VAWS_KNOWLEDGE_CANDIDATE_ROOT": CANDIDATE_ROOT_RELATIVE,
        "VAWS_KNOWLEDGE_ORIGIN_REPO": identity["origin_repo"],
        "VAWS_KNOWLEDGE_REDACTION_PROFILE": identity["redaction_profile"],
    }
    config_path = repo_root / ".vaws-local/knowledge/service.json"
    if config_path.is_file():
        result["VAWS_KNOWLEDGE_CONFIG"] = str(config_path)
    return result



def service_config(
    repo_root: Path,
    *,
    project_root: Path | None = None,
    candidate_root: Path | None = None,
) -> ServiceConfig:
    project = project_root or (repo_root / PROJECT_ROOT_RELATIVE)
    candidate = candidate_root or (repo_root / CANDIDATE_ROOT_RELATIVE)
    config_path = repo_root / ".vaws-local/knowledge/service.json"
    return load_config(
        {
            "backend": os.environ.get("VAWS_KNOWLEDGE_BACKEND", "openviking"),
            "layers": {
                "project": {"roots": [str(project)]},
                "candidate": {"root": str(candidate)},
            },
            "identity": knowledge_identity(repo_root),
        },
        env={},
        path=config_path if config_path.is_file() else None,
        base_dir=repo_root,
    )



def query_knowledge(*, knowledge_dir: Path, query: str, limit: int = 3) -> dict[str, Any]:
    """Keep index availability distinct from a successful query with zero results."""
    repo = knowledge_dir.parent.parent if knowledge_dir.parent.name == ".agents" else knowledge_dir.parent
    try:
        from vaws_knowledge.server.query import query as package_query
        result = package_query(service_config(repo, project_root=knowledge_dir), text=query, limit=limit)
        return result.to_dict()
    except Exception as exc:
        return {"results": [], "unavailable": True, "degraded": True, "index_detail": str(exc)}
