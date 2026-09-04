#!/usr/bin/env python3
"""Server-side payload builders for the v2 HTML report.

Every function turns the already-loaded ``html_report.Bundle`` into a
plain-dict JSON payload. The v2 renderer never concatenates per-operator
HTML strings the way the legacy renderer did (22k operator cards → 191 MB);
all detail data ships as gzipped JSON assets that the browser fetches and
renders on demand.

Data semantics (union vs sum, redundant filtering, L3 representative-step
selection, bound-stage decision) are *not* re-implemented here — they call
the same ``html_report`` helpers the legacy renderer uses, so the numbers
stay identical by construction.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from ascend_profile import html_report as hr  # type: ignore
    from ascend_profile import store  # type: ignore
except ImportError:  # pragma: no cover - allow running from scripts/ directly
    import html_report as hr  # type: ignore[no-redef]
    import store  # type: ignore[no-redef]

L2_SCHEMA = "l2/v1"
L3_SCHEMA = "l3/v1"
TIMELINE_SCHEMA = "timeline/v1"
FINDINGS_SCHEMA = "findings/v1"
MANIFEST_SCHEMA = "manifest/v1"
OVERVIEW_SCHEMA = "overview/v1"

#: Synthetic L2 bucket for step_summary rows without a ``step_class_id``
#: (unclassified/partial segments). The legacy SPA renders an L2 view for
#: every step; v2 keeps that reachability via this bucket.
UNCLASSIFIED_CLASS_ID = "_unclassified"

# Bubble tracing axis: gaps below this are scheduling noise, not bubbles.
BUBBLE_MIN_US = 1.0

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


def _r(v: Any, nd: int = 2) -> Any:
    """Round floats for compact JSON; pass through everything else."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return v
    return round(f, nd)


def _f(v: Any, default: float = 0.0) -> float:
    return hr.safe_float(v, default)


# ---------------------------------------------------------------------------
# shared plan: which step classes get L3 + representative step per class
# ---------------------------------------------------------------------------


def l3_plan(b: "hr.Bundle") -> dict[str, Any]:
    """Representative-step plan shared by L2 layer routing and L3 assets.

    Selection rules are exactly ``render_l2_views`` / ``render_l3_views``:
    top-3 step classes by ``wall_ms_sum``, representative = member whose
    ``wall_ms`` is closest to the class mean, uncovered classes fall back to
    the top-1 class's representative.
    """
    classes_sorted = sorted(b.step_class, key=lambda r: _f(r["wall_ms_sum"]), reverse=True)
    rep_step_per_class: dict[str, str] = {}
    covered: set[str] = set()
    for cls in classes_sorted[:3]:
        cls_id = cls["step_class_id"]
        members = [s for s in b.step_summary if s.get("step_class_id") == cls_id]
        if not members:
            continue
        target = _f(cls["wall_ms_mean"])
        rep = min(members, key=lambda x: abs(_f(x["wall_ms"]) - target))
        rep_step_per_class[cls_id] = rep["segment_id"]
        covered.add(cls_id)
    top_class_id = classes_sorted[0]["step_class_id"] if classes_sorted else None
    return {
        "classes_sorted": classes_sorted,
        "rep_step_per_class": rep_step_per_class,
        "covered": covered,
        "top_class_id": top_class_id,
        "top1_rep_seg": rep_step_per_class.get(top_class_id) if top_class_id else None,
        "class_of_seg": {s["segment_id"]: s.get("step_class_id", "") for s in b.step_summary},
    }


def l3_target_for_step(step_row: dict, plan: dict[str, Any]) -> tuple[str | None, str]:
    """Legacy L2→L3 routing precedence for one step row.

    Returns ``(target_seg_id, kind)`` where kind ∈ self / class_rep /
    top1_fallback / none — same labels the legacy UI shows as hints.
    """
    seg_id = step_row["segment_id"]
    own_cls = step_row.get("step_class_id", "")
    own_rep = plan["rep_step_per_class"].get(own_cls)
    if own_rep == seg_id and own_rep is not None:
        return seg_id, "self"
    if own_cls in plan["covered"] and own_rep:
        return own_rep, "class_rep"
    if plan["top1_rep_seg"]:
        return plan["top1_rep_seg"], "top1_fallback"
    return None, "none"


# ---------------------------------------------------------------------------
# layer validation (analysis_summary.json 优先，缺失时自行计算)
# ---------------------------------------------------------------------------


