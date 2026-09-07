#!/usr/bin/env python3
"""Three-layer knowledge read client with capability probing.

One query surface, three layers with different trust (see the federated
commons README):

| layer       | source                                    | trust |
|-------------|-------------------------------------------|-------|
| `shared`    | read-only cache pulled from `vaws-knowledge` | evidence + non-submitter confirmation |
| `project`   | `.agents/knowledge/` in this repo          | tied to this checkout, may be non-public |
| `candidate` | `.vaws-local/knowledge/candidates/`        | unreviewed single observation |

Two rules drive the whole module:

- **Never fail hard.** A missing cache, a malformed document or an absent
  knowledge service degrades the answer; it never becomes the reason a
  diagnosis stops.
- **A missing fact is never "supported".** Every payload states which layers
  actually answered, which did not, and that an empty result means unknown.
  That inversion — reading absence as approval — is an explicit repo-wide rule
  in `AGENTS.md`.

The shared layer is served from a local read-only cache. Calling into the
upstream knowledge MCP service is deliberately *not* implemented here: the
service is not published yet, so the probe reports it as absent rather than
pretending to consult it.
"""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping, Sequence

import vaws_knowledge as v1
import vaws_knowledge_v2 as v2

LAYERS: tuple[str, ...] = ("shared", "project", "candidate")
DEFAULT_LAYERS: tuple[str, ...] = ("shared", "project")

SHARED_CACHE_METADATA = "cache-metadata.json"
MCP_ENDPOINT_FILE = "mcp-endpoint.json"
MCP_ENDPOINT_ENV = "VAWS_KNOWLEDGE_MCP_ENDPOINT"

UNKNOWN_SEMANTICS = (
    "An empty or partial result means the fact is unknown to the layers listed "
    "in coverage.layers_answered. It never means the behaviour is supported."
)


def default_paths(repo_root: Path) -> dict[str, Path]:
    return {
        "knowledge_dir": repo_root / ".agents" / "knowledge",
        "shared_dir": repo_root / ".vaws-local" / "knowledge" / "shared",
        "candidate_dir": repo_root / ".vaws-local" / "knowledge" / "candidates",
    }


# ---------------------------------------------------------------------------
# capability probe
# ---------------------------------------------------------------------------


def _probe_project(knowledge_dir: Path) -> dict[str, Any]:
    if not knowledge_dir.is_dir():
        return {
            "status": "absent",
            "path": str(knowledge_dir),
            "detail": "project knowledge directory does not exist",
        }
    v1_files = sorted(
        path.name for path in knowledge_dir.glob("*.yaml") if path.name in v1.KNOWLEDGE_FILES
    )
    v2_files = [path.name for path, _ in v2.iter_documents(knowledge_dir)]
    _, problems = v2.load_entries(knowledge_dir)
    return {
        "status": "degraded" if problems else "available",
        "path": str(knowledge_dir),
        "v1_documents": v1_files,
        "v2_documents": v2_files,
        "problems": problems,
    }


def _probe_shared(shared_dir: Path) -> dict[str, Any]:
    metadata_path = shared_dir / SHARED_CACHE_METADATA
    if not shared_dir.is_dir() or not metadata_path.is_file():
        return {
            "status": "absent",
            "path": str(shared_dir),
            "detail": "no shared cache pulled from vaws-knowledge yet",
            "remedy": "python3 .agents/scripts/knowledge_shared_cache.py import --from <clone>/corpus/verified",
        }
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "status": "degraded",
            "path": str(shared_dir),
            "detail": f"shared cache metadata unreadable: {exc}",
        }
    _, problems = v2.load_entries(shared_dir, context=v2.PROJECT_LAYER)
    return {
        "status": "degraded" if problems else "available",
        "path": str(shared_dir),
        "source_repo": metadata.get("source_repo"),
        "source_ref": metadata.get("source_ref"),
        "pulled_at": metadata.get("pulled_at"),
        "documents": [path.name for path, _ in v2.iter_documents(shared_dir)],
        "problems": problems,
    }


