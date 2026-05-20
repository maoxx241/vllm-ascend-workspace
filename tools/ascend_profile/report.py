#!/usr/bin/env python3
"""Render Markdown/XLSX report packages from analysis artifacts."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .common import (
        SCHEMA_VERSION,
        TOOL_VERSION,
        csv_rows,
        read_json,
        read_jsonl,
        stable_id,
        utc_now,
        write_json,
        write_xlsx,
    )
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import (  # type: ignore[no-redef]
        SCHEMA_VERSION,
        TOOL_VERSION,
        csv_rows,
        read_json,
        read_jsonl,
        stable_id,
        utc_now,
        write_json,
        write_xlsx,
    )


def parse_jsonish(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else default
    text = value.strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def finding_rows(output_dir: Path) -> list[dict[str, Any]]:
    payload = read_json(output_dir / "diagnosis_findings.json", default={})
    rows = payload.get("diagnosis_findings", [])
    return rows if isinstance(rows, list) else []


REPORT_TOP_FINDING_LIMIT = 24


def top_findings(findings: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    return sorted(findings, key=lambda item: (severity_order.get(str(item.get("severity")), 9), str(item.get("finding_type"))))[
        :REPORT_TOP_FINDING_LIMIT
    ]


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _quantile(values: Sequence[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * q))))
    return ordered[idx]


def macro_timeline_lines(step_rows: Sequence[Mapping[str, Any]], anatomy_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Build the Macro Step Timeline section.

    Two tables:
      * per-rank rollup of step counts and wall/head/main/tail/bubble means;
      * top heaviest steps with anatomy ratios so the reader can jump
        directly to the worst offenders' row ranges.
    """

    by_rank: dict[str, list[Mapping[str, Any]]] = {}
    for row in step_rows:
        if row.get("segment_type") != "step":
            continue
        by_rank.setdefault(str(row.get("rank_id") or ""), []).append(row)

    anatomy_by_segment = {str(item.get("segment_id")): item for item in anatomy_rows}

    lines: list[str] = [
        "| Rank | Steps | Wall p50 ms | Wall p90 ms | Wall p99 ms | Head% | Main% | Tail% | Bubble% |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank_id in sorted(by_rank):
        items = by_rank[rank_id]
        wall = [_f(item.get("wall_ms")) for item in items]
        head_ratio: list[float] = []
        main_ratio: list[float] = []
        tail_ratio: list[float] = []
        bubble_ratio: list[float] = []
        for item in items:
            anatomy = anatomy_by_segment.get(str(item.get("segment_id")))
            if anatomy is None:
                continue
            head_ratio.append(_f(anatomy.get("head_ratio")))
            main_ratio.append(_f(anatomy.get("main_ratio")))
            tail_ratio.append(_f(anatomy.get("tail_ratio")))
            bubble_ratio.append(_f(anatomy.get("bubble_ratio")))

        def _avg(values: list[float]) -> float:
            return (sum(values) / len(values)) if values else 0.0

        lines.append(
            f"| `{rank_id}` | {len(items)} | "
            f"{_quantile(wall, 0.5):.3f} | {_quantile(wall, 0.9):.3f} | {_quantile(wall, 0.99):.3f} | "
            f"{_avg(head_ratio) * 100:.2f} | {_avg(main_ratio) * 100:.2f} | "
            f"{_avg(tail_ratio) * 100:.2f} | {_avg(bubble_ratio) * 100:.2f} |"
        )

    lines.extend(
        [
            "",
            "Top 8 heaviest steps (wall_ms desc):",
            "",
            "| Segment | Rank | Family | Layers | Wall ms | Head ms | Main ms | Tail ms | Bubble ms | Bubble% |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )

    sorted_steps = sorted(
        (
            row
            for row in step_rows
            if row.get("segment_type") == "step"
        ),
        key=lambda item: _f(item.get("wall_ms")),
        reverse=True,
    )[:8]
    for row in sorted_steps:
        anatomy = anatomy_by_segment.get(str(row.get("segment_id"))) or {}
        lines.append(
            f"| `{row.get('segment_id')}` | `{row.get('rank_id')}` | "
            f"`{row.get('step_family')}` | {row.get('main_layer_count')} | "
            f"{_f(row.get('wall_ms')):.3f} | {_f(anatomy.get('head_wall_ms')):.3f} | "
            f"{_f(anatomy.get('main_wall_ms')):.3f} | {_f(anatomy.get('tail_wall_ms')):.3f} | "
            f"{_f(row.get('underfeed_ms')):.3f} | {_f(anatomy.get('bubble_ratio')) * 100:.2f} |"
        )
    if not sorted_steps:
        lines.append("| — | — | — | — | 0 | 0 | 0 | 0 | 0 | 0 |")
    return lines


def step_class_view_lines(
    step_class_rows: Sequence[Mapping[str, Any]],
    layer_class_rows: Sequence[Mapping[str, Any]],
    *,
    top_n: int = 8,
) -> list[str]:
    """Render the per-step-class summary tables.

    Two tables:
      1. top step classes by ``member_count`` × ``wall_ms_mean`` (= total
         time spent in this class), with head / main / tail / bubble
         ratios so the reader can attribute time to anatomy windows.
      2. for the heaviest class only, the top layer classes inside it
         (``stp_cls_x -> lyr_cls_y``) so the report walks naturally
         from "which step class is heaviest" into "which layer drives it".
    """

    layer_class_by_id = {str(row.get("layer_class_id")): row for row in layer_class_rows}

    if not step_class_rows:
        return [
            "_No step classes were emitted (shape data missing or no completed steps)._",
        ]

    enriched = []
    for row in step_class_rows:
        member = int(row.get("member_count") or 0)
        mean = _f(row.get("wall_ms_mean"))
        enriched.append((member * mean, row))
    enriched.sort(key=lambda item: -item[0])
    top_classes = [row for _, row in enriched[:top_n]]

    lines: list[str] = [
        "Top step classes by total wall-time contribution (members × wall mean):",
        "",
        "| Step class | Family | Layers | Members | Wall mean ms | p50 ms | p90 ms | Head% | Main% | Tail% | Bubble% | Unknown shape |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for row in top_classes:
        unknown = "yes" if str(row.get("has_unknown_shape", "")).lower() in {"true", "1"} else ""
        lines.append(
            f"| `{row.get('step_class_id')}` | `{row.get('step_family')}` | "
            f"{row.get('main_layer_count')} | {row.get('member_count')} | "
            f"{_f(row.get('wall_ms_mean')):.3f} | {_f(row.get('wall_ms_p50')):.3f} | "
            f"{_f(row.get('wall_ms_p90')):.3f} | "
            f"{_f(row.get('head_ratio_mean')) * 100:.2f} | "
            f"{_f(row.get('main_ratio_mean')) * 100:.2f} | "
            f"{_f(row.get('tail_ratio_mean')) * 100:.2f} | "
            f"{_f(row.get('bubble_ratio_mean')) * 100:.2f} | {unknown} |"
        )

    if top_classes:
        heaviest = top_classes[0]
        top_layer_classes = parse_jsonish(heaviest.get("top_layer_classes"), [])
        lines.extend(
            [
                "",
                f"Top layer classes inside heaviest step class `{heaviest.get('step_class_id')}`:",
                "",
                "| Layer class | Wall ms sum | Members | Block kinds | Companion |",
                "|---|---:|---:|---|:---:|",
            ]
        )
        for entry in (top_layer_classes or [])[:8]:
            lc = layer_class_by_id.get(str(entry.get("layer_class_id"))) or {}
            kinds = parse_jsonish(lc.get("block_kinds"), [])
            companion = "yes" if str(lc.get("companion_layer", "")).lower() in {"true", "1"} else ""
            lines.append(
                f"| `{entry.get('layer_class_id')}` | {_f(entry.get('wall_ms_sum')):.3f} | "
                f"{entry.get('member_count')} | "
                f"`{'->'.join(str(item) for item in (kinds or []))}` | {companion} |"
            )
        if not top_layer_classes:
            lines.append("| _none_ | 0 | 0 | _empty_ | |")

        top_ops = parse_jsonish(heaviest.get("top_ops"), [])
        lines.extend(
            [
                "",
                f"Top operators inside heaviest step class `{heaviest.get('step_class_id')}` "
                "(aggregated across member blocks):",
                "",
                "| Operator | Task | Σ duration ms | Calls |",
                "|---|---|---:|---:|",
            ]
        )
        for op in (top_ops or [])[:8]:
            lines.append(
                f"| `{op.get('name')}` | `{op.get('task_type')}` | "
                f"{_f(op.get('duration_sum_us')) / 1000.0:.3f} | {op.get('call_count')} |"
            )
        if not top_ops:
            lines.append("| _none_ | — | 0 | 0 |")
    return lines


def layer_block_view_lines(
    layer_class_rows: Sequence[Mapping[str, Any]],
    block_class_rows: Sequence[Mapping[str, Any]],
    *,
    top_layer: int = 8,
    top_block: int = 12,
) -> list[str]:
    """Render the per-layer / per-block class summary tables.

    The table layout intentionally mirrors the user request: each layer
    class shows its block sequence (e.g. ``attention -> moe``), the
    wall-time share consumed by each block kind, and a companion-layer
    flag.  Block classes are then listed grouped by kind so the report
    can answer "which attention class is the cube-bound one?" without
    diving into the CSV.
    """

    if not layer_class_rows and not block_class_rows:
        return [
            "_No layer/block classes were emitted (block decomposition skipped)._",
        ]

    lines: list[str] = []

    if layer_class_rows:
        sorted_layers = sorted(
            layer_class_rows,
            key=lambda row: -(_f(row.get("wall_ms_mean")) * float(row.get("member_count") or 0)),
        )[:top_layer]
        lines.extend(
            [
                "Top layer classes by total wall-time (members × wall mean):",
                "",
                "| Layer class | Members | Block kinds | Companion | Wall mean ms | Wall p50 ms | Block-kind share |",
                "|---|---:|---|:---:|---:|---:|---|",
            ]
        )
        for row in sorted_layers:
            kinds = parse_jsonish(row.get("block_kinds"), [])
            companion = "yes" if str(row.get("companion_layer", "")).lower() in {"true", "1"} else ""
            shares = parse_jsonish(row.get("block_kind_wall_ms_share_mean"), {})
            shares_text = " / ".join(
                f"{kind}={share * 100:.1f}%"
                for kind, share in (shares or {}).items()
            ) or "_none_"
            lines.append(
                f"| `{row.get('layer_class_id')}` | {row.get('member_count')} | "
                f"`{'->'.join(str(item) for item in (kinds or []))}` | {companion} | "
                f"{_f(row.get('wall_ms_mean')):.3f} | {_f(row.get('wall_ms_p50')):.3f} | "
                f"{shares_text} |"
            )
    else:
        lines.append("_No layer classes (no shape-bearing layers detected)._")

    if block_class_rows:
        # Group by block_kind so the table stays readable for each kind.
        by_kind: dict[str, list[Mapping[str, Any]]] = {}
        for row in block_class_rows:
            by_kind.setdefault(str(row.get("block_kind") or "other"), []).append(row)
        kind_order = ("attention", "ffn", "moe", "aicpu", "other")

        lines.extend(
            [
                "",
                "Top block classes by total wall-time, grouped by block_kind. "
                "`bound_family` is computed on the aggregated AIC/AIV pipeline -- the "
                "`comm_share` column shows the fraction of wall time spent in HCCL "
                "communication (or `mix_comm_aiv` fused kernels) so consumers can "
                "swap lenses between compute and comms.",
                "",
                "| Kind | Block class | Companion | Members | Wall mean ms | Wall p50 ms | Bound family | Core | Comm share |",
                "|---|---|:---:|---:|---:|---:|---|---|---:|",
            ]
        )
        ordered_kinds = list(kind_order) + [k for k in by_kind if k not in kind_order]
        for kind in ordered_kinds:
            members = by_kind.get(kind) or []
            members.sort(
                key=lambda row: -(_f(row.get("wall_ms_mean")) * float(row.get("member_count") or 0)),
            )
            for row in members[:top_block]:
                companion = "yes" if str(row.get("companion_layer", "")).lower() in {"true", "1"} else ""
                lines.append(
                    f"| `{kind}` | `{row.get('block_class_id')}` | {companion} | "
                    f"{row.get('member_count')} | {_f(row.get('wall_ms_mean')):.3f} | "
                    f"{_f(row.get('wall_ms_p50')):.3f} | "
                    f"`{row.get('bound_family')}` | `{row.get('dominant_core')}` | "
                    f"{_f(row.get('comm_share_mean')) * 100:.2f}% |"
                )
    return lines


def operator_view_lines(
    operator_class_rows: Sequence[Mapping[str, Any]],
    hccl_class_rows: Sequence[Mapping[str, Any]],
    hccl_op_rows: Sequence[Mapping[str, Any]],
    *,
    top_compute: int = 10,
) -> list[str]:
    """Render the per-operator view (compute hot-spots + HCCL summary).

    Two layers:

    1. Top compute operators (rank-merged from
       ``operator_class_summary.csv``).  Excludes HCCL kernels so the
       table only shows AIC / AIV / mix_cv / mix_comm_aiv work; for each
       op we show the AIC / AIV / MTE2 stage breakdown so the reader can
       see *why* a kernel is bound where it is.
    2. HCCL summary (rank-merged from ``hccl_class_summary.csv`` plus
       per-rank rows from ``hccl_op_summary.csv``).  The per-kind row
       shows total time, calls, and ``rank_skew_ratio`` so collective
       imbalance is immediately visible.

    See ``knowledge/communication_taxonomy.md`` for the HCCL op_kind
    mapping and what level-0 / level-1 profiling can answer.
    """

    lines: list[str] = []

    compute_rows = [
        row
        for row in operator_class_rows
        if str(row.get("op_type") or "") not in {"communication", "mix_comm_aiv", "aicpu", "dsa", "unknown"}
    ]
    compute_rows.sort(key=lambda row: -_f(row.get("duration_sum_us")))

    if compute_rows:
        lines.extend(
            [
                "Top compute operators (rank-merged) — `op_type` and `bound_family` are the source-of-truth labels; "
                "the AIC / AIV / MTE2 columns are summed pipeline times in **microseconds** so the reader can see "
                "where the kernel actually spends its budget.",
                "",
                "| Operator | Task | op_type | Calls | Σ duration ms | Σ AIC ms | Σ AIV ms | aic_mte2 ms | aiv_mte2 ms | aiv_mte3 ms | bound_family | Core |",
                "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
            ]
        )
        for row in compute_rows[:top_compute]:
            lines.append(
                f"| `{row.get('name')}` | `{row.get('task_type')}` | `{row.get('op_type')}` | "
                f"{row.get('call_count')} | "
                f"{_f(row.get('duration_sum_us')) / 1000.0:.3f} | "
                f"{_f(row.get('aicore_time')) / 1000.0:.3f} | "
                f"{_f(row.get('aiv_time')) / 1000.0:.3f} | "
                f"{_f(row.get('aic_mte2_time')) / 1000.0:.3f} | "
                f"{_f(row.get('aiv_mte2_time')) / 1000.0:.3f} | "
                f"{_f(row.get('aiv_mte3_time')) / 1000.0:.3f} | "
                f"`{row.get('bound_family')}` | `{row.get('dominant_core')}` |"
            )
    else:
        lines.append("_No compute operators surfaced (operator_class_summary.csv is empty)._")

    if hccl_class_rows:
        lines.extend(
            [
                "",
                "HCCL collective summary (rank-merged across all ranks). `comm_aiv_fused` is the "
                "fused dispatch / combine kernel family (`op_type=mix_comm_aiv`).  `rank_skew_ratio` "
                "is `(max_rank_avg - min_rank_avg) / mean_rank_avg` for the per-call duration; values "
                "above ~0.30 are flagged as `communication_collective_slow` by `diagnostics.py`.  See "
                "`tools/ascend_profile/knowledge/communication_taxonomy.md` for op-kind mapping and "
                "level-0 vs level-1 caveats.",
                "",
                "| HCCL op | Fused (comm+AIV) | Ranks | Calls | Σ duration ms | Mean per call us | Min rank us | Max rank us | Skew |",
                "|---|:---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for row in hccl_class_rows:
            fused = "yes" if str(row.get("comm_aiv_fused", "")).lower() in {"true", "1"} else ""
            lines.append(
                f"| `{row.get('hccl_op_kind')}` | {fused} | "
                f"{row.get('rank_count')} | {row.get('call_count')} | "
                f"{_f(row.get('duration_sum_us')) / 1000.0:.3f} | "
                f"{_f(row.get('duration_avg_us')):.3f} | "
                f"{_f(row.get('rank_avg_min_us')):.3f} | "
                f"{_f(row.get('rank_avg_max_us')):.3f} | "
                f"{_f(row.get('rank_skew_ratio')) * 100:.2f}% |"
            )

        # Per-rank breakdown of the heaviest HCCL kind so users can spot
        # which rank is slow without opening the CSV.
        heaviest = max(
            hccl_class_rows,
            key=lambda row: _f(row.get("duration_sum_us")),
        )
        heaviest_kind = str(heaviest.get("hccl_op_kind") or "")
        heaviest_fused = str(heaviest.get("comm_aiv_fused", "")).lower() in {"true", "1"}
        rank_rows = [
            row
            for row in hccl_op_rows
            if str(row.get("hccl_op_kind") or "") == heaviest_kind
            and (str(row.get("comm_aiv_fused", "")).lower() in {"true", "1"}) == heaviest_fused
        ]
        if rank_rows:
            rank_rows.sort(key=lambda row: -_f(row.get("duration_avg_us")))
            lines.extend(
                [
                    "",
                    f"Per-rank breakdown of heaviest HCCL kind `{heaviest_kind}`"
                    + (" (comm_aiv_fused)" if heaviest_fused else "")
                    + " — sorted by `duration_avg_us` desc, the slowest rank is at the top.",
                    "",
                    "| Rank | Calls | Σ duration ms | Mean us | p50 us | p90 us | Max us |",
                    "|---|---:|---:|---:|---:|---:|---:|",
                ]
            )
            for row in rank_rows[:8]:
                lines.append(
                    f"| `{row.get('rank_id')}` | {row.get('call_count')} | "
                    f"{_f(row.get('duration_sum_us')) / 1000.0:.3f} | "
                    f"{_f(row.get('duration_avg_us')):.3f} | "
                    f"{_f(row.get('duration_p50_us')):.3f} | "
                    f"{_f(row.get('duration_p90_us')):.3f} | "
                    f"{_f(row.get('duration_max_us')):.3f} |"
                )
    else:
        lines.extend(
            [
                "",
                "_No HCCL collectives surfaced for this profile (single-rank capture or pure compute workload)._",
            ]
        )
    return lines


def pipeline_coverage_lines(summary_manifest: Mapping[str, Any], operator_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    """Render the Pipeline Coverage section.

    Three tables:
      1. coverage (fraction of events / operators that carry AIC/AIV
         stage signal).
      2. operator op_type histogram (always available -- comes from the
         ``Accelerator Core`` column, not from optional pipeline data).
      3. bound-family histogram, restricted to ops that actually carry
         pipeline signal so we never imply structure where the source
         CSV had none.
    """

    coverage = summary_manifest.get("pipeline_coverage") or {}
    lines = [
        "| Scope | With pipeline signal | Total | Coverage |",
        "|---|---:|---:|---:|",
        (
            f"| events | {coverage.get('events_with_pipeline_signal', 0)} | "
            f"{coverage.get('events_total', 0)} | "
            f"{_f(coverage.get('events_ratio')) * 100:.2f}% |"
        ),
        (
            f"| operators | {coverage.get('operators_with_pipeline_signal', 0)} | "
            f"{coverage.get('operators_total', 0)} | "
            f"{(coverage.get('operators_with_pipeline_signal', 0) / coverage.get('operators_total', 1)) * 100 if coverage.get('operators_total') else 0.0:.2f}% |"
        ),
        "",
        "Operator op_type histogram (from `Accelerator Core` column):",
        "",
        "| op_type | Operators | Σ duration ms | aicore Σms | aiv Σms |",
        "|---|---:|---:|---:|---:|",
    ]
    type_counts: Counter[str] = Counter()
    type_duration: dict[str, float] = {}
    type_aic: dict[str, float] = {}
    type_aiv: dict[str, float] = {}
    for row in operator_rows:
        op_type = str(row.get("op_type") or "unknown")
        type_counts[op_type] += 1
        type_duration[op_type] = type_duration.get(op_type, 0.0) + _f(row.get("duration_sum_us")) / 1000.0
        type_aic[op_type] = type_aic.get(op_type, 0.0) + _f(row.get("aicore_time")) / 1000.0
        type_aiv[op_type] = type_aiv.get(op_type, 0.0) + _f(row.get("aiv_time")) / 1000.0
    op_type_order = ("aic", "aiv", "mix_cv", "mix_comm_aiv", "communication", "aicpu", "dsa", "unknown")
    for op_type in op_type_order:
        if op_type not in type_counts:
            continue
        lines.append(
            f"| `{op_type}` | {type_counts[op_type]} | {type_duration[op_type]:.3f} | "
            f"{type_aic[op_type]:.3f} | {type_aiv[op_type]:.3f} |"
        )
    leftovers = [k for k in type_counts if k not in op_type_order]
    for op_type in leftovers:
        lines.append(
            f"| `{op_type}` | {type_counts[op_type]} | {type_duration[op_type]:.3f} | "
            f"{type_aic[op_type]:.3f} | {type_aiv[op_type]:.3f} |"
        )

    lines.extend(
        [
            "",
            "Operator bound family histogram (pipeline signal only):",
            "",
            "| bound_family | Operators | Σ duration ms |",
            "|---|---:|---:|",
        ]
    )
    family_counts: Counter[str] = Counter()
    family_duration: dict[str, float] = {}
    for row in operator_rows:
        signal = str(row.get("pipeline_signal") or "").lower() in {"true", "1"}
        if not signal:
            continue
        family = str(row.get("bound_family") or "unknown")
        family_counts[family] += 1
        family_duration[family] = family_duration.get(family, 0.0) + _f(row.get("duration_sum_us")) / 1000.0
    for family, count in family_counts.most_common():
        lines.append(f"| `{family}` | {count} | {family_duration.get(family, 0.0):.3f} |")
    if not family_counts:
        lines.append("| `none` | 0 | 0.000 |")
    return lines


def markdown_report(output_dir: Path, report_id: str) -> str:
    normalize_manifest = read_json(output_dir / "normalize_manifest.json", default={})
    segment_manifest = read_json(output_dir / "segment_manifest.json", default={})
    summary_manifest = read_json(output_dir / "summary_manifest.json", default={})
    cross_manifest = read_json(output_dir / "cross_rank_manifest.json", default={})
    diagnosis_payload = read_json(output_dir / "diagnosis_findings.json", default={})
    rank_rows = csv_rows(output_dir / "rank_summary.csv")
    step_rows = csv_rows(output_dir / "step_summary.csv")
    anatomy_rows = csv_rows(output_dir / "step_anatomy.csv")
    operator_rows = csv_rows(output_dir / "operator_summary.csv")
    operator_class_rows = csv_rows(output_dir / "operator_class_summary.csv")
    hccl_op_rows = csv_rows(output_dir / "hccl_op_summary.csv")
    hccl_class_rows = csv_rows(output_dir / "hccl_class_summary.csv")
    step_class_rows = csv_rows(output_dir / "step_class_summary.csv")
    layer_class_rows = csv_rows(output_dir / "layer_class_summary.csv")
    block_class_rows = csv_rows(output_dir / "block_class_summary.csv")
    findings = finding_rows(output_dir)
    finding_counts = Counter(str(item.get("finding_type") or "unknown") for item in findings)
    coverage = summary_manifest.get("pipeline_coverage") or {}
    coverage_pct = _f(coverage.get("events_ratio")) * 100
    lines = [
        "# Ascend Profiling Analysis Report",
        "",
        "## 1. Executive Summary",
        "",
        f"- Report id: `{report_id}`",
        f"- Profile root: `{normalize_manifest.get('profile_root')}`",
        f"- Rank count: `{normalize_manifest.get('rank_count')}`",
        f"- Event count: `{normalize_manifest.get('event_count')}`",
        f"- Step segments: `{segment_manifest.get('segment_count')}`",
        f"- Layer segments: `{segment_manifest.get('layer_count')}`",
        f"- Pipeline coverage: `{coverage_pct:.2f}%` of events expose AIC/AIV stage breakdown",
        f"- Diagnosis findings: `{len(findings)}`",
        "",
        "This report is generated from normalized device-side profiling events and rank-local step segments. "
        "Every finding is expected to reference evidence ids and source row ranges in the companion XLSX.",
        "",
        "## 2. Capture And Segmentation",
        "",
        "| Rank | Steps | Segments | Layer inventory | Wall ms | Busy ms | Underfeed | Roles |",
        "|---|---:|---:|---|---:|---:|---:|---|",
    ]
    for row in rank_rows[:64]:
        lines.append(
            f"| `{row.get('rank_id')}` | {row.get('step_count')} | {row.get('segment_count')} | "
            f"`{row.get('layer_count_inventory')}` | {row.get('wall_ms')} | {row.get('busy_union_ms')} | "
            f"{row.get('underfeed_ratio')} | `{row.get('role_counts')}` |"
        )
    lines.extend(
        [
            "",
            "## 3. Macro Step Timeline",
            "",
            "Per-step head / main / tail / bubble decomposition is derived from `step_anatomy.csv` "
            "(see `tools/ascend_profile/knowledge/step_anatomy.md` for the boundary rules).",
            "",
        ]
    )
    lines.extend(macro_timeline_lines(step_rows, anatomy_rows))
    lines.extend(
        [
            "",
            "## 4. Pipeline Coverage And Bound Families",
            "",
            "Pipeline figures only apply to events whose `kernel_details.csv` row exposed AIC/AIV stage columns. "
            "AICPU and HCCL events are tagged separately and do not count as missing data.  See "
            "`tools/ascend_profile/knowledge/pipeline_taxonomy.md` and `bound_classification.md` for the field schema.",
            "",
        ]
    )
    lines.extend(pipeline_coverage_lines(summary_manifest, operator_rows))
    lines.extend(
        [
            "",
            "## 5. Step Class View",
            "",
            "Steps are grouped into classes by **strict shape equality** -- two members "
            "share a class iff their structure signatures match *and* their ordered "
            "shape-bearing event sequences are identical (see "
            "`tools/ascend_profile/knowledge/step_class_grouping.md`).  Members with no "
            "shape-bearing events fall into singleton `*_unknown_shape_*` classes and are "
            "never merged into a real class.",
            "",
        ]
    )
    lines.extend(step_class_view_lines(step_class_rows, layer_class_rows))
    lines.extend(
        [
            "",
            "## 6. Layer And Block View",
            "",
            "Each transformer layer is split into one `attention` block followed by one "
            "`ffn` or `moe` block (see `tools/ascend_profile/knowledge/block_taxonomy.md`).  "
            "Layers that have no attention kernel are flagged as `companion_layer` so the "
            "report keeps them separate from the main forward pass.",
            "",
        ]
    )
    lines.extend(layer_block_view_lines(layer_class_rows, block_class_rows))
    lines.extend(
        [
            "",
            "## 7. Operator View",
            "",
            "Compute and HCCL operators are surfaced rank-merged so the table reflects the whole "
            "capture window.  See `tools/ascend_profile/knowledge/communication_taxonomy.md` for "
            "the HCCL `op_kind` mapping (allreduce / allgather / reducescatter / alltoallv / ...) "
            "and the level-0 vs level-1 caveats; rank-level rows are exported to "
            "`hccl_op_summary.csv` for slow-rank diagnostics.",
            "",
        ]
    )
    lines.extend(operator_view_lines(operator_class_rows, hccl_class_rows, hccl_op_rows))
    lines.extend(
        [
            "",
            "## 8. Step Inventory",
            "",
            "| Family | Layer count | Count | Avg wall ms | Avg main ms | Avg head ms | Avg tail ms | Max bubble ms |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in step_rows:
        if row.get("segment_type") != "step":
            continue
        key = (str(row.get("step_family")), str(row.get("main_layer_count")))
        grouped.setdefault(key, []).append(row)
    anatomy_by_segment_for_inv = {str(item.get("segment_id")): item for item in anatomy_rows}
    for (family, layer_count), items in sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0])):
        wall = [_f(item.get("wall_ms")) for item in items]
        bubble = [_f(item.get("largest_internal_bubble_ms")) for item in items]
        head_ms: list[float] = []
        main_ms: list[float] = []
        tail_ms: list[float] = []
        for item in items:
            anatomy = anatomy_by_segment_for_inv.get(str(item.get("segment_id")))
            if anatomy is None:
                continue
            head_ms.append(_f(anatomy.get("head_wall_ms")))
            main_ms.append(_f(anatomy.get("main_wall_ms")))
            tail_ms.append(_f(anatomy.get("tail_wall_ms")))

        def _avg_ms(values: list[float]) -> float:
            return (sum(values) / len(values)) if values else 0.0

        lines.append(
            f"| `{family}` | {layer_count} | {len(items)} | "
            f"{_avg_ms(wall):.6f} | {_avg_ms(main_ms):.6f} | {_avg_ms(head_ms):.6f} | "
            f"{_avg_ms(tail_ms):.6f} | {max(bubble) if bubble else 0.0:.6f} |"
        )
    lines.extend(
        [
            "",
            "## 9. Cross-Rank And Anomaly Findings",
            "",
            "| Severity | Type | Confidence | Ranks | Evidence | Summary |",
            "|---|---|---|---|---|---|",
        ]
    )
    for item in top_findings(findings):
        ranks = item.get("rank_ids") or []
        evidence = item.get("evidence_ids") or item.get("alignment_ids") or []
        lines.append(
            f"| {item.get('severity')} | `{item.get('finding_type')}` | {item.get('confidence')} | "
            f"`{ranks}` | `{evidence}` | {str(item.get('summary') or '').replace('|', '/')} |"
        )
    if not findings:
        lines.append("| info | `none` | high | `[]` | `[]` | No diagnosis findings were emitted. |")
    lines.extend(
        [
            "",
            "## 10. Finding Inventory",
            "",
            "| Finding type | Count |",
            "|---|---:|",
        ]
    )
    for finding_type, count in finding_counts.most_common():
        lines.append(f"| `{finding_type}` | {count} |")
    lines.extend(
        [
            "",
            "## 11. Evidence Chain",
            "",
            "- `report.xlsx:evidence_index` maps evidence ids to source rows, segment ids, and layer ids.",
            "- `report.xlsx:step_anatomy` is the head / main / tail / bubble per-step evidence table.",
            "- `report.xlsx:block_summary` is the per-block decomposition (attention/ffn/moe with bound + comm share).",
            "- `report.xlsx:step_class_summary`, `layer_class_summary`, `block_class_summary` carry the shape-strict class aggregates.",
            "- `report.xlsx:operator_class_summary` is the rank-merged operator view; `hccl_op_summary` and `hccl_class_summary` cover collective communication.",
            "- `report.xlsx:raw_kernel_index` maps normalized event ids back to original `kernel_details.csv` rows.",
            "- `report.xlsx:cross_rank_alignment` contains cross-rank step/operator alignment evidence.",
            "- `diagnosis_findings.json` is the machine-readable claim source for this Markdown report.",
            "",
            "## 12. Limitations",
            "",
            "- Step and layer segmentation is inferred from structural anchors and should be audited on new model families.",
            "- Pipeline coverage may be < 100% on older CANN versions; per-stage figures are skipped for events without source columns.",
            "- Host-side root cause attribution is not asserted unless host trace evidence is present.",
            "- Missing shape fields reduce confidence for slow-rank and DP-load diagnoses.",
            "",
        ]
    )
    return "\n".join(lines)


def sheet_rows(output_dir: Path) -> dict[str, list[Mapping[str, Any]]]:
    findings = finding_rows(output_dir)
    case_summary = [
        {
            "profile_root": read_json(output_dir / "normalize_manifest.json", default={}).get("profile_root"),
            "rank_count": read_json(output_dir / "normalize_manifest.json", default={}).get("rank_count"),
            "event_count": read_json(output_dir / "normalize_manifest.json", default={}).get("event_count"),
            "finding_count": len(findings),
            "finding_types": dict(Counter(str(item.get("finding_type") or "unknown") for item in findings)),
        }
    ]
    raw_kernel_index = csv_rows(output_dir / "raw_kernel_index.csv")
    raw_kernel_sheet = raw_kernel_index[:200000]
    if len(raw_kernel_index) > len(raw_kernel_sheet):
        raw_kernel_sheet.append(
            {
                "event_id": "__truncated__",
                "rank_id": "",
                "source_id": "",
                "row_idx": "",
                "name": f"XLSX raw_kernel_index truncated at {len(raw_kernel_sheet)} rows; use raw_kernel_index.csv for complete data.",
            }
        )
    bubble_windows = list(read_jsonl(output_dir / "evidence" / "bubble_windows.jsonl"))
    return {
        "README": [
            {
                "key": "traceability",
                "value": "Use evidence_id -> evidence_index -> raw_kernel_index/source row.",
            },
            {
                "key": "source",
                "value": "Generated from Ascend profiling normalized events and step segments.",
            },
        ],
        "case_summary": case_summary,
        "rank_summary": csv_rows(output_dir / "rank_summary.csv"),
        "step_summary": csv_rows(output_dir / "step_summary.csv"),
        "step_anatomy": csv_rows(output_dir / "step_anatomy.csv"),
        "step_class_summary": csv_rows(output_dir / "step_class_summary.csv"),
        "layer_summary": csv_rows(output_dir / "layer_summary.csv"),
        "layer_class_summary": csv_rows(output_dir / "layer_class_summary.csv"),
        "block_summary": csv_rows(output_dir / "block_summary.csv"),
        "block_class_summary": csv_rows(output_dir / "block_class_summary.csv"),
        "operator_summary": csv_rows(output_dir / "operator_summary.csv"),
        "operator_class_summary": csv_rows(output_dir / "operator_class_summary.csv"),
        "hccl_op_summary": csv_rows(output_dir / "hccl_op_summary.csv"),
        "hccl_class_summary": csv_rows(output_dir / "hccl_class_summary.csv"),
        "bubble_windows": bubble_windows,
        "wait_anchor_ops": csv_rows(output_dir / "wait_anchor_ops.csv"),
        "aicpu_summary": csv_rows(output_dir / "aicpu_summary.csv"),
        "cross_rank_alignment": csv_rows(output_dir / "cross_rank_alignment.csv"),
        "diagnosis_findings": findings,
        "evidence_index": csv_rows(output_dir / "evidence_index.csv"),
        "raw_kernel_index": raw_kernel_sheet,
    }


def render_report(output_dir: Path) -> dict[str, Any]:
    report_dir = output_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_id = stable_id("report", output_dir, read_json(output_dir / "normalize_manifest.json", default={}).get("profile_root"))
    markdown = markdown_report(output_dir, report_id)
    (report_dir / "report.md").write_text(markdown, encoding="utf-8")
    sheets = sheet_rows(output_dir)
    write_xlsx(report_dir / "report.xlsx", sheets)

    # HTML report (rich, single-file, zero-dependency). Best-effort: don't fail
    # the whole stage if html rendering hits an exception — emit a stub file
    # noting the failure so downstream consumers can still see something.
    html_path = report_dir / "report.html"
    html_status = "ok"
    html_error: str | None = None
    try:
        try:
            from .html_report import build_html_report
        except ImportError:  # pragma: no cover
            import sys as _sys
            _sys.path.insert(0, str(Path(__file__).resolve().parent))
            from html_report import build_html_report  # type: ignore[no-redef]
        build_html_report(output_dir, html_path)
    except Exception as exc:  # noqa: BLE001
        html_status = "error"
        html_error = f"{type(exc).__name__}: {exc}"
        html_path.write_text(
            "<!doctype html><meta charset='utf-8'><title>HTML report failed</title>"
            "<body style='font-family:sans-serif;padding:20px;background:#0d1117;color:#c9d1d9'>"
            "<h1>HTML report could not be rendered</h1>"
            f"<pre style='color:#f85149'>{html_error}</pre>"
            "<p>Fall back to <code>report.md</code> / <code>report.xlsx</code> in this directory.</p>",
            encoding="utf-8",
        )

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "report",
        "created_at": utc_now(),
        "report_id": report_id,
        "output_dir": str(report_dir),
        "files": {
            "markdown": "report.md",
            "xlsx": "report.xlsx",
            "html": "report.html",
            "manifest": "manifest.json",
        },
        "html_status": html_status,
        "sheet_map": {name: name for name in sheets},
        "claim_ids": [item.get("claim_id") for item in finding_rows(output_dir)],
    }
    if html_error:
        manifest["html_error"] = html_error
    write_json(report_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = render_report(Path(args.output))
    print({"stage": "report", "output_dir": manifest["output_dir"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
