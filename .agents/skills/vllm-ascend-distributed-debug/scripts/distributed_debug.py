#!/usr/bin/env python3
"""Create and analyze structured vLLM Ascend distributed-debug cases."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[4]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from vaws_run_manifest import (  # noqa: E402
    RunManifestError,
    add_artifact,
    load_manifest,
    new_manifest,
    transition_status,
    write_manifest,
)

SCHEMA_VERSION = 1
REQUIRED_RANK_FIELDS = (
    "global_rank",
    "node",
    "device",
    "local_rank",
    "tp_rank",
    "pp_rank",
    "dp_rank",
    "ep_rank",
    "pcp_rank",
    "dcp_rank",
)
COLLECTIVE_EVENTS = {"collective_enter", "collective_exit"}


class DistributedDebugError(ValueError):
    """Raised when case evidence violates the distributed-debug contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write(
        path, json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    )


def _load_json(path: Path, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DistributedDebugError(f"cannot read {label} {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise DistributedDebugError(f"{label} root must be an object")
    return payload


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise DistributedDebugError(f"cannot read events {path}: {exc}") from exc
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise DistributedDebugError(
                f"events line {number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(row, dict):
            raise DistributedDebugError(f"events line {number} must be an object")
        rows.append(row)
    return rows


def validate_config(config: Mapping[str, Any]) -> None:
    errors: list[str] = []
    if config.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(config.get("run_id"), str) or not config["run_id"]:
        errors.append("run_id must be a non-empty string")
    world_size = config.get("expected_world_size")
    if not isinstance(world_size, int) or isinstance(world_size, bool) or world_size < 1:
        errors.append("expected_world_size must be a positive integer")
    ranks = config.get("ranks")
    seen: set[int] = set()
    if not isinstance(ranks, list) or not ranks:
        errors.append("ranks must be a non-empty array")
        ranks = []
    for index, rank in enumerate(ranks):
        if not isinstance(rank, Mapping):
            errors.append(f"ranks[{index}] must be an object")
            continue
        for field in REQUIRED_RANK_FIELDS:
            value = rank.get(field)
            if field == "node":
                if not isinstance(value, str) or not value:
                    errors.append(f"ranks[{index}].node must be a non-empty string")
            elif not isinstance(value, int) or isinstance(value, bool) or value < 0:
                errors.append(f"ranks[{index}].{field} must be a non-negative integer")
        global_rank = rank.get("global_rank")
        if isinstance(global_rank, int):
            if global_rank in seen:
                errors.append(f"global_rank is duplicated: {global_rank}")
            seen.add(global_rank)
    if isinstance(world_size, int) and len(ranks) != world_size:
        errors.append(
            f"rank count {len(ranks)} does not match expected_world_size {world_size}"
        )
    if isinstance(world_size, int) and seen != set(range(world_size)):
        errors.append("global ranks must be contiguous from 0 to expected_world_size-1")
    groups = config.get("groups", [])
    if not isinstance(groups, list):
        errors.append("groups must be an array")
        groups = []
    group_names: set[str] = set()
    for index, group in enumerate(groups):
        if not isinstance(group, Mapping):
            errors.append(f"groups[{index}] must be an object")
            continue
        name = group.get("name")
        members = group.get("ranks")
        if not isinstance(name, str) or not name:
            errors.append(f"groups[{index}].name must be a non-empty string")
        elif name in group_names:
            errors.append(f"group name is duplicated: {name}")
        else:
            group_names.add(name)
        if (
            not isinstance(members, list)
            or not members
            or any(
                not isinstance(rank, int) or isinstance(rank, bool) for rank in members
            )
        ):
            errors.append(f"groups[{index}].ranks must be a non-empty integer array")
        elif len(set(members)) != len(members):
            errors.append(f"groups[{index}].ranks contains duplicates")
        elif not set(members).issubset(seen):
            errors.append(f"groups[{index}].ranks contains unknown ranks")
    endpoints = config.get("network_endpoints", [])
    if not isinstance(endpoints, list):
        errors.append("network_endpoints must be an array")
        endpoints = []
    endpoint_keys: set[tuple[str, int]] = set()
    for index, endpoint in enumerate(endpoints):
        if not isinstance(endpoint, Mapping):
            errors.append(f"network_endpoints[{index}] must be an object")
            continue
        address = endpoint.get("address")
        port = endpoint.get("port")
        if not isinstance(address, str) or not address:
            errors.append(
                f"network_endpoints[{index}].address must be a non-empty string"
            )
        if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
            errors.append(f"network_endpoints[{index}].port must be 1..65535")
        if isinstance(address, str) and isinstance(port, int):
            key = (address, port)
            if key in endpoint_keys:
                errors.append(f"network endpoint is duplicated: {address}:{port}")
            endpoint_keys.add(key)
    if errors:
        raise DistributedDebugError("; ".join(errors))


def validate_event(event: Mapping[str, Any], known_ranks: set[int]) -> None:
    errors: list[str] = []
    rank = event.get("rank")
    if not isinstance(rank, int) or isinstance(rank, bool) or rank not in known_ranks:
        errors.append("rank must identify a topology rank")
    if not isinstance(event.get("timestamp"), str) or not event["timestamp"]:
        errors.append("timestamp must be a non-empty string")
    if not isinstance(event.get("phase"), str) or not event["phase"]:
        errors.append("phase must be a non-empty string")
    kind = event.get("event")
    if not isinstance(kind, str) or not kind:
        errors.append("event must be a non-empty string")
    if kind in COLLECTIVE_EVENTS:
        if not isinstance(event.get("group"), str) or not event["group"]:
            errors.append("collective event requires group")
        if (
            not isinstance(event.get("sequence"), int)
            or isinstance(event.get("sequence"), bool)
            or event["sequence"] < 0
        ):
            errors.append("collective event requires a non-negative sequence")
        if not isinstance(event.get("operation"), str) or not event["operation"]:
            errors.append("collective event requires operation")
    if errors:
        raise DistributedDebugError("; ".join(errors))


def init_case(
    output_dir: Path, *, config_path: Path, created_at: str | None = None
) -> dict[str, Any]:
    if output_dir.exists() and any(output_dir.iterdir()):
        raise DistributedDebugError(f"output directory is not empty: {output_dir}")
    config = _load_json(config_path, "case config")
    validate_config(config)
    timestamp = created_at or utc_now()
    output_dir.mkdir(parents=True, exist_ok=True)
    for directory in ("rank-logs", "stack-dumps", "metadata-samples"):
        (output_dir / directory).mkdir()
    topology = {
        "schema_version": SCHEMA_VERSION,
        "expected_world_size": config["expected_world_size"],
        "ranks": config["ranks"],
        "groups": config.get("groups", []),
    }
    _write_json(output_dir / "case-config.json", config)
    _write_json(output_dir / "topology.json", topology)
    _write_json(output_dir / "environment.json", config.get("environment", {}))
    _write_json(output_dir / "process-tree.json", config.get("process_tree", {}))
    _write_json(
        output_dir / "network-endpoints.json", config.get("network_endpoints", [])
    )
    _atomic_write(output_dir / "events.jsonl", "")
    _atomic_write(
        output_dir / "reproduction.md",
        "# Distributed failure reproduction\n\n"
        f"Command: `{' '.join(config.get('command', []))}`\n\n"
        "Record the original topology before reducing any parallel dimension.\n",
    )
    manifest = new_manifest(
        run_type="debug",
        run_id=config["run_id"],
        workspace_snapshot=config.get("workspace_snapshot", {}),
        environment=config.get("environment", {}),
        model=config.get("model", {}),
        topology=topology,
        command=config.get("command", []),
        created_at=timestamp,
    )
    for name, kind, uri in (
        ("topology", "topology", "topology.json"),
        ("environment", "environment", "environment.json"),
        ("process-tree", "process-tree", "process-tree.json"),
        ("network-endpoints", "network-endpoints", "network-endpoints.json"),
        ("events", "rank-events", "events.jsonl"),
        ("reproduction", "reproduction", "reproduction.md"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "planned",
        "run_id": config["run_id"],
        "world_size": config["expected_world_size"],
        "output_dir": str(output_dir.resolve()),
    }


def ingest_events(
    output_dir: Path, *, events_path: Path, updated_at: str | None = None
) -> dict[str, Any]:
    topology = _load_json(output_dir / "topology.json", "topology")
    known_ranks = {rank["global_rank"] for rank in topology["ranks"]}
    incoming = _load_jsonl(events_path)
    for event in incoming:
        validate_event(event, known_ranks)
    existing = _load_jsonl(output_dir / "events.jsonl")
    combined = existing + incoming
    _atomic_write(
        output_dir / "events.jsonl",
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in combined
        ),
    )
    timestamp = updated_at or utc_now()
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
        write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": "ingested",
        "added": len(incoming),
        "total": len(combined),
    }


def _finding(
    code: str, severity: str, summary: str, **evidence: Any
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "summary": summary,
        "evidence": evidence,
    }


def analyze_evidence(
    topology: Mapping[str, Any],
    endpoints: Iterable[Mapping[str, Any]],
    events: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    ranks = {rank["global_rank"] for rank in topology["ranks"]}
    groups = {group["name"]: set(group["ranks"]) for group in topology["groups"]}
    events_by_rank: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    findings: list[dict[str, Any]] = []
    unknown_groups: set[str] = set()
    collective_rows: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        rank = event["rank"]
        events_by_rank[rank].append(event)
        if event["event"] in COLLECTIVE_EVENTS:
            group = event["group"]
            if group not in groups:
                unknown_groups.add(group)
            collective_rows[(group, event["sequence"])].append(event)
    missing_ranks = sorted(ranks - set(events_by_rank))
    if missing_ranks:
        findings.append(
            _finding(
                "missing-rank-evidence",
                "incomplete",
                "No normalized events were captured for some ranks.",
                ranks=missing_ranks,
            )
        )
    if unknown_groups:
        findings.append(
            _finding(
                "unknown-process-group",
                "confirmed",
                "Collective events reference groups absent from topology.",
                groups=sorted(unknown_groups),
            )
        )
    for (group, sequence), rows in sorted(collective_rows.items()):
        if group not in groups:
            continue
        expected = groups[group]
        operations = sorted({row["operation"] for row in rows})
        entered = {
            row["rank"] for row in rows if row["event"] == "collective_enter"
        }
        exited = {row["rank"] for row in rows if row["event"] == "collective_exit"}
        if len(operations) > 1:
            findings.append(
                _finding(
                    "collective-operation-mismatch",
                    "confirmed",
                    "Ranks disagree on the operation at one collective sequence.",
                    group=group,
                    sequence=sequence,
                    operations=operations,
                )
            )
        if entered != expected:
            findings.append(
                _finding(
                    "collective-participant-mismatch",
                    "confirmed",
                    "The collective entry set does not match group membership.",
                    group=group,
                    sequence=sequence,
                    expected=sorted(expected),
                    entered=sorted(entered),
                    missing=sorted(expected - entered),
                    unexpected=sorted(entered - expected),
                )
            )
        stalled = sorted(entered - exited)
        if stalled:
            findings.append(
                _finding(
                    "collective-enter-without-exit",
                    "candidate",
                    "Ranks entered a collective without a matching exit event.",
                    group=group,
                    sequence=sequence,
                    ranks=stalled,
                )
            )
    endpoint_keys: dict[tuple[str, int], list[Mapping[str, Any]]] = defaultdict(list)
    for endpoint in endpoints:
        endpoint_keys[(endpoint["address"], endpoint["port"])].append(endpoint)
    for (address, port), rows in endpoint_keys.items():
        if len(rows) > 1:
            findings.append(
                _finding(
                    "endpoint-collision",
                    "confirmed",
                    "Multiple declared endpoints bind the same address and port.",
                    address=address,
                    port=port,
                    endpoints=rows,
                )
            )
    last_progress = {
        str(rank): {
            "timestamp": rows[-1]["timestamp"],
            "phase": rows[-1]["phase"],
            "event": rows[-1]["event"],
        }
        for rank, rows in sorted(events_by_rank.items())
    }
    last_phases = {row["phase"] for row in last_progress.values()}
    if len(last_phases) > 1:
        findings.append(
            _finding(
                "rank-phase-divergence",
                "candidate",
                "Ranks stopped in different phases.",
                phases={
                    phase: sorted(
                        int(rank)
                        for rank, row in last_progress.items()
                        if row["phase"] == phase
                    )
                    for phase in sorted(last_phases)
                },
            )
        )
    confirmed = [row["code"] for row in findings if row["severity"] == "confirmed"]
    incomplete = [row["code"] for row in findings if row["severity"] == "incomplete"]
    if confirmed:
        status = "diagnosed"
    elif incomplete or not events_by_rank:
        status = "inconclusive"
    elif findings:
        status = "hypothesis"
    else:
        status = "no-mismatch-detected"
    return {
        "schema_version": SCHEMA_VERSION,
        "status": status,
        "world_size": len(ranks),
        "event_count": sum(len(rows) for rows in events_by_rank.values()),
        "last_progress": last_progress,
        "findings": findings,
        "confirmed_findings": confirmed,
        "evidence_gaps": incomplete,
    }


def render_report(analysis: Mapping[str, Any]) -> str:
    lines = [
        "# Distributed debug report",
        "",
        f"- Status: **{analysis['status']}**",
        f"- World size: `{analysis['world_size']}`",
        f"- Normalized events: `{analysis['event_count']}`",
        "",
        "## Findings",
        "",
    ]
    if not analysis["findings"]:
        lines.append("No structured rank, group, endpoint, or collective mismatch was detected.")
    for row in analysis["findings"]:
        lines.extend(
            [
                f"### `{row['code']}` ({row['severity']})",
                "",
                row["summary"],
                "",
                "```json",
                json.dumps(row["evidence"], ensure_ascii=False, indent=2, sort_keys=True),
                "```",
                "",
            ]
        )
    lines.extend(["## Last progress by rank", "", "```json"])
    lines.append(
        json.dumps(analysis["last_progress"], ensure_ascii=False, indent=2, sort_keys=True)
    )
    lines.extend(["```", ""])
    return "\n".join(lines)


def analyze_case(
    output_dir: Path, *, updated_at: str | None = None
) -> dict[str, Any]:
    topology = _load_json(output_dir / "topology.json", "topology")
    endpoints_payload = json.loads(
        (output_dir / "network-endpoints.json").read_text(encoding="utf-8")
    )
    if not isinstance(endpoints_payload, list):
        raise DistributedDebugError("network-endpoints root must be an array")
    events = _load_jsonl(output_dir / "events.jsonl")
    analysis = analyze_evidence(topology, endpoints_payload, events)
    _write_json(output_dir / "analysis.json", analysis)
    _atomic_write(output_dir / "report.md", render_report(analysis))
    timestamp = updated_at or utc_now()
    manifest = load_manifest(output_dir / "manifest.json")
    if manifest["status"] == "planned":
        manifest = transition_status(manifest, "running", updated_at=timestamp)
    for name, kind, uri in (
        ("analysis", "analysis", "analysis.json"),
        ("report", "report", "report.md"),
    ):
        manifest = add_artifact(
            manifest, name=name, kind=kind, uri=uri, updated_at=timestamp
        )
    terminal = "failed" if analysis["status"] == "diagnosed" else "inconclusive"
    manifest = transition_status(manifest, terminal, updated_at=timestamp)
    write_manifest(output_dir / "manifest.json", manifest)
    return {
        "status": analysis["status"],
        "confirmed_findings": analysis["confirmed_findings"],
        "evidence_gaps": analysis["evidence_gaps"],
        "analysis": str((output_dir / "analysis.json").resolve()),
        "report": str((output_dir / "report.md").resolve()),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    init_parser = subparsers.add_parser("init", help="create a debug case")
    init_parser.add_argument("--output-dir", required=True, type=Path)
    init_parser.add_argument("--config", required=True, type=Path)
    ingest_parser = subparsers.add_parser("ingest", help="append normalized events")
    ingest_parser.add_argument("--output-dir", required=True, type=Path)
    ingest_parser.add_argument("--events", required=True, type=Path)
    analyze_parser = subparsers.add_parser("analyze", help="analyze case evidence")
    analyze_parser.add_argument("--output-dir", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.action == "init":
            payload = init_case(args.output_dir, config_path=args.config)
        elif args.action == "ingest":
            payload = ingest_events(args.output_dir, events_path=args.events)
        else:
            payload = analyze_case(args.output_dir)
    except (DistributedDebugError, RunManifestError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
