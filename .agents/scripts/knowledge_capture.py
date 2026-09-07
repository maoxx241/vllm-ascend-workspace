#!/usr/bin/env python3
"""Capture or merge one verified workspace knowledge candidate.

Applicability is collected as a coordinate, not as prose. A capture reads the
concrete environment that produced the fix — ``soc``, ``cann``, ``driver``,
``python_abi``, ``torch``, ``torch_npu``, ``vllm``, ``vllm_ascend``, plus
``model`` / ``topology`` / ``execution_mode`` / ``component`` where relevant —
from the Run Manifest of the run, from an explicit ``--env`` pair, or from the
candidate payload. A value that is genuinely unavailable is recorded as
``unknown``; it is never invented, and the result payload lists every unknown
dimension so promotion cannot quietly claim a coordinate nobody established.

The v1 invocation (``--input`` with ``applicable_versions`` in the payload)
keeps working: the payload is written forward as a schema 2 candidate whose
coordinate is all-``unknown`` when no environment is available.
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

from vaws_knowledge import (  # noqa: E402
    COORDINATE_DIMENSIONS,
    COORDINATE_UNKNOWN,
    KnowledgeError,
    capture_candidate,
    knowledge_session_key,
    normalize_coordinate,
)

# Manifest keys that carry each coordinate dimension. Run Manifest v1 leaves
# ``environment`` / ``model`` / ``topology`` as free-form objects, so several
# spellings are accepted rather than requiring producers to change first.
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


_PROGRESS = False


def emit_progress(phase: str, message: str) -> None:
    """Phase progress on stderr, opt-in.

    Capture is called from other skills' wrappers that treat any stderr byte
    as a fault, so progress stays behind ``--progress``. The same information
    is always in the stdout payload under ``coordinate``.
    """

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
    """Read the coordinate out of a Run Manifest v1 document.

    Only values actually present are read. Everything else stays ``unknown``:
    a manifest that did not record CANN is evidence that CANN is unknown, not
    licence to fill in a plausible one.
    """

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


# Candidate ``scope`` keys that already name a coordinate dimension. Reading
# them is not inference: the capturing agent stated them about this very
# claim, and dropping them would force a human to retype what is already
# recorded.
_SCOPE_KEYS: dict[str, tuple[str, ...]] = {
    "soc": ("soc", "socs", "hardware"),
    "model": ("model", "models"),
    "topology": ("topology", "topologies", "parallelism"),
    "execution_mode": ("execution_mode", "execution_modes", "mode", "modes"),
    "component": ("component", "components", "subsystem"),
}


def coordinate_from_candidate_scope(payload: Mapping[str, Any]) -> dict[str, str]:
    """Read the coordinate dimensions the candidate's own ``scope`` names."""

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument(
        "--defer",
        action="store_true",
        help="stage the candidate for the current SessionEnd hook",
    )
    parser.add_argument(
        "--session-id",
        help="session identity for --defer; defaults to CODEX_THREAD_ID or source.session_id",
    )
    parser.add_argument(
        "--run-manifest",
        type=Path,
        help="Run Manifest v1 of the run that produced the fix; read for the coordinate",
    )
    parser.add_argument(
        "--env",
        action="append",
        dest="env_pairs",
        help="explicit coordinate value, e.g. --env cann=8.2.RC1; repeatable",
    )
    parser.add_argument(
        "--candidate-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "candidates",
    )
    parser.add_argument(
        "--pending-dir",
        type=Path,
        default=ROOT / ".vaws-local" / "knowledge" / "pending",
    )
    parser.add_argument(
        "--knowledge-dir",
        type=Path,
        default=ROOT / ".agents" / "knowledge",
    )
    parser.add_argument(
        "--progress",
        action="store_true",
        help="stream coordinate-collection progress on stderr",
    )
    args = parser.parse_args(argv)
    global _PROGRESS
    _PROGRESS = bool(args.progress)
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise KnowledgeError("input root must be an object")

        coordinate_source = "unavailable"
        collected: dict[str, str] = {}
        from_scope = coordinate_from_candidate_scope(payload)
        if from_scope:
            collected.update(from_scope)
            coordinate_source = "candidate-scope"
            emit_progress(
                "coordinate",
                "candidate scope supplied: " + ", ".join(sorted(from_scope)),
            )
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
        payload["environment"] = normalize_coordinate(collected, source=coordinate_source)
        unknown = [
            name
            for name in COORDINATE_DIMENSIONS
            if payload["environment"][name] == COORDINATE_UNKNOWN
        ]
        if unknown:
            emit_progress(
                "coordinate",
                "recorded as unknown (never guessed): " + ", ".join(unknown),
            )

        candidate_dir = args.candidate_dir
        if args.defer:
            source = payload.setdefault("source", {})
            if not isinstance(source, dict):
                raise KnowledgeError("source must be an object")
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
            candidate_dir = args.pending_dir / knowledge_session_key(session_id)
        result = capture_candidate(
            payload,
            candidate_dir=candidate_dir,
            knowledge_dir=args.knowledge_dir,
        )
        if args.defer and result["status"] != "already-promoted":
            result["deferred"] = True
            result["session_key"] = knowledge_session_key(source["session_id"])
    except (OSError, json.JSONDecodeError, KnowledgeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
