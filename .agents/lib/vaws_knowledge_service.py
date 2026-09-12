"""Workspace roots and optional Markdown lookup over the installed knowledge owner."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Sequence

from vaws_local_owner import managed_path, managed_python, managed_receipt, windows_interop_env, windows_mounted_workspace

if TYPE_CHECKING:
    from vaws_knowledge.server.layers import ServiceConfig

PROJECT_ROOT_RELATIVE = ".agents/knowledge"
CANDIDATE_ROOT_RELATIVE = ".vaws-local/knowledge/candidate"
EXPERIENCE_PROJECT_ROOT_RELATIVE = ".agents/experiences"
EXPERIENCE_CANDIDATE_ROOT_RELATIVE = ".vaws-local/experience/candidate"


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
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
        )
    except OSError:
        return "local/unpublished"
    if proc.returncode != 0:
        return "local/unpublished"
    return origin_repo_from_url(proc.stdout.strip())


def knowledge_identity(repo_root: Path) -> dict[str, str]:
    return {
        "contributor": "anonymous",
        "origin_repo": origin_repo_from_git(repo_root),
    }


def knowledge_server_env(repo_root: Path) -> dict[str, str]:
    """Use the same roots from any client cwd or service-config directory."""

    repo_root = repo_root.resolve()
    identity = knowledge_identity(repo_root)
    result = {"VAWS_KNOWLEDGE_ORIGIN_REPO": identity["origin_repo"]}
    config_path = repo_root / ".vaws-local/knowledge/service.json"
    if config_path.is_file():
        result["VAWS_KNOWLEDGE_CONFIG"] = str(config_path)
    else:
        result["VAWS_KNOWLEDGE_PROJECT_ROOTS"] = str(repo_root / PROJECT_ROOT_RELATIVE)
        result["VAWS_KNOWLEDGE_CANDIDATE_ROOT"] = str(repo_root / CANDIDATE_ROOT_RELATIVE)
        result["VAWS_EXPERIENCE_PROJECT_ROOTS"] = str(repo_root / EXPERIENCE_PROJECT_ROOT_RELATIVE)
        result["VAWS_EXPERIENCE_CANDIDATE_ROOT"] = str(repo_root / EXPERIENCE_CANDIDATE_ROOT_RELATIVE)
        result["VAWS_KNOWLEDGE_STATE"] = str(repo_root / ".vaws-local/knowledge/instance")
    return result


def knowledge_owner_python(repo_root: Path) -> str:
    """A Windows-mounted workspace has one Windows knowledge database owner."""
    return managed_python(repo_root)


def knowledge_owner_path(repo_root: Path, value: object) -> str:
    return managed_path(value, windows=windows_mounted_workspace(repo_root))


def knowledge_owner_env(repo_root: Path) -> dict[str, str]:
    environment = knowledge_server_env(repo_root)
    environment["VAWS_ENV_RECEIPT"] = managed_receipt(repo_root)["receipt"]
    if windows_mounted_workspace(repo_root):
        for key in ("VAWS_KNOWLEDGE_CONFIG", "VAWS_KNOWLEDGE_PROJECT_ROOTS",
                    "VAWS_KNOWLEDGE_CANDIDATE_ROOT", "VAWS_KNOWLEDGE_STATE",
                    "VAWS_EXPERIENCE_PROJECT_ROOTS", "VAWS_EXPERIENCE_CANDIDATE_ROOT"):
            if key in environment:
                environment[key] = knowledge_owner_path(repo_root, environment[key])
        if os.environ.get("WSLENV"):
            environment["WSLENV"] = os.environ["WSLENV"]
        environment = windows_interop_env(environment)
    return environment


def run_knowledge_cli(repo_root: Path, arguments: Sequence[str]) -> tuple[int, dict[str, Any]]:
    """Run the installed owner without importing its runtime into the caller."""
    try:
        executable = knowledge_owner_python(repo_root)
    except (OSError, ValueError, RuntimeError) as exc:
        return 1, {"status": "pending", "ready": False, "reason": str(exc)}
    if not Path(executable).is_file():
        return 1, {"status": "pending", "ready": False,
                   "reason": "knowledge owner interpreter is not installed", "interpreter": executable}
    command = [executable, "-m", "vaws_knowledge", *arguments]
    try:
        completed = subprocess.run(
            command, cwd=str(repo_root), env={**os.environ, **knowledge_owner_env(repo_root)},
            stdout=subprocess.PIPE, stderr=sys.stderr, text=True, encoding="utf-8", check=False,
        )
        payload = json.loads(completed.stdout)
        if not isinstance(payload, dict):
            raise ValueError("knowledge command returned no JSON object")
    except (OSError, ValueError, TypeError) as exc:
        return 1, {"status": "pending", "ready": False, "reason": str(exc)}
    return completed.returncode, payload


def prepare_knowledge(repo_root: Path) -> dict[str, Any]:
    code, payload = run_knowledge_cli(
        repo_root, ["prepare", "--project", knowledge_owner_path(repo_root, repo_root)],
    )
    if code != 0 or payload.get("ready") is not True:
        return {**payload, "status": "pending", "ready": False}
    return payload


def service_config(
    repo_root: Path,
    *,
    project_root: Path | None = None,
    candidate_root: Path | None = None,
    kind: str = "knowledge",
) -> ServiceConfig:
    from vaws_knowledge.server.layers import load_config

    repo_root = repo_root.resolve()
    config_path = repo_root / ".vaws-local/knowledge/service.json"
    mapping: dict[str, Any] = {}
    if not config_path.is_file():
        mapping = {
            "state_root": str(repo_root / ".vaws-local/knowledge/instance"),
            "layers": {
                "project": {"roots": [str(repo_root / PROJECT_ROOT_RELATIVE)]},
                "candidate": {"root": str(repo_root / CANDIDATE_ROOT_RELATIVE)},
            },
            "identity": knowledge_identity(repo_root),
            "experience": {"layers": {
                "project": {"roots": [str(repo_root / EXPERIENCE_PROJECT_ROOT_RELATIVE)]},
                "candidate": {"root": str(repo_root / EXPERIENCE_CANDIDATE_ROOT_RELATIVE)},
            }},
        }
    if os.environ.get("VAWS_KNOWLEDGE_BACKEND"):
        mapping["backend"] = os.environ["VAWS_KNOWLEDGE_BACKEND"]
    if kind not in {"knowledge", "experience"}:
        raise ValueError(f"unknown reference kind: {kind}")
    if project_root is not None or candidate_root is not None:
        selected = mapping if kind == "knowledge" else mapping.setdefault("experience", {})
        if project_root is not None:
            selected.setdefault("layers", {})["project"] = {"roots": [str(project_root)]}
        if candidate_root is not None:
            selected.setdefault("layers", {})["candidate"] = {"root": str(candidate_root)}
    return load_config(
        mapping,
        env={},
        path=config_path if config_path.is_file() else None,
        base_dir=repo_root,
    ).for_kind(kind)


def query_knowledge(*, knowledge_dir: Path, query: str, limit: int = 3) -> dict[str, Any]:
    """Keep index availability distinct from a successful query with zero results."""
    return _query_references(directory=knowledge_dir, query=query, limit=limit, kind="knowledge")


def query_experience(*, experience_dir: Path, query: str, limit: int = 3) -> dict[str, Any]:
    """Look up historical cases separately from current knowledge."""
    return _query_references(directory=experience_dir, query=query, limit=limit, kind="experience")


def _query_references(*, directory: Path, query: str, limit: int, kind: str) -> dict[str, Any]:
    repo = directory.parent.parent if directory.parent.name == ".agents" else directory.parent
    try:
        from vaws_knowledge.server.query import query as package_query
        result = package_query(service_config(repo, project_root=directory, kind=kind), text=query, limit=limit)
        return result.to_dict()
    except Exception as exc:
        return {"kind": kind, "results": [], "unavailable": True, "degraded": True, "index_detail": str(exc)}