def compute_layer_validation(root: Path, b: "hr.Bundle", analysis_summary: dict | None) -> dict[str, Any]:
    """Layer-detection validation card data.

    Primary source: ``report/analysis_summary.json`` → ``layer_validation``
    (written by the report stage; absent on first-run renders and for
    standalone html runs). Fallback: derive the same shape from
    ``rank_summary.csv`` ``layer_count_inventory`` + ``segment_manifest.json``
    ``model_context.expected_layers`` — the same inputs the summary builder
    uses. Never raises on missing inputs; degrades to status=unknown.
    """
    lv = (analysis_summary or {}).get("layer_validation")
    if isinstance(lv, dict) and lv.get("status"):
        out = dict(lv)
        out["source"] = "analysis_summary"
        return out

    inventories: dict[str, tuple[int, ...]] = {}
    for row in b.rank_summary:
        rid = str(row.get("rank_id") or "")
        raw = store.parse_jsonish(row.get("layer_count_inventory"), [])
        values = tuple(sorted({int(_f(v)) for v in (raw or []) if _f(v) > 0}))
        if rid and values:
            inventories[rid] = values
    segment_manifest = hr.load_json(root / "segment_manifest.json") or {}
    model_context = segment_manifest.get("model_context") or {}
    expected = model_context.get("expected_layers")
    try:
        expected_layers = int(expected) if expected else None
    except (TypeError, ValueError):
        expected_layers = None

    detected_min = detected_max = None
    per_rank_consistent: bool | None = None
    outliers: list[dict[str, Any]] = []
    if inventories:
        all_counts = [c for vals in inventories.values() for c in vals]
        detected_min, detected_max = min(all_counts), max(all_counts)
        tuple_counts = Counter(inventories.values())
        modal = tuple_counts.most_common(1)[0][0]
        per_rank_consistent = len(tuple_counts) == 1
        outliers = [
            {"rank_id": rid, "layer_count_inventory": list(vals)}
            for rid, vals in sorted(inventories.items())
            if vals != modal
        ][:8]
    layers_match: bool | None = None
    if expected_layers is not None and inventories:
        layers_match = any(expected_layers in vals for vals in inventories.values())

    if not inventories and expected_layers is None:
        status = "unknown"
    elif layers_match is False or per_rank_consistent is False:
        status = "degraded"
    else:
        status = "ok"
    limitations: list[str] = []
    if expected_layers is None:
        limitations.append("expected layer count unknown (no config.json / model-context layer count)")
    if not inventories:
        limitations.append("rank_summary.csv has no layer_count_inventory; detected_layers is null")
    return {
        "status": status,
        "expected_layers": expected_layers,
        "expected_source": "model_context" if expected_layers is not None else "unknown",
        "detected_layers": {"min": detected_min, "max": detected_max, "per_rank_outliers": outliers},
        "layers_match": layers_match,
        "per_rank_consistent": per_rank_consistent,
        "segmentation_mode": None,
        "confidence": model_context.get("confidence") if model_context.get("available") else None,
        "limitations": limitations,
        "source": "computed",
    }


# ---------------------------------------------------------------------------
# L1 overview
# ---------------------------------------------------------------------------


def bound_family_of_event(e: "hr.Event") -> str:
    """Coarse bound family for one event — histogram-grade version of the
    per-card decision (no raw-row ratio lookup; pipeline times only)."""
    if e.op_type == "communication":
        return "communication"
    if e.op_type == "aicpu":
        return "aicpu"
    stage = hr.pick_bound_stage(e.pipeline) if e.pipeline else ""
    return hr.STAGE_FAMILY.get(stage, "unknown") if stage else "unknown"


def _bound_family_fast(e: "hr.Event") -> str:
    """Inlined ``bound_family_of_event`` for the capture-wide histogram hot
    loop (one dict scan instead of a helper call per event)."""
    op_type = e.op_type
    if op_type == "communication":
        return "communication"
    if op_type == "aicpu":
        return "aicpu"
    pipe = e.pipeline
    if not pipe:
        return "unknown"
    best = None
    bestv = 0.0
    for k in _PIPELINE_STAGE_KEYS:
        v = pipe.get(k)
        if v is not None and v > bestv:
            best = k
            bestv = v
    return hr.STAGE_FAMILY.get(best, "unknown") if best else "unknown"


_PIPELINE_STAGE_KEYS = tuple(hr.AIC_STAGES) + tuple(hr.AIV_STAGES)


