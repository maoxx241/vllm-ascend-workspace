#!/usr/bin/env python3
"""Render Markdown/XLSX report packages from analysis artifacts."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .analysis_summary import build_analysis_summary
    from .common import (
        SCHEMA_VERSION,
        TOOL_VERSION,
        csv_rows,
        emit_stage_json,
        read_json,
        read_jsonl,
        stable_id,
        utc_now,
        write_json,
        write_xlsx,
    )
    from .store import iter_csv_rows, parse_jsonish, to_float
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from analysis_summary import build_analysis_summary  # type: ignore[no-redef]
    from common import (
        # type: ignore[no-redef]
        SCHEMA_VERSION,
        TOOL_VERSION,
        csv_rows,
        emit_stage_json,
        read_json,
        read_jsonl,
        stable_id,
        utc_now,
        write_json,
        write_xlsx,
    )
    from store import iter_csv_rows, parse_jsonish, to_float  # type: ignore[no-redef]


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


def _quantile(values: Sequence[float], q: float) -> float:
    # NOTE: intentionally NOT ``metrics.quantile`` — that one interpolates
    # between neighbours; this nearest-rank variant keeps report numbers
    # stable across refactors. Do not merge the two.
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
        wall = [to_float(item.get("wall_ms")) for item in items]
        head_ratio: list[float] = []
        main_ratio: list[float] = []
        tail_ratio: list[float] = []
        bubble_ratio: list[float] = []
        for item in items:
            anatomy = anatomy_by_segment.get(str(item.get("segment_id")))
            if anatomy is None:
                continue
            head_ratio.append(to_float(anatomy.get("head_ratio")))
            main_ratio.append(to_float(anatomy.get("main_ratio")))
            tail_ratio.append(to_float(anatomy.get("tail_ratio")))
            bubble_ratio.append(to_float(anatomy.get("bubble_ratio")))

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
        key=lambda item: to_float(item.get("wall_ms")),
        reverse=True,
    )[:8]
    for row in sorted_steps:
        anatomy = anatomy_by_segment.get(str(row.get("segment_id"))) or {}
        lines.append(
            f"| `{row.get('segment_id')}` | `{row.get('rank_id')}` | "
            f"`{row.get('step_family')}` | {row.get('main_layer_count')} | "
            f"{to_float(row.get('wall_ms')):.3f} | {to_float(anatomy.get('head_wall_ms')):.3f} | "
            f"{to_float(anatomy.get('main_wall_ms')):.3f} | {to_float(anatomy.get('tail_wall_ms')):.3f} | "
            f"{to_float(row.get('underfeed_ms')):.3f} | {to_float(anatomy.get('bubble_ratio')) * 100:.2f} |"
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
        mean = to_float(row.get("wall_ms_mean"))
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
            f"{to_float(row.get('wall_ms_mean')):.3f} | {to_float(row.get('wall_ms_p50')):.3f} | "
            f"{to_float(row.get('wall_ms_p90')):.3f} | "
            f"{to_float(row.get('head_ratio_mean')) * 100:.2f} | "
            f"{to_float(row.get('main_ratio_mean')) * 100:.2f} | "
            f"{to_float(row.get('tail_ratio_mean')) * 100:.2f} | "
            f"{to_float(row.get('bubble_ratio_mean')) * 100:.2f} | {unknown} |"
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
                f"| `{entry.get('layer_class_id')}` | {to_float(entry.get('wall_ms_sum')):.3f} | "
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
                f"{to_float(op.get('duration_sum_us')) / 1000.0:.3f} | {op.get('call_count')} |"
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
            key=lambda row: -(to_float(row.get("wall_ms_mean")) * float(row.get("member_count") or 0)),
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
                f"{to_float(row.get('wall_ms_mean')):.3f} | {to_float(row.get('wall_ms_p50')):.3f} | "
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
                key=lambda row: -(to_float(row.get("wall_ms_mean")) * float(row.get("member_count") or 0)),
            )
            for row in members[:top_block]:
                companion = "yes" if str(row.get("companion_layer", "")).lower() in {"true", "1"} else ""
                lines.append(
                    f"| `{kind}` | `{row.get('block_class_id')}` | {companion} | "
                    f"{row.get('member_count')} | {to_float(row.get('wall_ms_mean')):.3f} | "
                    f"{to_float(row.get('wall_ms_p50')):.3f} | "
                    f"`{row.get('bound_family')}` | `{row.get('dominant_core')}` | "
                    f"{to_float(row.get('comm_share_mean')) * 100:.2f}% |"
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
    compute_rows.sort(key=lambda row: -to_float(row.get("duration_sum_us")))

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
                f"{to_float(row.get('duration_sum_us')) / 1000.0:.3f} | "
                f"{to_float(row.get('aicore_time')) / 1000.0:.3f} | "
                f"{to_float(row.get('aiv_time')) / 1000.0:.3f} | "
                f"{to_float(row.get('aic_mte2_time')) / 1000.0:.3f} | "
                f"{to_float(row.get('aiv_mte2_time')) / 1000.0:.3f} | "
                f"{to_float(row.get('aiv_mte3_time')) / 1000.0:.3f} | "
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
                "`ascend_profile/knowledge/communication_taxonomy.md` for op-kind mapping and "
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
                f"{to_float(row.get('duration_sum_us')) / 1000.0:.3f} | "
                f"{to_float(row.get('duration_avg_us')):.3f} | "
                f"{to_float(row.get('rank_avg_min_us')):.3f} | "
                f"{to_float(row.get('rank_avg_max_us')):.3f} | "
                f"{to_float(row.get('rank_skew_ratio')) * 100:.2f}% |"
            )

        # Per-rank breakdown of the heaviest HCCL kind so users can spot
        # which rank is slow without opening the CSV.
        heaviest = max(
            hccl_class_rows,
            key=lambda row: to_float(row.get("duration_sum_us")),
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
            rank_rows.sort(key=lambda row: -to_float(row.get("duration_avg_us")))
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
                    f"{to_float(row.get('duration_sum_us')) / 1000.0:.3f} | "
                    f"{to_float(row.get('duration_avg_us')):.3f} | "
                    f"{to_float(row.get('duration_p50_us')):.3f} | "
                    f"{to_float(row.get('duration_p90_us')):.3f} | "
                    f"{to_float(row.get('duration_max_us')):.3f} |"
                )
    else:
        lines.extend(
            [
                "",
                "_No HCCL collectives surfaced for this profile (single-rank capture or pure compute workload)._",
            ]
        )
    return lines


def model_fingerprint_lines(
    inferred_rows: Sequence[Mapping[str, Any]],
    feature_rows: Sequence[Mapping[str, Any]],
    layer_type_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
) -> list[str]:
    """Render profile-derived model fingerprint and candidate matching.

    These rows are estimates from profiling evidence, not config facts.
    They intentionally live outside diagnosis findings.
    """

    lines: list[str] = [
        "Profile-derived fingerprint. These values come from `kernel_details.csv` shape fields, "
        "`step_summary.csv`, and `layer_summary.csv`; they narrow candidate models but do not prove "
        "non-profile fields such as tokenizer ids or rope theta. If an `lm_head` / logits matmul is "
        "visible, the output dimension is reported as a vocab or vocab-shard candidate.",
        "",
        "| Field | Inferred value | Confidence | Observations | Evidence | Note |",
        "|---|---:|---|---:|---|---|",
    ]
    if inferred_rows:
        for row in inferred_rows:
            lines.append(
                f"| `{row.get('field')}` | `{row.get('inferred_value')}` | "
                f"{row.get('confidence')} | {row.get('observations')} | "
                f"`{row.get('evidence')}` | {str(row.get('note') or '').replace('|', '/')} |"
            )
    else:
        lines.append("| _none_ | `unknown` | unknown | 0 | — | no shape evidence |")

    lines.extend(
        [
            "",
            "Observed architecture features:",
            "",
            "| Feature | Confidence | Events | Evidence | Note |",
            "|---|---|---:|---|---|",
        ]
    )
    if feature_rows:
        for row in feature_rows:
            lines.append(
                f"| `{row.get('feature')}` | {row.get('confidence')} | "
                f"{row.get('event_count')} | `{row.get('evidence')}` | "
                f"{str(row.get('note') or '').replace('|', '/')} |"
            )
    else:
        lines.append("| _none_ | unknown | 0 | — | no feature signatures |")

    lines.extend(
        [
            "",
            "Observed layer/block structure:",
            "",
            "| Layer type | Observations | Share |",
            "|---|---:|---:|",
        ]
    )
    if layer_type_rows:
        for row in layer_type_rows[:12]:
            lines.append(
                f"| `{row.get('layer_type')}` | {row.get('observations')} | "
                f"{to_float(row.get('share')) * 100:.2f}% |"
            )
    else:
        lines.append("| `unknown` | 0 | 0.00% |")

    lines.extend(
        [
            "",
            "Candidate model match (local fingerprint catalog, not a network lookup):",
            "",
            "| Candidate | Confidence | Score | Match ratio | Reasons |",
            "|---|---|---:|---:|---|",
        ]
    )
    if candidate_rows:
        for row in candidate_rows[:8]:
            reasons = parse_jsonish(row.get("matched_reasons"), [])
            if not isinstance(reasons, list):
                reasons = [str(reasons)]
            lines.append(
                f"| `{row.get('model_name')}` | {row.get('confidence')} | "
                f"{to_float(row.get('score')):.1f}/{to_float(row.get('max_score')):.1f} | "
                f"{to_float(row.get('match_ratio')) * 100:.1f}% | "
                f"{', '.join(str(item) for item in reasons[:8]).replace('|', '/')} |"
            )
    else:
        lines.append("| _none_ | unknown | 0/0 | 0.0% | no fingerprint catalog match |")
    return lines


def operator_efficiency_lines(
    operator_efficiency_rows: Sequence[Mapping[str, Any]],
    *,
    top_n: int = 12,
) -> list[str]:
    """Render LLMInsight-style operator calculation estimates."""

    lines: list[str] = [
        "Operator calculation estimates are derived from CANN shape/dtype fields. "
        "FLOPs are modeled for matmul / fused-attention / vector-like kernels; bytes are "
        "read+write tensor bytes. MFU-style efficiency uses the selected hardware theoretical "
        "peak; reclaim ranking uses the sustained peak when a measured factor exists.",
        "",
        "| Operator | Work class | DType | Calls | Σ duration ms | Est TFLOPs | Achieved TFLOPS | Theoretical peak | MFU | Sustained peak | Sustained eff | Reclaim sustained ms | Confidence |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    rows = [
        row
        for row in operator_efficiency_rows
        if to_float(row.get("reclaim_us_sustained")) > 0 or to_float(row.get("reclaim_us_theoretical")) > 0 or to_float(row.get("estimated_flops")) > 0 or to_float(row.get("estimated_bytes")) > 0
    ][:top_n]
    for row in rows:
        lines.append(
            f"| `{row.get('name')}` | `{row.get('work_class')}` | `{row.get('dtype')}` | {row.get('call_count')} | "
            f"{to_float(row.get('duration_sum_us')) / 1000.0:.3f} | "
            f"{to_float(row.get('estimated_flops')) / 1e12:.6f} | "
            f"{to_float(row.get('achieved_tflops')):.3f} | "
            f"{to_float(row.get('theoretical_peak_tflops_or_tops')):.3f} | "
            f"{to_float(row.get('mfu_theoretical')) * 100:.2f}% | "
            f"{to_float(row.get('sustained_peak_tflops_or_tops')):.3f} | "
            f"{to_float(row.get('sustained_efficiency')) * 100:.2f}% | "
            f"{to_float(row.get('reclaim_us_sustained')) / 1000.0:.3f} | "
            f"{row.get('confidence')} |"
        )
    if not rows:
        lines.append("| _none_ | `unmodeled` | | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | no shape/dtype evidence |")
    return lines


def hardware_context_lines(
    hardware_rows: Sequence[Mapping[str, Any]],
    theoretical_rows: Sequence[Mapping[str, Any]],
    summary_manifest: Mapping[str, Any],
) -> list[str]:
    summary = {
        str(row.get("key")): row.get("value")
        for row in hardware_rows
        if row.get("key") not in (None, "")
    }
    limitations = (summary_manifest.get("hardware_insights") or {}).get("limitations") or []
    lines: list[str] = [
        "Hardware context is explicit evidence for MFU denominators. Current-host hardware is not treated as profiling provenance unless the user, manifest, or hardware profile identifies it as the capture hardware.",
        "",
        "| Key | Value |",
        "|---|---|",
    ]
    keys = [
        "hardware_model",
        "hardware_source",
        "theoretical_peak_source",
        "measurement_source",
        "fp16_tflops_theoretical",
        "bf16_tflops_theoretical",
        "int8_tops_theoretical",
        "fp16_tflops_sustained",
        "bf16_tflops_sustained",
        "int8_tops_sustained",
        "fp16_sustained_factor",
        "bf16_sustained_factor",
        "int8_sustained_factor",
        "memory_size_gib",
        "cann_ddr_derived_gbps",
    ]
    for key in keys:
        if key in summary:
            lines.append(f"| `{key}` | `{summary.get(key)}` |")
    if not hardware_rows:
        lines.append("| `hardware_model` | `unknown` |")
    lines.extend(
        [
            "",
            "CANN platform configs discovered in this analysis environment:",
            "",
            "| SoC | Cube cores | Cube MHz | FP16 TFLOPS | BF16 TFLOPS | INT8 TOPS | Memory GiB | Source |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in theoretical_rows[:16]:
        lines.append(
            f"| `{row.get('soc_version')}` | {row.get('cube_core_cnt')} | {to_float(row.get('cube_freq_mhz')):.0f} | "
            f"{to_float(row.get('fp16_tflops')):.3f} | {to_float(row.get('bf16_tflops')):.3f} | "
            f"{to_float(row.get('int8_tops')):.3f} | {to_float(row.get('memory_size_gib')):.1f} | "
            f"`{Path(str(row.get('source_path') or '')).name}` |"
        )
    if not theoretical_rows:
        lines.append("| _none_ | 0 | 0 | 0 | 0 | 0 | 0 | no CANN platform_config found |")
    if limitations:
        lines.extend(["", "Hardware limitations:", ""])
        for item in limitations:
            lines.append(f"- {item}")
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
            f"{to_float(coverage.get('events_ratio')) * 100:.2f}% |"
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
        type_duration[op_type] = type_duration.get(op_type, 0.0) + to_float(row.get("duration_sum_us")) / 1000.0
        type_aic[op_type] = type_aic.get(op_type, 0.0) + to_float(row.get("aicore_time")) / 1000.0
        type_aiv[op_type] = type_aiv.get(op_type, 0.0) + to_float(row.get("aiv_time")) / 1000.0
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
        family_duration[family] = family_duration.get(family, 0.0) + to_float(row.get("duration_sum_us")) / 1000.0
    for family, count in family_counts.most_common():
        lines.append(f"| `{family}` | {count} | {family_duration.get(family, 0.0):.3f} |")
    if not family_counts:
        lines.append("| `none` | 0 | 0.000 |")
    return lines


RAW_KERNEL_SHEET_ROW_LIMIT = 200_000

# Columns projected into the xlsx raw_kernel sheet. The normalized event
# index also carries profile_id (constant per run) and the fat JSON blobs
# shape_features / pipeline_us (~400 chars/row) -- keeping them would
# double the sheet XML for no drill-down value, so the stream projects
# only the columns below.
RAW_KERNEL_SHEET_COLUMNS = (
    "event_id",
    "rank_id",
    "source_id",
    "row_idx",
    "name_raw",
    "task_type",
    "accelerator_core",
    "stream_id",
    "start_us",
    "end_us",
    "duration_us",
    "wait_us",
    "op_categories",
    "op_roles",
    "shape_signature",
    "op_type",
)

# CSV artifacts loaded once per report render and shared by
# ``markdown_report`` / ``sheet_rows`` / ``validate_evidence_chain`` (each
# used to re-read the same files -- and ``sheet_rows`` alone read
# ``normalize_manifest.json`` three times).
_BUNDLE_CSV_NAMES = (
    "rank_summary",
    "step_summary",
    "step_anatomy",
    "operator_summary",
    "operator_class_summary",
    "operator_efficiency_summary",
    "hccl_op_summary",
    "hccl_class_summary",
    "model_inferred_config",
    "model_feature_summary",
    "model_layer_type_summary",
    "model_candidate_summary",
    "model_context_summary",
    "model_config_overview",
    "model_parameter_estimate",
    "model_kv_cache_estimate",
    "model_config_feature_summary",
    "hardware_summary",
    "hardware_theoretical_peaks",
    "step_class_summary",
    "layer_summary",
    "layer_class_summary",
    "block_summary",
    "block_class_summary",
    "wait_anchor_ops",
    "aicpu_summary",
    "cross_rank_alignment",
    "evidence_index",
)


def raw_kernel_sheet_rows(
    output_dir: Path,
    *,
    limit: int = RAW_KERNEL_SHEET_ROW_LIMIT,
) -> list[Mapping[str, Any]]:
    """Rows for the xlsx ``raw_kernel_index`` sheet.

    Source is ``normalized_event_index.csv`` -- a column superset of the
    retired raw_kernel_index.csv -- streamed via ``store.iter_csv_rows`` so
    a multi-million-row event index is never materialised just to take the
    first ``limit`` rows. Reading stops as soon as the limit is reached;
    one extra row is probed to detect truncation.
    """
    path = output_dir / "normalized_event_index.csv"
    rows: list[Mapping[str, Any]] = []
    truncated = False
    if path.is_file():
        for row_idx, row in iter_csv_rows(path):
            if row_idx >= limit:
                truncated = True
                break
            rows.append({key: row.get(key, "") for key in RAW_KERNEL_SHEET_COLUMNS})
    if truncated:
        marker = {key: "" for key in RAW_KERNEL_SHEET_COLUMNS}
        marker["event_id"] = "__truncated__"
        marker["name_raw"] = (
            f"XLSX raw_kernel_index truncated at {limit} rows; use normalized_event_index.csv for complete data."
        )
        rows.append(marker)
    return rows


def _load_report_bundle(output_dir: Path) -> dict[str, Any]:
    """Load every artifact the report stage needs, exactly once."""
    return {
        "manifests": {
            "normalize": read_json(output_dir / "normalize_manifest.json", default={}),
            "segment": read_json(output_dir / "segment_manifest.json", default={}),
            "summary": read_json(output_dir / "summary_manifest.json", default={}),
            "cross_rank": read_json(output_dir / "cross_rank_manifest.json", default={}),
        },
        "findings": finding_rows(output_dir),
        "csvs": {name: csv_rows(output_dir / f"{name}.csv") for name in _BUNDLE_CSV_NAMES},
        "bubble_windows": list(read_jsonl(output_dir / "evidence" / "bubble_windows.jsonl")),
        "raw_kernel_sheet": raw_kernel_sheet_rows(output_dir),
    }


def _id_set_from_csv(path: Path, column: str) -> set[str]:
    """Read a single column of a CSV into a set of non-empty strings.

    Used for id-membership checks where materialising full row dicts (the
    ``csv_rows`` path) would be wasted work on wide tables.
    """
    ids: set[str] = set()
    if not path.is_file():
        return ids
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        header = next(reader, None)
        if not header or column not in header:
            return ids
        idx = header.index(column)
        for row in reader:
            if idx < len(row):
                value = row[idx].strip()
                if value:
                    ids.add(value)
    return ids


def markdown_report(output_dir: Path, report_id: str, *, bundle: Mapping[str, Any] | None = None) -> str:
    if bundle is None:
        bundle = _load_report_bundle(output_dir)
    manifests = bundle["manifests"]
    csvs = bundle["csvs"]
    normalize_manifest = manifests["normalize"]
    segment_manifest = manifests["segment"]
    summary_manifest = manifests["summary"]
    rank_rows = csvs["rank_summary"]
    step_rows = csvs["step_summary"]
    anatomy_rows = csvs["step_anatomy"]
    operator_rows = csvs["operator_summary"]
    operator_class_rows = csvs["operator_class_summary"]
    operator_eff_rows = csvs["operator_efficiency_summary"]
    hccl_op_rows = csvs["hccl_op_summary"]
    hccl_class_rows = csvs["hccl_class_summary"]
    model_inferred_rows = csvs["model_inferred_config"]
    model_feature_rows = csvs["model_feature_summary"]
    model_layer_type_rows = csvs["model_layer_type_summary"]
    model_candidate_rows = csvs["model_candidate_summary"]
    hardware_rows = csvs["hardware_summary"]
    hardware_theoretical_rows = csvs["hardware_theoretical_peaks"]
    step_class_rows = csvs["step_class_summary"]
    layer_class_rows = csvs["layer_class_summary"]
    block_class_rows = csvs["block_class_summary"]
    findings = bundle["findings"]
    finding_counts = Counter(str(item.get("finding_type") or "unknown") for item in findings)
    coverage = summary_manifest.get("pipeline_coverage") or {}
    coverage_pct = to_float(coverage.get("events_ratio")) * 100
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
            "(see `ascend_profile/knowledge/step_anatomy.md` for the boundary rules).",
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
            "`ascend_profile/knowledge/pipeline_taxonomy.md` and `bound_classification.md` for the field schema.",
            "",
        ]
    )
    lines.extend(pipeline_coverage_lines(summary_manifest, operator_rows))
    lines.extend(
        [
            "",
            "## 5. Profile-Derived Model Fingerprint",
            "",
            "This section implements a profiling-first model-analysis lens: it infers the "
            "candidate config fields and model family from observed layers, block structure, "
            "operator signatures, and CANN shape cells. A `config.json` is useful as a "
            "comparison source, but is not required for this fingerprint.",
            "",
        ]
    )
    lines.extend(
        model_fingerprint_lines(
            model_inferred_rows,
            model_feature_rows,
            model_layer_type_rows,
            model_candidate_rows,
        )
    )
    lines.extend(
        [
            "",
            "## 6. Hardware Peak And MFU Context",
            "",
        ]
    )
    lines.extend(hardware_context_lines(hardware_rows, hardware_theoretical_rows, summary_manifest))
    lines.extend(
        [
            "",
            "## 7. Step Class View",
            "",
            "Steps are grouped into classes by **strict shape equality** -- two members "
            "share a class iff their structure signatures match *and* their ordered "
            "shape-bearing event sequences are identical (see "
            "`ascend_profile/knowledge/step_class_grouping.md`).  Members with no "
            "shape-bearing events fall into singleton `*_unknown_shape_*` classes and are "
            "never merged into a real class.",
            "",
        ]
    )
    lines.extend(step_class_view_lines(step_class_rows, layer_class_rows))
    lines.extend(
        [
            "",
            "## 8. Layer And Block View",
            "",
            "Each transformer layer is split into one `attention` block followed by one "
            "`ffn` or `moe` block (see `ascend_profile/knowledge/block_taxonomy.md`).  "
            "Layers that have no attention kernel are flagged as `companion_layer` so the "
            "report keeps them separate from the main forward pass.",
            "",
        ]
    )
    lines.extend(layer_block_view_lines(layer_class_rows, block_class_rows))
    lines.extend(
        [
            "",
            "## 9. Operator View",
            "",
            "Compute and HCCL operators are surfaced rank-merged so the table reflects the whole "
            "capture window.  See `ascend_profile/knowledge/communication_taxonomy.md` for "
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
            "## 10. Operator Calculation And Roofline Estimates",
            "",
        ]
    )
    lines.extend(operator_efficiency_lines(operator_eff_rows))
    lines.extend(
        [
            "",
            "## 11. Step Inventory",
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
        wall = [to_float(item.get("wall_ms")) for item in items]
        bubble = [to_float(item.get("largest_internal_bubble_ms")) for item in items]
        head_ms: list[float] = []
        main_ms: list[float] = []
        tail_ms: list[float] = []
        for item in items:
            anatomy = anatomy_by_segment_for_inv.get(str(item.get("segment_id")))
            if anatomy is None:
                continue
            head_ms.append(to_float(anatomy.get("head_wall_ms")))
            main_ms.append(to_float(anatomy.get("main_wall_ms")))
            tail_ms.append(to_float(anatomy.get("tail_wall_ms")))

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
            "## 12. Cross-Rank And Anomaly Findings",
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
            "## 13. Finding Inventory",
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
            "## 14. Evidence Chain",
            "",
            "- `report.xlsx:evidence_index` maps evidence ids to source rows, segment ids, and layer ids.",
            "- `report.xlsx:step_anatomy` is the head / main / tail / bubble per-step evidence table.",
            "- `report.xlsx:block_summary` is the per-block decomposition (attention/ffn/moe with bound + comm share).",
            "- `report.xlsx:step_class_summary`, `layer_class_summary`, `block_class_summary` carry the shape-strict class aggregates.",
            "- `report.xlsx:operator_class_summary` is the rank-merged operator view; `hccl_op_summary` and `hccl_class_summary` cover collective communication.",
            "- `report.xlsx:model_inferred_config`, `model_feature_summary`, and `model_candidate_summary` are profiling-derived model-fingerprint tables.",
            "- `report.xlsx:hardware_summary` and `hardware_theoretical_peaks` document the selected hardware denominator and CANN-derived theoretical peaks.",
            "- `report.xlsx:operator_efficiency_summary` is the shape-derived FLOPs / bytes / theoretical-MFU / sustained-roofline ranking table.",
            "- `report.xlsx:raw_kernel_index` maps normalized event ids back to original `kernel_details.csv` rows (first 200k rows streamed from `normalized_event_index.csv`, a column superset of the retired standalone raw_kernel_index.csv).",
            "- `report.xlsx:cross_rank_alignment` contains cross-rank step/operator alignment evidence.",
            "- `diagnosis_findings.json` is the machine-readable claim source for this Markdown report.",
            "- `report.xlsx:bubble_windows` rows carry per-bubble host-side soft attribution (`soft_attribution.soft_root_cause_labels`, salvaged from the retired anomaly skill's rulebook §11) when `trace_view.json` was registered for the rank.",
            "",
            "## 15. Limitations",
            "",
            "- Step and layer segmentation is inferred from structural anchors and should be audited on new model families.",
            "- Pipeline coverage may be < 100% on older CANN versions; per-stage figures are skipped for events without source columns.",
            "- Host-side root cause attribution is not asserted unless host trace evidence is present.",
            "- Missing shape fields reduce confidence for slow-rank and DP-load diagnoses.",
            "- Model fingerprint matching narrows candidates; vocab can be inferred only when lm_head/logits shapes are visible, and tensor parallelism may expose only a shard.",
            "- MFU is only a real capture-hardware metric when the selected hardware comes from profiling provenance, a collection manifest, a hardware profile, or explicit user input.",
            "- Operator FLOPs / bytes / roofline estimates are derived ranking signals, not diagnosis findings.",
        ]
    )
    # Host-trace soft-attribution status from the summarize stage: missing
    # trace_view.json or truncated retention must be visible here, not
    # just in summary_manifest.json.
    host_trace = summary_manifest.get("host_trace") or {}
    for item in host_trace.get("limitations") or []:
        if str(item).strip():
            lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


def sheet_rows(output_dir: Path, *, bundle: Mapping[str, Any] | None = None) -> dict[str, list[Mapping[str, Any]]]:
    if bundle is None:
        bundle = _load_report_bundle(output_dir)
    manifests = bundle["manifests"]
    csvs = bundle["csvs"]
    findings = bundle["findings"]
    normalize_manifest = manifests["normalize"]
    case_summary = [
        {
            "profile_root": normalize_manifest.get("profile_root"),
            "rank_count": normalize_manifest.get("rank_count"),
            "event_count": normalize_manifest.get("event_count"),
            "finding_count": len(findings),
            "finding_types": dict(Counter(str(item.get("finding_type") or "unknown") for item in findings)),
        }
    ]
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
        "rank_summary": csvs["rank_summary"],
        "step_summary": csvs["step_summary"],
        "step_anatomy": csvs["step_anatomy"],
        "step_class_summary": csvs["step_class_summary"],
        "layer_summary": csvs["layer_summary"],
        "layer_class_summary": csvs["layer_class_summary"],
        "block_summary": csvs["block_summary"],
        "block_class_summary": csvs["block_class_summary"],
        "operator_summary": csvs["operator_summary"],
        "operator_class_summary": csvs["operator_class_summary"],
        "operator_efficiency_summary": csvs["operator_efficiency_summary"],
        "model_inferred_config": csvs["model_inferred_config"],
        "model_feature_summary": csvs["model_feature_summary"],
        "model_layer_type_summary": csvs["model_layer_type_summary"],
        "model_candidate_summary": csvs["model_candidate_summary"],
        "model_context_summary": csvs["model_context_summary"],
        "model_config_overview": csvs["model_config_overview"],
        "model_parameter_estimate": csvs["model_parameter_estimate"],
        "model_kv_cache_estimate": csvs["model_kv_cache_estimate"],
        "model_config_feature_summary": csvs["model_config_feature_summary"],
        "hardware_summary": csvs["hardware_summary"],
        "hardware_theoretical_peaks": csvs["hardware_theoretical_peaks"],
        "hccl_op_summary": csvs["hccl_op_summary"],
        "hccl_class_summary": csvs["hccl_class_summary"],
        "bubble_windows": bundle["bubble_windows"],
        "wait_anchor_ops": csvs["wait_anchor_ops"],
        "aicpu_summary": csvs["aicpu_summary"],
        "cross_rank_alignment": csvs["cross_rank_alignment"],
        "diagnosis_findings": findings,
        "evidence_index": csvs["evidence_index"],
        "raw_kernel_index": bundle["raw_kernel_sheet"],
    }


def validate_evidence_chain(output_dir: Path, *, bundle: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Verify every finding can be traced to evidence rows or to an explicit
    limitation. Designed to be cheap (just file-scoped joins) so it can run
    before every report render.

    A finding must satisfy at least one of:
      * has ``evidence_ids``, and every id resolves into ``evidence_index.csv``;
      * has ``alignment_ids``, and every id resolves into ``cross_rank_alignment.csv``;
      * carries a non-empty ``limitations`` string/array;
      * is explicitly tagged ``confidence == "info"``.

    Findings that fail all four checks are returned as ``hard_errors``.
    """
    findings = bundle["findings"] if bundle is not None else finding_rows(output_dir)

    if bundle is not None:
        evidence_ids: set[str] = set()
        for row in bundle["csvs"]["evidence_index"]:
            ev_id = (row.get("evidence_id") or "").strip()
            if ev_id:
                evidence_ids.add(ev_id)
        alignment_ids: set[str] = set()
        for row in bundle["csvs"]["cross_rank_alignment"]:
            al_id = (row.get("alignment_id") or "").strip()
            if al_id:
                alignment_ids.add(al_id)
    else:
        # Standalone path: only the id columns are needed, so stream those
        # instead of materialising full row dicts.
        evidence_ids = _id_set_from_csv(output_dir / "evidence_index.csv", "evidence_id")
        alignment_ids = _id_set_from_csv(output_dir / "cross_rank_alignment.csv", "alignment_id")

    hard_errors: list[dict[str, Any]] = []
    soft_warnings: list[dict[str, Any]] = []
    checked = 0
    for finding in findings:
        checked += 1
        claim_id = finding.get("claim_id") or finding.get("finding_id") or "?"
        confidence = str(finding.get("confidence") or "").lower()
        limitations = finding.get("limitations")
        has_limitation = (
            (isinstance(limitations, str) and limitations.strip())
            or (isinstance(limitations, (list, tuple)) and any(limitations))
        )
        if confidence == "info" or has_limitation:
            continue

        ev_ids = finding.get("evidence_ids") or []
        al_ids = finding.get("alignment_ids") or []
        if not ev_ids and not al_ids:
            hard_errors.append({
                "claim_id": claim_id,
                "issue": "missing_evidence_and_alignment",
                "summary": finding.get("summary"),
                "confidence": confidence,
            })
            continue

        unknown_evidence = [e for e in ev_ids if e not in evidence_ids]
        unknown_alignment = [a for a in al_ids if a not in alignment_ids]
        if unknown_evidence or unknown_alignment:
            (hard_errors if not has_limitation else soft_warnings).append({
                "claim_id": claim_id,
                "issue": "evidence_id_not_found",
                "unknown_evidence_ids": unknown_evidence,
                "unknown_alignment_ids": unknown_alignment,
                "confidence": confidence,
            })

    return {
        "findings_checked": checked,
        "evidence_rows": len(evidence_ids),
        "alignment_rows": len(alignment_ids),
        "hard_errors": hard_errors,
        "soft_warnings": soft_warnings,
    }


