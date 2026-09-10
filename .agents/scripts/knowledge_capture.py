#!/usr/bin/env python3
"""Capture one knowledge candidate through the installed commons engine.

Required fields are title and non-empty content. Known source, conditions,
and evidence are kept when present. Writes Markdown under
``.vaws-local/knowledge/candidate/``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

import vaws_redaction as redaction  # noqa: E402
from vaws_knowledge.server.capture import CaptureRefused, CaptureRejected, capture  # noqa: E402
from vaws_knowledge_service import (  # noqa: E402
    already_promoted_slug,
    commons_entry,
    find_candidate_entry,
    infer_repo_root,
    service_config,
)
from vaws_knowledge_service import (  # noqa: E402
    COORDINATE_DIMENSIONS,
    KnowledgeError,
    knowledge_session_key,
    normalize_coordinate,
    unknown_coordinate_dimensions,
)

_MANIFEST_KEYS: dict[str, tuple[str, ...]] = {
    "soc": ("soc", "soc_version", "chip", "npu", "hardware_model"),
    "cann": ("cann", "cann_version"),
    "driver": ("driver", "driver_version", "firmware"),
    "python_abi": ("python_abi", "soabi", "python"),
    "torch": ("torch", "torch_version"),
    "torch_npu": ("torch_npu", "torch_npu_version"),
    "vllm": ("vllm", "vllm_version"),
    "vllm_ascend": ("vllm_ascend", "vllm_ascend_version"),
    "model": ("model", "model_name", "name", "served_model_name"),
    "topology": ("topology", "parallelism", "tp", "tensor_parallel_size"),
    "execution_mode": ("execution_mode", "mode", "graph_mode"),
    "component": ("component", "subsystem"),
}

_SCOPE_KEYS: dict[str, tuple[str, ...]] = {
    "soc": ("soc", "socs", "hardware"),
    "model": ("model", "models"),
    "topology": ("topology", "topologies", "parallelism"),
    "execution_mode": ("execution_mode", "execution_modes", "mode", "modes"),
    "component": ("component", "components", "subsystem"),
}

_PROGRESS = False


def emit_progress(phase: str, message: str) -> None:
    if _PROGRESS:
        print(f"[{phase}] {message}", file=sys.stderr, flush=True)


def _first_value(source: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        if key not in source:
            continue
        value = source[key]
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return str(value)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def coordinate_from_manifest(manifest: Mapping[str, Any]) -> dict[str, str]:
    scopes = [
        manifest.get("environment") if isinstance(manifest.get("environment"), Mapping) else {},
        manifest.get("model") if isinstance(manifest.get("model"), Mapping) else {},
        manifest.get("topology") if isinstance(manifest.get("topology"), Mapping) else {},
        manifest.get("workspace_snapshot")
        if isinstance(manifest.get("workspace_snapshot"), Mapping)
        else {},
    ]
    coordinate: dict[str, str] = {}
    for dimension, keys in _MANIFEST_KEYS.items():
        for scope in scopes:
            value = _first_value(scope, keys)
            if value is not None:
                coordinate[dimension] = value
                break
    topology = manifest.get("topology")
    if "topology" not in coordinate and isinstance(topology, Mapping):
        parts = []
        for key, prefix in (
            ("tensor_parallel_size", "tp"),
            ("tp", "tp"),
            ("data_parallel_size", "dp"),
            ("dp", "dp"),
            ("expert_parallel_size", "ep"),
            ("ep", "ep"),
        ):
            value = topology.get(key)
            if isinstance(value, int) and value > 0:
                parts.append(f"{prefix}{value}")
        if parts:
            coordinate["topology"] = ",".join(dict.fromkeys(parts))
    return coordinate


def coordinate_from_candidate_scope(payload: Mapping[str, Any]) -> dict[str, str]:
    scope = payload.get("scope")
    if not isinstance(scope, Mapping):
        return {}
    coordinate: dict[str, str] = {}
    for dimension, keys in _SCOPE_KEYS.items():
        for key in keys:
            value = scope.get(key)
            if isinstance(value, str) and value.strip():
                coordinate[dimension] = value.strip()
                break
            if isinstance(value, (list, tuple)):
                items = [str(item).strip() for item in value if str(item).strip()]
                if items:
                    coordinate[dimension] = ",".join(dict.fromkeys(items))
                    break
    return coordinate


def parse_env_pairs(pairs: list[str] | None) -> dict[str, str]:
    coordinate: dict[str, str] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise KnowledgeError(f"--env expects dimension=value, got: {pair!r}")
        key, value = pair.split("=", 1)
        key = key.strip()
        if key not in COORDINATE_DIMENSIONS:
            raise KnowledgeError(
                f"unknown coordinate dimension {key!r}; expected one of: "
                + ", ".join(COORDINATE_DIMENSIONS)
            )
        if value.strip():
            coordinate[key] = value.strip()
    return coordinate


def collect_coordinate(payload: dict[str, Any], args: argparse.Namespace) -> tuple[dict[str, str], str]:
    coordinate_source = "unavailable"
    collected: dict[str, str] = {}
    from_scope = coordinate_from_candidate_scope(payload)
    if from_scope:
        collected.update(from_scope)
        coordinate_source = "candidate-scope"
        emit_progress("coordinate", "candidate scope supplied: " + ", ".join(sorted(from_scope)))
    if isinstance(payload.get("environment"), Mapping):
        collected.update(
            {
                key: str(value)
                for key, value in payload["environment"].items()
                if key in COORDINATE_DIMENSIONS and isinstance(value, str) and value.strip()
            }
        )
        coordinate_source = "explicit"
    if args.run_manifest:
        manifest = json.loads(args.run_manifest.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict):
            raise KnowledgeError("run manifest root must be an object")
        from_manifest = coordinate_from_manifest(manifest)
        collected.update(from_manifest)
        coordinate_source = "run-manifest"
        emit_progress(
            "coordinate",
            f"run manifest supplied {len(from_manifest)}/{len(COORDINATE_DIMENSIONS)} dimensions",
        )
    explicit = parse_env_pairs(args.env_pairs)
    if explicit:
        collected.update(explicit)
        if coordinate_source == "unavailable":
            coordinate_source = "explicit"
    return collected, coordinate_source


def _title_and_content(payload: Mapping[str, Any]) -> tuple[str, str]:
    title = str(payload.get("title") or payload.get("summary") or "").strip()
    content = str(payload.get("content") or "").strip()
    if not content:
        parts = [
            str(payload[key]).strip()
            for key in ("symptom", "root_cause", "resolution", "avoidance")
            if str(payload.get(key) or "").strip()
        ]
        content = "\n\n".join(parts)
    return title, content


def write_commons(
    payload: Mapping[str, Any],
    *,
    knowledge_dir: Path,
    candidate_root: Path,
) -> dict[str, Any]:
    repo_root = infer_repo_root(knowledge_dir.resolve(), knowledge_dir.resolve().parent)
    title, content = _title_and_content(payload)
    config = service_config(
        repo_root,
        project_root=knowledge_dir.resolve(),
        candidate_root=candidate_root,
    )
    conditions = payload.get("conditions")
    if not isinstance(conditions, Mapping):
        conditions = payload.get("environment") if isinstance(payload.get("environment"), Mapping) else None
    return capture(
        title=title,
        content=content,
        source=payload.get("source") if isinstance(payload.get("source"), Mapping) else None,
        conditions=conditions,
        evidence=payload.get("evidence"),
        config=config,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path)
    parser.add_argument("--title", default="")
    parser.add_argument("--content", default="")
    parser.add_argument(
        "--defer",
        action="store_true",
        help="write the same candidate-layer yaml now; keep a session id on the receipt",
    )
    parser.add_argument("--session-id")
    parser.add_argument("--run-manifest", type=Path)
    parser.add_argument("--env", action="append", dest="env_pairs")
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "candidate",
        help="commons candidate-layer yaml root",
    )
    parser.add_argument(
        "--pending-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "pending",
        help="unused; leftover JSON pending is still flushed by the session-end hook",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    parser.add_argument("--progress", action="store_true")
    args = parser.parse_args(argv)
    global _PROGRESS
    _PROGRESS = bool(args.progress)
    try:
        if args.input is None and not (args.title and args.content):
            raise KnowledgeError("provide --title and --content, or --input JSON")
        if args.input is not None:
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise KnowledgeError("input root must be an object")
        else:
            payload = {"title": args.title, "content": args.content}
        if args.title:
            payload["title"] = args.title
        if args.content:
            payload["content"] = args.content

        collected, coordinate_source = collect_coordinate(payload, args)
        known = {
            name: value
            for name, value in collected.items()
            if str(value).strip() and str(value).strip().lower() != "unknown"
        }
        payload["environment"] = known
        unknown = [name for name in COORDINATE_DIMENSIONS if name not in known]
        if unknown:
            emit_progress("coordinate", "omitted unknown dimensions: " + ", ".join(unknown))

        redaction.require_writable(payload, path="payload")
        export_hits = [
            finding.to_dict()
            for finding in redaction.export_findings(redaction.scan(payload, path="payload"))
        ]

        session_id = None
        if args.defer:
            source = payload.get("source")
            if source is not None and not isinstance(source, dict):
                raise KnowledgeError("source must be an object")
            source = payload.setdefault("source", {})
            session_id = (
                args.session_id
                or os.environ.get("CODEX_THREAD_ID")
                or source.get("session_id")
            )
            if not session_id:
                raise KnowledgeError(
                    "--defer requires --session-id, CODEX_THREAD_ID, or source.session_id"
                )
            source["session_id"] = session_id

        try:
            promoted = already_promoted_slug(payload, args.knowledge_dir.resolve())
        except Exception:  # noqa: BLE001 - promotion check is optional
            promoted = None
        if promoted:
            result = {
                "status": "already-promoted",
                "candidate_id": promoted,
                "entry_id": promoted,
                "path": None,
            }
        else:
            written = write_commons(
                payload,
                knowledge_dir=args.knowledge_dir,
                candidate_root=args.candidate_dir.resolve(),
            )
            slug = str(written.get("slug") or written.get("uri"))
            result = {
                "status": "passed",
                "candidate_id": slug,
                "slug": written.get("slug"),
                "uri": written.get("uri"),
                "ref": written.get("ref") or written.get("uri"),
                "path": written.get("path") or written.get("file"),
                "commons": written,
            }
        result["coordinate"] = {
            "source": coordinate_source,
            "values": dict(payload.get("environment") or {}),
            "unknown_dimensions": unknown,
            "complete": not unknown,
        }
        result["redaction"] = {
            "level": "export" if export_hits else "clear",
            "findings": export_hits,
        }
        if args.defer and result["status"] != "already-promoted":
            result["deferred"] = True
            result["session_key"] = knowledge_session_key(str(session_id))
    except redaction.RedactionError as exc:
        print(json.dumps({"status": "failed", "error": str(exc), "redaction": {"level": "block"}}))
        return 1
    except (CaptureRefused, CaptureRejected) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    except (OSError, json.JSONDecodeError, KnowledgeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