def build_overview(
    b: "hr.Bundle",
    *,
    layer_validation: dict[str, Any],
    findings_groups: list[dict[str, Any]],
    knowledge_refs: list[Any],
    analysis_summary_loaded: bool,
) -> dict[str, Any]:
    """Everything the L1 static view renders, plus the small slice the
    client-side app needs for navigation (embedded as ``window.__OVERVIEW__``).
    """
    rank_count = len(b.rank_summary)
    step_count = sum(int(_f(r["step_count"])) for r in b.rank_summary)
    total_wall = sum(_f(r["wall_ms"]) for r in b.rank_summary) / max(rank_count, 1)
    total_wall_all = sum(_f(r["wall_ms"]) for r in b.rank_summary)
    ep = hr.compute_ep_balance(b)
    comp = hr.assess_companion_run(b)

    # ---- cross-rank rows (same numbers as legacy L1 table) ----
    busy_mean = 0.0
    if b.rank_summary:
        import statistics

        busy_mean = statistics.mean(_f(r["busy_union_ms"]) for r in b.rank_summary)
    rank_rows = []
    for r in sorted(b.rank_summary, key=lambda x: _f(x["busy_union_ms"]), reverse=True):
        busy = _f(r["busy_union_ms"])
        diff = (busy - busy_mean) / busy_mean if busy_mean else 0.0
        speed = "slow" if diff > 0.30 else ("fast" if diff < -0.30 else "normal")
        wl_class, wl_label = hr.classify_workload(b, r["rank_id"])
        gmm = ep["by_rank"].get(r["rank_id"], 0.0) if ep["available"] else None
        rank_rows.append({
            "rank_id": r["rank_id"],
            "rank_short": hr.short_rank_label(r["rank_id"]),
            "step_count": int(_f(r["step_count"])),
            "wall_ms": _r(_f(r["wall_ms"])),
            "busy_ms": _r(busy),
            "busy_diff_pct": _r(diff * 100, 1),
            "gmm_ms": _r(gmm / 1000.0) if gmm is not None else None,
            "underfeed_pct": _r(_f(r["underfeed_ratio"]) * 100, 1),
            "speed": speed,
            "workload_class": wl_class,
            "workload_label": wl_label,
        })

    # ---- histograms: op_type + bound_family duration share (active events) ----
    op_type_us: dict[str, float] = defaultdict(float)
    bound_us: dict[str, float] = defaultdict(float)
    for e in b.events:
        if getattr(e, "redundant", False):
            continue
        op_type_us[e.op_type or "unknown"] += e.duration_us
        bound_us[_bound_family_fast(e)] += e.duration_us
    hist_total = sum(op_type_us.values()) or 1.0
    op_type_hist = [
        {"key": k, "ms": _r(v / 1000.0), "pct": _r(v / hist_total * 100, 2),
         "color": hr.OP_TYPE_COLOR.get(k, "#8b949e")}
        for k, v in sorted(op_type_us.items(), key=lambda kv: -kv[1])
    ]
    bound_hist = [
        {"key": k, "ms": _r(v / 1000.0), "pct": _r(v / hist_total * 100, 2),
         "color": hr.BOUND_FAMILY_COLOR.get(k, "#8b949e")}
        for k, v in sorted(bound_us.items(), key=lambda kv: -kv[1])
    ]

    # ---- class rollup tables (step / layer / block) ----
    def class_rows(rows: list, id_key: str, extra_keys: tuple[str, ...]) -> list[dict[str, Any]]:
        out = []
        for row in sorted(rows, key=lambda x: -_f(x.get("wall_ms_sum"))):
            entry: dict[str, Any] = {
                "id": row.get(id_key, ""),
                "members": int(_f(row.get("member_count"))),
                "ranks": int(_f(row.get("rank_count"))),
                "wall_ms_sum": _r(_f(row.get("wall_ms_sum"))),
                "wall_ms_mean": _r(_f(row.get("wall_ms_mean"))),
                "wall_ms_p50": _r(_f(row.get("wall_ms_p50"))),
                "wall_ms_p90": _r(_f(row.get("wall_ms_p90"))),
                "share_pct": _r(_f(row.get("wall_ms_sum")) / total_wall_all * 100, 2) if total_wall_all else None,
            }
            for key in extra_keys:
                entry[key] = row.get(key)
            out.append(entry)
        return out

    step_classes = class_rows(
        b.step_class, "step_class_id", ("step_family", "main_layer_count", "bubble_ms_mean"))
    layer_classes = class_rows(b.layer_class, "layer_class_id", ("block_kinds", "bubble_ms_mean"))
    block_classes = class_rows(
        b.block_class, "block_class_id", ("block_kind", "bound_family", "comm_share_mean"))

    # ---- per-rank step gantt data (server renders the SVG) ----
    by_rank: dict[str, list] = defaultdict(list)
    for s in b.step_summary:
        by_rank[s["rank_id"]].append(s)
    for rid in by_rank:
        by_rank[rid].sort(key=lambda x: _f(x["start_us"]))
    gantt_ranks = sorted(by_rank.keys())
    max_wall_ms = max((_f(r["wall_ms"]) for r in b.rank_summary), default=0.0)
    gantt_steps = []
    for ri, rid in enumerate(gantt_ranks):
        rank_start = min((_f(s["start_us"]) for s in by_rank[rid]), default=0.0)
        for seg in by_rank[rid]:
            wall = _f(seg["wall_ms"])
            cls_id = seg.get("step_class_id") or UNCLASSIFIED_CLASS_ID
            gantt_steps.append({
                "r": ri,
                "t0": _r((_f(seg["start_us"]) - rank_start) / 1000.0),
                "t1": _r((_f(seg["end_us"]) - rank_start) / 1000.0),
                "seg": seg["segment_id"],
                "cls": cls_id,
                "fam": seg.get("step_family", ""),
                "wall": _r(wall),
                "bubble_ms": _r(_f(seg.get("bubble_ratio")) * wall),
                "layers": int(_f(seg.get("main_layer_count"))),
                "color": hr.class_color(seg.get("step_family", ""), seg.get("step_class_id", "")),
            })
    families_present = sorted({s.get("step_family", "") for s in b.step_summary})

    findings_freq = (
        Counter(str(f.get("type") or f.get("finding_type") or "?") for f in b.findings).most_common(1)[0][0]
        if b.findings else "—"
    )

    return {
        "schema": OVERVIEW_SCHEMA,
        "kpis": {
            "rank_count": rank_count,
            "step_count": step_count,
            "avg_wall_ms": _r(total_wall),
            "ep": {
                "available": ep["available"],
                "peak_to_mean": _r(ep["peak_to_mean"], 3),
                "peak_ms": _r(ep["peak_us"] / 1000.0),
                "mean_ms": _r(ep["mean_us"] / 1000.0),
                "spread_pct": _r(ep["spread"] * 100, 1),
                "by_rank_ms": {k: _r(v / 1000.0) for k, v in ep["by_rank"].items()},
            },
            "companion": {
                "n_companion": comp["n_companion"],
                "n_total_aligned": comp["n_total_aligned"],
                "pairs": [
                    {"real": [hr.short_rank_label(x) for x in p["real_ranks"]],
                     "dummy": [hr.short_rank_label(x) for x in p["dummy_ranks"]],
                     "count": p["count"]}
                    for p in comp["companion_rank_pairs"]
                ],
            },
            "findings_count": len(b.findings),
            "findings_freq": findings_freq,
        },
        "layer_validation": layer_validation,
        "ranks": rank_rows,
        "op_type_hist": op_type_hist,
        "bound_family_hist": bound_hist,
        "step_classes": step_classes,
        "layer_classes": layer_classes,
        "block_classes": block_classes,
        "gantt": {
            "ranks": [hr.short_rank_label(r) for r in gantt_ranks],
            "rank_ids": gantt_ranks,
            "max_wall_ms": _r(max_wall_ms),
            "steps": gantt_steps,
            "families": [
                {"key": f, "label": hr.family_label(f), "color": hr.FAMILY_COLOR.get(f, "#58a6ff")}
                for f in families_present
            ],
        },
        "findings_groups": findings_groups,
        "knowledge_refs": knowledge_refs,
        "analysis_summary_loaded": analysis_summary_loaded,
    }