def _probe_candidates(candidate_dir: Path) -> dict[str, Any]:
    if not candidate_dir.is_dir():
        return {
            "status": "absent",
            "path": str(candidate_dir),
            "detail": "no local candidate queue",
        }
    return {
        "status": "available",
        "path": str(candidate_dir),
        "count": len(list(candidate_dir.glob("*.json"))),
    }


def _probe_mcp(repo_root: Path) -> dict[str, Any]:
    endpoint = os.environ.get(MCP_ENDPOINT_ENV)
    descriptor = repo_root / ".vaws-local" / "knowledge" / MCP_ENDPOINT_FILE
    if not endpoint and not descriptor.is_file():
        return {
            "status": "absent",
            "detail": (
                "knowledge MCP service not configured; shared results come from the "
                "local read-only cache only"
            ),
        }
    return {
        "status": "configured",
        "detail": (
            "an endpoint is configured but this client does not call it yet; the "
            "upstream server package is unpublished, so shared results still come "
            "from the local cache"
        ),
        "source": MCP_ENDPOINT_ENV if endpoint else str(descriptor),
    }


def probe_capabilities(
    *,
    repo_root: Path,
    knowledge_dir: Path | None = None,
    shared_dir: Path | None = None,
    candidate_dir: Path | None = None,
) -> dict[str, Any]:
    paths = default_paths(repo_root)
    return {
        "project": _probe_project(knowledge_dir or paths["knowledge_dir"]),
        "shared": _probe_shared(shared_dir or paths["shared_dir"]),
        "candidate": _probe_candidates(candidate_dir or paths["candidate_dir"]),
        "knowledge_mcp": _probe_mcp(repo_root),
        "validator": {
            "status": "stdlib",
            "detail": (
                "jsonschema is unavailable in this environment; the v2 contract is "
                "enforced by .agents/lib/vaws_knowledge_v2.py"
            ),
            "jsonschema": False,
            "pyyaml": v2.yaml_available(),
        },
    }


# ---------------------------------------------------------------------------
# querying
# ---------------------------------------------------------------------------


def _v2_matches(
    entries: Sequence[Mapping[str, Any]],
    *,
    layer: str,
    query: str,
    kinds: Sequence[str] | None,
    include_unverified: bool,
    include_deprecated: bool,
    min_score: int,
) -> list[dict[str, Any]]:
    selected = set(kinds) if kinds else None
    matches: list[dict[str, Any]] = []
    for entry in entries:
        kind = entry.get("_kind")
        if selected is not None and kind not in selected:
            continue
        status = entry.get("status")
        if status == "deprecated" and not include_deprecated:
            continue
        if status == "unverified" and not include_unverified:
            continue
        view = v2.match_view(entry)
        score = v1.score_entry(query, view)
        if score <= 0 or score < min_score:
            continue
        rule = entry.get("rule", {})
        matches.append(
            {
                "id": entry.get("slug"),
                "uuid": entry.get("uuid"),
                "kind": kind,
                "layer": layer,
                "schema_version": v2.SCHEMA_VERSION,
                "status": status,
                "summary": str(rule.get("summary") or rule.get("symptom") or entry.get("slug")),
                "applicable_versions": view["applicable_versions"],
                "scope_summary": view["applicable_versions"],
                "unresolved_dimensions": v2.unresolved_dimensions(entry),
                "warning": v2.status_warning(entry),
                "score": score,
                "source_file": entry.get("_source_file"),
            }
        )
    return matches