def render_report(
    output_dir: Path,
    *,
    skip_html: bool = False,
    report_mode: str = "full-raw",
    skip_xlsx: bool = False,
    stage_timings: Sequence[Mapping[str, Any]] | None = None,
    html_renderer: str = "v2",
    html_single_file: bool = False,
    events=None,
) -> dict[str, Any]:
    report_dir = output_dir / "report"
    report_dir.mkdir(parents=True, exist_ok=True)
    bundle = _load_report_bundle(output_dir)
    report_id = stable_id("report", output_dir, bundle["manifests"]["normalize"].get("profile_root"))

    chain = validate_evidence_chain(output_dir, bundle=bundle)
    if chain["hard_errors"]:
        first = chain["hard_errors"][0]
        raise RuntimeError(
            "evidence chain broken for "
            f"{len(chain['hard_errors'])} finding(s); first offender: "
            f"claim_id={first.get('claim_id')} issue={first.get('issue')}. "
            "Either downgrade these findings to confidence=info, attach a "
            "non-empty `limitations` field, or fix the evidence reference."
        )

    markdown = markdown_report(output_dir, report_id, bundle=bundle)
    (report_dir / "report.md").write_text(markdown, encoding="utf-8")
    # The XLSX workbook is the heaviest report artifact (every summary CSV
    # re-encoded as sheets). ``skip_xlsx`` (fast mode) drops it entirely; the
    # manifest then reports ``xlsx_status="skipped"`` and a null sheet_map,
    # and analysis_summary.json plus the CSVs carry the same numbers.
    sheets: dict[str, list[Mapping[str, Any]]] | None = None
    if not skip_xlsx:
        sheets = sheet_rows(output_dir, bundle=bundle)
        write_xlsx(report_dir / "report.xlsx", sheets)

    # HTML report (rich, zero-dependency). Three modes:
    #   * summary  — skip entirely; stub file explains. Used for
    #                first-stage pipeline debugging where md+xlsx is
    #                enough and HTML render time would just slow the
    #                feedback loop.
    #   * full-raw — render the complete L1/L2/L3 report with raw kernel
    #                rows attached to operator cards (default).
    # ``skip_html=True`` forces summary regardless of mode.
    # Renderer choice (orthogonal to mode):
    #   * v2     — thin shell + gzipped assets/ + on-demand browser rendering
    #              (default; scales to multi-million-event captures);
    #   * legacy — the pre-v2 single-file SPA (kept as a fallback; produces
    #              very large HTML on big captures).
    # ``html_single_file`` asks v2 to embed all assets (base64+gzip) into one
    # HTML file; v2 refuses with a clear error when the estimate exceeds the
    # single-file threshold.
    html_path = report_dir / "report.html"
    html_status = "ok"
    html_error: str | None = None
    effective_mode = "summary" if skip_html else report_mode

    if effective_mode == "summary":
        html_status = "skipped"
        html_path.write_text(
            "<!doctype html><meta charset='utf-8'><title>HTML report skipped</title>"
            "<body style='font-family:sans-serif;padding:20px;background:#0d1117;color:#c9d1d9'>"
            "<h1>HTML report skipped</h1>"
            "<p>This run was invoked with <code>--skip-html</code> or "
            "<code>--report-mode summary</code>. Use <code>report.md</code> / "
            "<code>report.xlsx</code> in this directory.</p>",
            encoding="utf-8",
        )
    else:
        try:
            if html_renderer == "legacy":
                try:
                    from .html_report import build_html_report
                except ImportError:  # pragma: no cover
                    import sys as _sys
                    _sys.path.insert(0, str(Path(__file__).resolve().parent))
                    from html_report import build_html_report  # type: ignore[no-redef]
                build_html_report(output_dir, html_path, events=events)
            else:
                try:
                    from .html_report_v2 import build_html_report_v2
                except ImportError:  # pragma: no cover
                    import sys as _sys
                    _sys.path.insert(0, str(Path(__file__).resolve().parent))
                    from html_report_v2 import build_html_report_v2  # type: ignore[no-redef]
                build_html_report_v2(
                    output_dir,
                    html_path,
                    events=events,
                    single_file=html_single_file,
                )
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

    # Agent-first compact summary (analysis_summary.json). Built after the
    # HTML block so ``html_status`` is final; numbers all come from the same
    # bundle as the human reports.
    analysis_summary = build_analysis_summary(
        output_dir,
        bundle=bundle,
        html_status=html_status,
        report_mode=effective_mode,
        skip_xlsx=skip_xlsx,
        stage_timings=stage_timings,
    )
    write_json(report_dir / "analysis_summary.json", analysis_summary)

    host_trace_status = (bundle["manifests"]["summary"].get("host_trace") or {}).get("status")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "tool_version": TOOL_VERSION,
        "analysis_stage": "report",
        "created_at": utc_now(),
        "report_id": report_id,
        "output_dir": str(report_dir),
        "files": {
            "markdown": "report.md",
            "xlsx": None if skip_xlsx else "report.xlsx",
            "html": "report.html",
            "analysis_summary": "analysis_summary.json",
            "manifest": "manifest.json",
        },
        "html_status": html_status,
        "report_mode": effective_mode,
        "html_renderer": html_renderer,
        "html_single_file": bool(html_single_file) if html_renderer == "v2" else False,
        "xlsx_status": "skipped" if skip_xlsx else "ok",
        "skip_xlsx": bool(skip_xlsx),
        "host_trace_status": host_trace_status,
        "sheet_map": ({name: name for name in sheets} if sheets is not None else None),
        "claim_ids": [item.get("claim_id") for item in bundle["findings"]],
        "evidence_chain": {
            "findings_checked": chain["findings_checked"],
            "evidence_rows": chain["evidence_rows"],
            "alignment_rows": chain["alignment_rows"],
            "soft_warning_count": len(chain["soft_warnings"]),
        },
    }
    if html_error:
        manifest["html_error"] = html_error
    write_json(report_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", required=True)
    parser.add_argument("--skip-html", action="store_true")
    parser.add_argument(
        "--skip-xlsx",
        action="store_true",
        help=(
            "skip report.xlsx entirely (fast mode): the workbook is the "
            "heaviest report artifact and every number in it is also in the "
            "summary CSVs / analysis_summary.json. The manifest records "
            "xlsx_status=skipped and a null sheet_map."
        ),
    )
    parser.add_argument(
        "--report-mode",
        choices=("summary", "full-raw"),
        default="full-raw",
        help=(
            "summary: skip HTML (stub file written) — for first-stage "
            "pipeline debugging when md+xlsx is enough. "
            "full-raw: render the complete L1/L2/L3 HTML with operator "
            "cards backed by raw kernel_details rows."
        ),
    )
    parser.add_argument(
        "--html-renderer",
        choices=("v2", "legacy"),
        default="v2",
        help=(
            "v2 (default): thin-shell report.html + gzipped assets/ loaded "
            "on demand — scales to multi-million-event captures. "
            "legacy: the pre-v2 single-file SPA (fallback; very large HTML "
            "on big captures)."
        ),
    )
    parser.add_argument(
        "--html-single-file",
        action="store_true",
        help=(
            "v2 only: embed all assets (base64+gzip) into report.html so it "
            "works over file://; refused with a clear error when the "
            "estimated size exceeds the 20 MB single-file threshold."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manifest = render_report(
        Path(args.output),
        skip_html=bool(args.skip_html),
        report_mode=args.report_mode,
        skip_xlsx=bool(args.skip_xlsx),
        html_renderer=args.html_renderer,
        html_single_file=bool(args.html_single_file),
    )
    emit_stage_json({
        "stage": "report",
        "output_dir": manifest["output_dir"],
        "html_status": manifest.get("html_status"),
        "html_renderer": manifest.get("html_renderer"),
        "xlsx_status": manifest.get("xlsx_status"),
    })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