# ---------------------------------------------------------------------------
# findings asset
# ---------------------------------------------------------------------------


def rollup_findings(findings: list) -> list[dict[str, Any]]:
    """Group findings by (finding_type, severity, summary) — same grouping
    contract as ``analysis_summary.rollup_findings`` (kept local so the HTML
    renderer does not depend on the summary module's internals)."""
    grouped: dict[tuple[str, str, str], int] = {}
    for f in findings:
        if not isinstance(f, dict):
            continue
        key = (
            str(f.get("finding_type") or f.get("type") or "unknown"),
            str(f.get("severity") or "info"),
            str(f.get("summary") or ""),
        )
        grouped[key] = grouped.get(key, 0) + 1
    groups = [
        {"finding_type": t, "severity": s, "summary": summ, "occurrences": n}
        for (t, s, summ), n in grouped.items()
    ]
    groups.sort(key=lambda g: (_SEVERITY_ORDER.get(g["severity"], 9), -g["occurrences"], g["finding_type"]))
    return groups


def build_findings_payload(
    b: "hr.Bundle",
    root: Path,
    groups: list[dict[str, Any]],
    analysis_summary: dict | None,
) -> dict[str, Any]:
    """Full findings + evidence links + knowledge_refs for the findings panel."""
    evidence: dict[str, dict[str, Any]] = {}
    for row in hr.load_csv(root / "evidence_index.csv"):
        eid = row.get("evidence_id")
        if not eid:
            continue
        evidence[eid] = {
            "kind": row.get("kind") or "",
            "rank_id": row.get("rank_id") or "",
            "segment_id": row.get("segment_id") or "",
            "layer_id": row.get("layer_id") or "",
            "row_start": int(_f(row.get("row_start"))),
            "row_end": int(_f(row.get("row_end"))),
            "summary": row.get("summary") or "",
        }
    seg_to_class = {
        s["segment_id"]: (s.get("step_class_id") or UNCLASSIFIED_CLASS_ID)
        for s in b.step_summary
    }
    layer_index = {
        str(ls.get("layer_id")): {
            "segment_id": ls.get("segment_id") or "",
            "layer_index": ls.get("layer_index"),
            "layer_role": ls.get("layer_role") or "main",
        }
        for ls in b.layer_segments
        if ls.get("layer_id")
    }
    # knowledge_refs arrive via analysis_summary.json's findings groups
    # (placeholder today; empty → the UI section hides itself).
    refs_by_key: dict[tuple[str, str, str], list] = {}
    for g in (analysis_summary or {}).get("findings") or []:
        if not isinstance(g, dict):
            continue
        refs = g.get("knowledge_refs")
        if refs:
            refs_by_key[(str(g.get("finding_type")), str(g.get("severity")), str(g.get("summary")))] = refs
    for g in groups:
        refs = refs_by_key.get((g["finding_type"], g["severity"], g["summary"]))
        if refs:
            g["knowledge_refs"] = refs
    return {
        "schema": FINDINGS_SCHEMA,
        "groups": groups,
        "findings": b.findings,
        "evidence": evidence,
        "seg_to_class": seg_to_class,
        "layer_index": layer_index,
    }