def _candidate_matches(
    candidate_dir: Path,
    *,
    query: str,
    kinds: Sequence[str] | None,
    min_score: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    matches: list[dict[str, Any]] = []
    problems: list[str] = []
    if not candidate_dir.is_dir():
        return matches, problems
    selected = set(kinds) if kinds else None
    for path in sorted(candidate_dir.glob("*.json")):
        try:
            candidate = v1.load_candidate(path)
        except v1.KnowledgeError as exc:
            problems.append(str(exc))
            continue
        if selected is not None and candidate.get("kind") not in selected:
            continue
        view = {
            "id": candidate["candidate_id"],
            "rule": {
                "summary": candidate["summary"],
                "symptom": candidate["symptom"],
                "root_cause": candidate["root_cause"],
                "resolution": candidate["resolution"],
                "fingerprints": candidate["fingerprints"],
            },
        }
        score = v1.score_entry(query, view)
        if score <= 0 or score < min_score:
            continue
        environment = candidate.get("environment") or {}
        unknown = sorted(
            name
            for name in v2.SCOPE_DIMENSIONS
            if str(environment.get(name, v1.COORDINATE_UNKNOWN)) == v1.COORDINATE_UNKNOWN
        )
        matches.append(
            {
                "id": candidate["candidate_id"],
                "uuid": None,
                "kind": candidate["kind"],
                "layer": "candidate",
                "schema_version": candidate.get("schema_version"),
                "status": "candidate",
                "summary": candidate["summary"],
                "applicable_versions": candidate.get("applicable_versions", ""),
                "scope_summary": candidate.get("applicable_versions", ""),
                "unresolved_dimensions": unknown,
                "warning": "candidate: unreviewed local observation",
                "score": score,
                "source_file": path.name,
            }
        )
    return matches, problems


def query(
    *,
    repo_root: Path,
    query: str,
    layers: Sequence[str] = DEFAULT_LAYERS,
    kinds: Sequence[str] | None = None,
    limit: int = 3,
    include_unverified: bool = False,
    include_deprecated: bool = False,
    min_score: int = 0,
    knowledge_dir: Path | None = None,
    shared_dir: Path | None = None,
    candidate_dir: Path | None = None,
) -> dict[str, Any]:
    """Query the configured layers and describe exactly what answered."""

    if limit < 1:
        raise v1.KnowledgeError("limit must be greater than zero")
    unknown_layers = sorted(set(layers) - set(LAYERS))
    if unknown_layers:
        raise v1.KnowledgeError(f"unknown layers: {', '.join(unknown_layers)}")

    paths = default_paths(repo_root)
    knowledge_dir = knowledge_dir or paths["knowledge_dir"]
    shared_dir = shared_dir or paths["shared_dir"]
    candidate_dir = candidate_dir or paths["candidate_dir"]

    capabilities = probe_capabilities(
        repo_root=repo_root,
        knowledge_dir=knowledge_dir,
        shared_dir=shared_dir,
        candidate_dir=candidate_dir,
    )

    matches: list[dict[str, Any]] = []
    answered: list[str] = []
    missing: list[dict[str, Any]] = []
    problems: list[str] = []

    if "project" in layers:
        capability = capabilities["project"]
        if capability["status"] == "absent":
            missing.append(
                {
                    "layer": "project",
                    "status": "absent",
                    "detail": capability.get("detail", ""),
                    "effect": "no project-layer facts were consulted",
                }
            )
        else:
            answered.append("project")
            # A document that failed to load answered nothing. Surfacing that
            # only inside the capability probe would let a caller read the
            # top-level ``problems: []`` as "the layer had nothing to say".
            for detail in capability.get("problems", []):
                problems.append(detail)
                missing.append(
                    {
                        "layer": "project",
                        "status": "degraded",
                        "detail": detail,
                        "effect": "one project document did not load; its facts "
                        "were not consulted",
                    }
                )
            try:
                for match in v1.query_knowledge(
                    knowledge_dir=knowledge_dir,
                    query=query,
                    kinds=kinds,
                    limit=limit * 4,
                    include_deprecated=include_deprecated,
                    include_unverified=include_unverified,
                    min_score=min_score,
                ):
                    match.setdefault("layer", "project")
                    matches.append(match)
            except v1.KnowledgeError as exc:
                problems.append(str(exc))
                missing.append(
                    {
                        "layer": "project",
                        "status": "degraded",
                        "detail": str(exc),
                        "effect": "project-layer facts are incomplete",
                    }
                )

    if "shared" in layers:
        capability = capabilities["shared"]
        if capability["status"] == "absent":
            missing.append(
                {
                    "layer": "shared",
                    "status": "absent",
                    "detail": capability.get("detail", ""),
                    "remedy": capability.get("remedy", ""),
                    "effect": "degraded to project+candidate; shared facts were not consulted",
                }
            )
        else:
            answered.append("shared")
            shared_entries, shared_problems = v2.load_entries(shared_dir)
            problems.extend(shared_problems)
            matches.extend(
                _v2_matches(
                    shared_entries,
                    layer="shared",
                    query=query,
                    kinds=kinds,
                    include_unverified=include_unverified,
                    include_deprecated=include_deprecated,
                    min_score=min_score,
                )
            )

    if "candidate" in layers:
        capability = capabilities["candidate"]
        if capability["status"] == "absent":
            missing.append(
                {
                    "layer": "candidate",
                    "status": "absent",
                    "detail": capability.get("detail", ""),
                    "effect": "no local candidate observations were consulted",
                }
            )
        else:
            answered.append("candidate")
            candidate_matches, candidate_problems = _candidate_matches(
                candidate_dir, query=query, kinds=kinds, min_score=min_score
            )
            problems.extend(candidate_problems)
            matches.extend(candidate_matches)

    if capabilities["knowledge_mcp"]["status"] != "available":
        missing.append(
            {
                "layer": "knowledge_mcp",
                "status": capabilities["knowledge_mcp"]["status"],
                "detail": capabilities["knowledge_mcp"]["detail"],
                "effect": "shared results come from the local read-only cache",
            }
        )

    layer_rank = {"shared": 0, "project": 1, "candidate": 2}
    matches.sort(
        key=lambda item: (
            -int(item.get("score", 0)),
            layer_rank.get(str(item.get("layer")), 9),
            str(item.get("kind")),
            str(item.get("id")),
        )
    )
    selected = matches[:limit]
    return {
        "status": "passed",
        "query": query,
        "matches": selected,
        "degraded": bool(missing),
        "degradation": missing,
        "coverage": {
            "layers_requested": list(layers),
            "layers_answered": answered,
            "layers_unavailable": [item["layer"] for item in missing],
            "result": "match" if selected else "no-match",
        },
        "unknown_semantics": UNKNOWN_SEMANTICS,
        "problems": problems,
        "capabilities": capabilities,
    }


def get_entry(
    *,
    repo_root: Path,
    entry_id: str,
    layers: Sequence[str] = LAYERS,
    knowledge_dir: Path | None = None,
    shared_dir: Path | None = None,
    candidate_dir: Path | None = None,
) -> dict[str, Any] | None:
    """Fetch one full entry by v1 id, v2 slug, v2 uuid, or candidate id."""

    paths = default_paths(repo_root)
    knowledge_dir = knowledge_dir or paths["knowledge_dir"]
    shared_dir = shared_dir or paths["shared_dir"]
    candidate_dir = candidate_dir or paths["candidate_dir"]

    if "project" in layers and knowledge_dir.is_dir():
        try:
            found = v1.get_knowledge_entry(knowledge_dir=knowledge_dir, entry_id=entry_id)
        except v1.KnowledgeError:
            found = None
        if found:
            found.setdefault("layer", "project")
            return found
    if "shared" in layers:
        entries, _ = v2.load_entries(shared_dir)
        for entry in entries:
            if entry_id in {entry.get("slug"), entry.get("uuid")}:
                record = deepcopy(entry)
                return {
                    "layer": "shared",
                    "kind": record.pop("_kind", None),
                    "source_file": record.pop("_source_file", None),
                    "schema_version": v2.SCHEMA_VERSION,
                    "entry": record,
                }
    if "candidate" in layers and candidate_dir.is_dir():
        path = candidate_dir / f"{entry_id}.json"
        if path.is_file():
            try:
                candidate = v1.load_candidate(path)
            except v1.KnowledgeError:
                candidate = None
            if candidate is not None:
                return {
                    "layer": "candidate",
                    "kind": candidate.get("kind"),
                    "source_file": path.name,
                    "schema_version": candidate.get("schema_version"),
                    "entry": candidate,
                }
    return None