# ---------------------------------------------------------------------------
# L2 class asset
# ---------------------------------------------------------------------------


def _kernel_rollup_rows(step_events: list, step_busy_us: float) -> tuple[list[dict[str, Any]], int]:
    rollup = hr.kernel_rollup_by_bound(step_events)
    name_to_union = hr.union_duration_us_by_name(step_events)
    rows = []
    for rec in rollup[:30]:
        union_us = name_to_union.get(rec["kernel"], rec["duration_us"])
        rows.append({
            "k": rec["kernel"],
            "ot": rec["op_type"],
            "n": rec["count"],
            "u_ms": _r(union_us / 1000.0),
            "pct": _r(union_us / step_busy_us * 100, 2) if step_busy_us > 0 else 0.0,
            "bf": rec["bound_family"],
            "bs": rec["bound_stage"],
        })
    return rows, len(rollup)


def _step_detail(
    b: "hr.Bundle",
    s: dict,
    plan: dict[str, Any],
    *,
    seg_idx_in_rank: dict[str, int],
    by_rank: dict[str, list],
) -> dict[str, Any]:
    """Per-step structure: phase split, cross-rank compare, kernel rollup,
    layer list with L3 routing. Same math as ``_render_l2_single_step``."""
    seg_id = s["segment_id"]
    rid = s["rank_id"]
    step_idx = seg_idx_in_rank.get(seg_id, 0)
    step_seg_meta = b._step_seg_by_id.get(seg_id) or {}
    split = hr.split_main_speculative_tail(b, step_seg_meta, rid)

    step_wall_ms = split["step_wall_ms"]
    step_wall_us = step_wall_ms * 1000.0
    step_busy_us = split["step_busy_us"]
    bubble_us = max(0.0, step_wall_us - step_busy_us)

    xrank = []
    for other_rid in sorted(by_rank.keys()):
        if step_idx >= len(by_rank[other_rid]):
            continue
        other = by_rank[other_rid][step_idx]
        other_wall = _f(other["wall_ms"])
        xrank.append({
            "rank": hr.short_rank_label(other_rid),
            "rank_id": other_rid,
            "wall": _r(other_wall),
            "bubble_pct": _r(_f(other.get("bubble_ratio")) * 100, 1),
            "diff_pct": _r((other_wall - _f(s["wall_ms"])) / _f(s["wall_ms"]) * 100, 1) if _f(s["wall_ms"]) > 0 else 0.0,
            "fam": other.get("step_family", ""),
            "seg": other["segment_id"],
            "cls": other.get("step_class_id") or UNCLASSIFIED_CLASS_ID,
            "self": other_rid == rid,
        })

    kernels, kernels_total = _kernel_rollup_rows(split["step_events"], step_busy_us)

    target_seg, target_kind = l3_target_for_step(s, plan)
    target_cls = plan["class_of_seg"].get(target_seg, "") if target_seg else ""

    layers = []
    layers_in_step = sorted(
        hr.layer_segments_in_step(b, rid, int(step_seg_meta.get("row_start", 0)), int(step_seg_meta.get("row_end", 0))),
        key=lambda x: x.get("row_start", 0),
    )
    for ls in layers_in_step:
        lev = hr.events_in_row_range(b.events, ls["row_start"], ls["row_end"], rid)
        lev_active = [e for e in lev if not getattr(e, "redundant", False)]
        ldur = hr.union_duration_us(lev_active)
        lay_idx = ls.get("layer_index", "?")
        role = ls.get("layer_role", "main")
        layers.append({
            "idx": lay_idx,
            "role": role,
            "comp": hr.derive_layer_composition(b, ls),
            "ms": _r(ldur / 1000.0),
            "pct": _r(ldur / step_busy_us * 100, 2) if step_busy_us > 0 else 0.0,
            "n": len(lev),
            "l3": f"{lay_idx}-{role}" if target_seg else None,
            "l3c": target_cls if target_seg else None,
            "l3k": target_kind,
        })

    return {
        "wall_ms": _r(_f(s["wall_ms"])),
        "bubble_ratio": _r(_f(s.get("bubble_ratio")), 4),
        "start_us": _r(_f(s.get("start_us"))),
        "end_us": _r(_f(s.get("end_us"))),
        "family": s.get("step_family", ""),
        "layers_count": int(_f(s.get("main_layer_count"))),
        "model": hr.guess_model_structure(b, s),
        "has_attention": str(s.get("has_attention", "")).lower() == "true",
        "has_moe": str(s.get("has_moe", "")).lower() == "true",
        "phase": {
            "head_ms": _r(split["head_us"] / 1000.0),
            "main_ms": _r(split["main_us"] / 1000.0),
            "spec_ms": _r(split["spec_us"] / 1000.0),
            "tail_ms": _r(split["tail_us"] / 1000.0),
            "bubble_ms": _r(bubble_us / 1000.0),
            "head_bubble_ms": _r(split["head_bubble_ms"]),
            "main_bubble_ms": _r(split["main_bubble_ms"]),
            "tail_bubble_ms": _r(split["tail_bubble_ms"]),
            "spec_layer_count": split["spec_layer_count"],
            "busy_ms": _r(step_busy_us / 1000.0),
        },
        "xrank": xrank,
        "kernels": kernels,
        "kernels_total": kernels_total,
        "layers": layers,
        "l3_target": {"seg": target_seg, "kind": target_kind, "cls": target_cls},
    }


def build_l2_class_payload(
    b: "hr.Bundle",
    cls: dict,
    plan: dict[str, Any],
    *,
    seg_idx_in_rank: dict[str, int],
    by_rank: dict[str, list],
    members: list | None = None,
) -> dict[str, Any]:
    """One ``assets/l2/<step_class_id>.json.gz`` payload.

    ``members`` overrides the step_summary filter — used by the synthetic
    unclassified-class bucket for steps without a ``step_class_id``.
    """
    cls_id = cls["step_class_id"]
    if members is None:
        members = [s for s in b.step_summary if s.get("step_class_id") == cls_id]
    members = sorted(members, key=lambda x: (x["rank_id"], _f(x["start_us"])))
    rep_seg = plan["rep_step_per_class"].get(cls_id)

    steps = []
    detail: dict[str, Any] = {}
    for s in members:
        seg_id = s["segment_id"]
        steps.append({
            "seg": seg_id,
            "rank": hr.short_rank_label(s["rank_id"]),
            "rank_id": s["rank_id"],
            "idx": seg_idx_in_rank.get(seg_id, 0),
            "wall": _r(_f(s["wall_ms"])),
            "bubble_pct": _r(_f(s.get("bubble_ratio")) * 100, 1),
            "fam": s.get("step_family", ""),
            "s": _r(_f(s.get("start_us"))),
            "e": _r(_f(s.get("end_us"))),
            "layers": int(_f(s.get("main_layer_count"))),
            "rep": 1 if seg_id == rep_seg else 0,
        })
        detail[seg_id] = _step_detail(b, s, plan, seg_idx_in_rank=seg_idx_in_rank, by_rank=by_rank)

    return {
        "schema": L2_SCHEMA,
        "class_id": cls_id,
        "family": cls.get("step_family", ""),
        "family_label": hr.family_label(cls.get("step_family", ""), int(_f(cls.get("main_layer_count")))),
        "main_layer_count": int(_f(cls.get("main_layer_count"))),
        "member_count": int(_f(cls.get("member_count")) or len(members)),
        "rank_count": int(_f(cls.get("rank_count"))),
        "wall_ms_sum": _r(_f(cls.get("wall_ms_sum"))),
        "wall_ms_mean": _r(_f(cls.get("wall_ms_mean"))),
        "wall_ms_p50": _r(_f(cls.get("wall_ms_p50"))),
        "wall_ms_p90": _r(_f(cls.get("wall_ms_p90"))),
        "bubble_ms_mean": _r(_f(cls.get("bubble_ms_mean"))),
        "rep_segment_id": rep_seg,
        "has_l3": cls_id in plan["covered"],
        "steps": steps,
        "step_detail": detail,
    }


# ---------------------------------------------------------------------------
# L3 layer asset (operator cards + bubble axis)
# ---------------------------------------------------------------------------


def _event_card_payload(
    e: "hr.Event",
    layer_busy_us: float,
    *,
    kernel_layer_union_us: dict,
    kernel_layer_count: Counter,
    kernel_step_union_us: dict,
    kernel_step_count: Counter,
    step_busy_us: float,
) -> dict[str, Any]:
    """JSON twin of ``html_report.render_operator_card`` — every number the
    46-field card shows, computed by the same helpers."""
    short = hr.short_op_name(e.name)
    bound, bound_ratio, basis = hr._decide_bound_stage(e)
    bound_family = hr.STAGE_FAMILY.get(bound, "unknown")

    if e.op_type == "aic":
        stage_pool = hr.AIC_STAGES
    elif e.op_type == "aiv":
        stage_pool = hr.AIV_STAGES
    else:
        stage_pool = hr.AIC_STAGES + hr.AIV_STAGES

    stages = []
    if e.op_type != "communication":
        ordered = ([bound] if bound and bound in stage_pool else []) + [s for s in stage_pool if s != bound]
        for st in ordered:
            ratio = hr._stage_ratio_value(e.raw_row, st)
            stages.append({
                "k": st,
                "us": _r(_f(e.pipeline.get(st))),
                "r": _r(ratio, 4) if ratio is not None else None,
                "fam": hr.STAGE_FAMILY.get(st, "unknown"),
                "hot": 1 if st == bound else 0,
            })

    klayer_us = (kernel_layer_union_us or {}).get(short, e.duration_us)
    klayer_n = (kernel_layer_count or {}).get(short, 1)
    kstep_us = (kernel_step_union_us or {}).get(short, e.duration_us)
    kstep_n = (kernel_step_count or {}).get(short, 1)

    extras = []
    for key in ("cube_utilization(%)", "aic_icache_miss_rate", "aiv_icache_miss_rate"):
        v = e.raw_row.get(key)
        if v in (None, "", "N/A"):
            continue
        try:
            extras.append([key, _r(float(v), 4)])
        except (TypeError, ValueError):
            continue

    raw = {
        f: e.raw_row[f]
        for f in hr.RAW_KD_FIELDS
        if f in e.raw_row and e.raw_row[f] not in (None, "", "N/A")
    }
    return {
        "bound": bound or "",
        "br": _r(bound_ratio, 4) if bound_ratio is not None else None,
        "basis": basis,
        "bf": bound_family,
        "stages": stages,
        "sp": _r(e.duration_us / layer_busy_us * 100, 2) if layer_busy_us > 0 else 0.0,
        "klp": _r(klayer_us / layer_busy_us * 100, 2) if layer_busy_us > 0 else 0.0,
        "kln": klayer_n,
        "klu": _r(klayer_us),
        "ksp": _r(kstep_us / step_busy_us * 100, 2) if step_busy_us > 0 else 0.0,
        "ksn": kstep_n,
        "ksu": _r(kstep_us),
        "lu": _r(layer_busy_us),
        "su": _r(step_busy_us),
        "ex": extras,
        "bd": str(e.raw_row.get("Block Dim") or ""),
        "mbd": str(e.raw_row.get("Mix Block Dim") or ""),
        "raw": raw,
    }


def _bubble_gaps(events: list, *, min_us: float = BUBBLE_MIN_US) -> list[dict[str, Any]]:
    """Idle gaps inside the union of event intervals (bubble tracing axis)."""
    intervals = sorted((e.start_us, e.end_us) for e in events if e.end_us > e.start_us)
    if not intervals:
        return []
    gaps = []
    cur_s, cur_e = intervals[0]
    for s, en in intervals[1:]:
        if s > cur_e:
            gap = s - cur_e
            if gap >= min_us:
                gaps.append({"s": _r(cur_e), "d": _r(gap)})
            cur_s, cur_e = s, en
        else:
            cur_e = max(cur_e, en)
    return gaps


def build_l3_layer_payload(
    b: "hr.Bundle",
    cls_id: str,
    seg_id: str,
    ls: dict,
    rank_id: str,
    *,
    step_busy_us: float,
    kernel_step_union_us: dict,
    kernel_step_count: Counter,
) -> dict[str, Any]:
    """One ``assets/l3/<step_class_id>/<layer_key>.json.gz`` payload.

    Unlike the legacy renderer there is no 200-card cap — every active event
    in the layer carries its full card payload (数据不裁); size is bounded by
    gzip instead of by culling.
    """
    lay_idx = ls.get("layer_index", "?")
    role = ls.get("layer_role", "main")
    lev = hr.events_in_row_range(b.events, ls["row_start"], ls["row_end"], rank_id)
    lev_active = [e for e in lev if not getattr(e, "redundant", False)]
    lev_active.sort(key=lambda e: e.start_us)
    layer_busy_us = hr.union_duration_us(lev_active)
    kernel_layer_union_us = hr.union_duration_us_by_name(lev_active)
    kernel_layer_count = Counter(hr.short_op_name(e.name) for e in lev_active)

    events = []
    for e in lev_active:
        events.append({
            "n": e.name,
            "s": hr.short_op_name(e.name),
            "t": e.op_type,
            "st": e.stream_id or "",
            "ts": _r(e.start_us),
            "d": _r(e.duration_us),
            "w": _r(e.wait_us),
            "tt": e.task_type,
            "card": _event_card_payload(
                e, layer_busy_us,
                kernel_layer_union_us=kernel_layer_union_us,
                kernel_layer_count=kernel_layer_count,
                kernel_step_union_us=kernel_step_union_us,
                kernel_step_count=kernel_step_count,
                step_busy_us=step_busy_us,
            ),
        })

    return {
        "schema": L3_SCHEMA,
        "class_id": cls_id,
        "rep_segment_id": seg_id,
        "layer_key": f"{lay_idx}-{role}",
        "layer_index": lay_idx,
        "role": role,
        "rank_id": rank_id,
        "rank_short": hr.short_rank_label(rank_id),
        "layer_busy_us": _r(layer_busy_us),
        "step_busy_us": _r(step_busy_us),
        "start_us": _r(_f(ls.get("start_us"))),
        "end_us": _r(_f(ls.get("end_us"))),
        "events": events,
        "bubbles": _bubble_gaps(lev_active),
    }


def build_l3_assets_for_rep(b: "hr.Bundle", cls_id: str, seg_id: str) -> dict[str, dict[str, Any]]:
    """All layer payloads for one representative step → {layer_key: payload}."""
    step_meta = b._step_seg_by_id.get(seg_id)
    rank_id = next((s["rank_id"] for s in b.step_summary if s["segment_id"] == seg_id), None)
    if not step_meta or rank_id is None:
        return {}
    step_events = hr.events_in_row_range(b.events, step_meta["row_start"], step_meta["row_end"], rank_id)
    step_events_active = [e for e in step_events if not getattr(e, "redundant", False)]
    step_busy_us = hr.union_duration_us(step_events_active)
    kernel_step_union_us = hr.union_duration_us_by_name(step_events_active)
    kernel_step_count = Counter(hr.short_op_name(e.name) for e in step_events_active)

    out = {}
    layers = sorted(
        hr.layer_segments_in_step(b, rank_id, int(step_meta["row_start"]), int(step_meta["row_end"])),
        key=lambda x: x.get("row_start", 0),
    )
    for ls in layers:
        payload = build_l3_layer_payload(
            b, cls_id, seg_id, ls, rank_id,
            step_busy_us=step_busy_us,
            kernel_step_union_us=kernel_step_union_us,
            kernel_step_count=kernel_step_count,
        )
        out[payload["layer_key"]] = payload
    return out


# ---------------------------------------------------------------------------
# timeline asset (per rank)
# ---------------------------------------------------------------------------


def build_timeline_payload(b: "hr.Bundle", rank_id: str) -> dict[str, Any]:
    """One ``assets/timeline/<rank>.json.gz`` payload.

    Events are dictionary-coded (name / stream / op_type) and stored as
    offsets from the first event start so numbers stay small and gzip
    friendly. ``flags`` bit0 = redundant (comm-shadow dedup copy).
    """
    events = [e for e in b.events if e.rank_id == rank_id]
    events.sort(key=lambda e: e.start_us)
    names: dict[str, int] = {}
    streams: dict[str, int] = {}
    op_types: dict[str, int] = {}

    t0 = events[0].start_us if events else 0.0
    rows = []
    # Inline dict-coding (no per-field helper calls) — this is the hottest
    # loop in the payload builders (3 codes × N events).
    names_get, streams_get, op_get = names.get, streams.get, op_types.get
    for e in events:
        ni = names_get(e.name)
        if ni is None:
            ni = len(names)
            names[e.name] = ni
        stream = e.stream_id or ""
        si = streams_get(stream)
        if si is None:
            si = len(streams)
            streams[stream] = si
        otype = e.op_type or "unknown"
        oi = op_get(otype)
        if oi is None:
            oi = len(op_types)
            op_types[otype] = oi
        flags = 1 if getattr(e, "redundant", False) else 0
        rows.append([
            ni,
            _r(e.start_us - t0),
            _r(e.duration_us),
            si,
            oi,
            flags,
        ])

    rank_steps = [s for s in b.step_summary if s["rank_id"] == rank_id]
    rank_steps.sort(key=lambda x: _f(x["start_us"]))
    steps = [
        {
            "seg": s["segment_id"],
            "cls": s.get("step_class_id") or UNCLASSIFIED_CLASS_ID,
            "fam": s.get("step_family", ""),
            "s": _r(_f(s["start_us"]) - t0),
            "e": _r(_f(s["end_us"]) - t0),
            "wall": _r(_f(s["wall_ms"])),
            "bub": _r(_f(s.get("bubble_ratio")) * _f(s["wall_ms"])),
            "idx": i,
        }
        for i, s in enumerate(rank_steps)
    ]
    names_list = [None] * len(names)
    for name, idx in names.items():
        names_list[idx] = name
    streams_list = [None] * len(streams)
    for stream, idx in streams.items():
        streams_list[idx] = stream
    op_types_list = [None] * len(op_types)
    for op_type, idx in op_types.items():
        op_types_list[idx] = op_type
    return {
        "schema": TIMELINE_SCHEMA,
        "rank_id": rank_id,
        "rank_short": hr.short_rank_label(rank_id),
        "t0_us": _r(t0),
        "names": names_list,
        "streams": streams_list,
        "op_types": op_types_list,
        "events": rows,
        "steps": steps,
    }


def rank_ids(b: "hr.Bundle") -> list[str]:
    return sorted({e.rank_id for e in b.events} or {r["rank_id"] for r in b.rank_summary})
