#!/usr/bin/env python3
"""Agent-first compact analysis summary (``report/analysis_summary.json``).

The Markdown/XLSX/HTML reports are built for humans; this module distils the
same report-stage bundle into one small JSON document (target <= 50 KB) that
an agent can load in a single read to answer "what did the capture look like,
was layer segmentation trustworthy, where did the time go, and what should I
look at first?".

Every number is re-derived from artifacts the pipeline already wrote (the
report bundle loaded by ``report._load_report_bundle`` plus the stage
manifests, ``analysis_context.json``, ``source_index.json`` and
``diagnosis_findings.json``) -- nothing is recomputed from raw events and no
new conclusions are invented here. Fields whose inputs are missing are
emitted as ``null`` with an explanation appended to ``limitations``.

Share denominators (documented so consumers do not have to guess):
  * ``bubble_share_of_wall`` -- sum of per-step ``underfeed_ms`` over the sum
    of per-step ``wall_ms`` (step-scoped; both from ``step_summary.csv``).
  * ``comm_share_of_wall`` and every ``share_of_wall`` under
    ``top_operator_classes`` -- rank-merged durations from
    ``operator_class_summary.csv`` over the sum of per-rank ``wall_ms`` from
    ``rank_summary.csv`` (capture-scoped, the same denominator
    ``sweep.cross_root_rollup_rows`` uses for ``hccl_share_of_wall``).
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .common import TOOL_VERSION, read_json, utc_now
    from .store import parse_jsonish, to_float, to_int
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import TOOL_VERSION, read_json, utc_now  # type: ignore[no-redef]
    from store import parse_jsonish, to_float, to_int  # type: ignore[no-redef]


ANALYSIS_SUMMARY_SCHEMA_VERSION = 1
TOOL_NAME = "ascend-profiling-analysis"

# Findings rollup caps: at most this many groups are embedded; the rest is
# accounted for in ``finding_counts.rollup_overflow``.
ROLLUP_GROUP_LIMIT = 50
ROLLUP_RANK_LIMIT = 8
ROLLUP_SEGMENT_SAMPLE_LIMIT = 4
ROLLUP_EVIDENCE_SAMPLE_LIMIT = 4
ROLLUP_LIMITATION_LIMIT = 3

# Digit runs in finding summaries are per-instance values (durations, skews);
# long hex ids (segment/claim ids) are per-instance too. Normalizing both
# collapses one logical issue into a single rollup group.
_SUMMARY_NUM_RE = re.compile(r"[0-9a-f]{12,}|\d+(?:\.\d+)?")

TOP_STEP_CLASS_LIMIT = 5
TOP_OPERATOR_CLASS_LIMIT = 10
TOP_HCCL_KIND_LIMIT = 5
MODEL_CANDIDATE_LIMIT = 3
PER_RANK_OUTLIER_LIMIT = 8

_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
_CONFIDENCE_ORDER = {"high": 3, "medium": 2, "low": 1, "info": 0}

# Operator classes with these op_types are not compute hot-spots (they are
# communication / host-side / unclassified work); same filter as
# ``report.operator_view_lines`` so both views agree on "top compute ops".
_NON_COMPUTE_OP_TYPES = {"communication", "mix_comm_aiv", "aicpu", "dsa", "unknown"}
_COMM_OP_TYPES = {"communication", "mix_comm_aiv"}

# Segmentation strategy modes (segment.py) mapped onto the compact public
# enum used in ``layer_validation.segmentation_mode``.
_SEGMENTATION_MODE_MAP = {
    "model_guided": "model_guided",
    "knowledge_uniform_period": "uniform",
    "exact_cover_knowledge_miss": "exact_cover_knowledge_miss",
}


def _quantile_nearest(values: Sequence[float], q: float) -> float:
    """Nearest-rank quantile, mirroring ``report._quantile``.

    Kept as a local copy (instead of importing ``report``) so this module
    stays importable from ``report`` without an import cycle.
    """
    ordered = sorted(values)
    if not ordered:
        return 0.0
    idx = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * q))))
    return ordered[idx]


def _mean(values: Sequence[float]) -> float:
    return (sum(values) / len(values)) if values else 0.0


def _round6(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def _identity(
    output_dir: Path,
    normalize_manifest: Mapping[str, Any],
    candidate_rows: Sequence[Mapping[str, Any]],
    limitations: list[str],
) -> dict[str, Any]:
    rank_summaries = [
        item for item in (normalize_manifest.get("rank_summaries") or []) if isinstance(item, Mapping)
    ]
    starts = [to_float(item.get("start_us")) for item in rank_summaries if item.get("start_us") not in (None, "")]
    ends = [to_float(item.get("end_us")) for item in rank_summaries if item.get("end_us") not in (None, "")]
    if starts and ends:
        capture = {
            "start_us": min(starts),
            "end_us": max(ends),
            "wall_ms": round((max(ends) - min(starts)) / 1000.0, 6),
        }
    else:
        capture = {"start_us": None, "end_us": None, "wall_ms": None}
        limitations.append("normalize_manifest.rank_summaries missing start/end; identity.capture is null")

    source_index = read_json(output_dir / "source_index.json", default={}) or {}
    kernel_kinds = {
        str(source.get("kind"))
        for source in (source_index.get("sources") or [])
        if isinstance(source, Mapping)
        and str(source.get("kind")) in {"kernel_details_csv", "kernel_details_db"}
    }
    if kernel_kinds == {"kernel_details_csv"}:
        source_kind: str | None = "csv"
    elif kernel_kinds == {"kernel_details_db"}:
        source_kind = "db"
    elif kernel_kinds:
        source_kind = "mixed"
    else:
        source_kind = None
        limitations.append("source_index.json missing or has no kernel source; identity.source is null")

    analysis_context = read_json(output_dir / "analysis_context.json", default={}) or {}
    if not analysis_context:
        limitations.append("analysis_context.json missing; identity.model.model_id/config_path are null")

    candidate_names = [
        str(row.get("model_name"))
        for row in sorted(candidate_rows, key=lambda row: -to_float(row.get("score")))
        if row.get("model_name")
    ][:MODEL_CANDIDATE_LIMIT]

    return {
        "profile_root": normalize_manifest.get("profile_root"),
        "rank_count": normalize_manifest.get("rank_count"),
        "event_count": normalize_manifest.get("event_count"),
        "capture": capture,
        "source": source_kind,
        "model": {
            "model_id": analysis_context.get("model_id"),
            "config_path": analysis_context.get("model_config"),
            "candidate_names": candidate_names,
        },
    }


# ---------------------------------------------------------------------------
# layer validation
# ---------------------------------------------------------------------------


def _config_num_layers(overview_rows: Sequence[Mapping[str, Any]]) -> int | None:
    """``num_hidden_layers`` from a user-supplied config.json.

    ``summarize.model_config_insights`` projects the config into
    ``model_config_overview.csv`` key/value rows; ``num_layers`` is the key
    it uses for ``num_hidden_layers`` / ``n_layers``.
    """
    for row in overview_rows:
        if str(row.get("key")) == "num_layers":
            value = to_int(row.get("value"), default=0)
            return value if value > 0 else None
    return None


def _model_context_source_bucket(source: str) -> str:
    if source.startswith("model_fingerprint_catalog"):
        return "knowledge"
    if source.startswith("external_model_config"):
        # config.json fetched from a hub by the model-context resolver.
        return "config"
    if source and source != "none":
        return "fingerprint"
    return "unknown"


def _layer_validation(
    output_dir: Path,
    bundle: Mapping[str, Any],
    limitations: list[str],
) -> dict[str, Any]:
    segment_manifest = bundle["manifests"]["segment"]
    analysis_context = read_json(output_dir / "analysis_context.json", default={}) or {}
    model_context = segment_manifest.get("model_context")
    if not isinstance(model_context, Mapping) or not model_context:
        model_context = read_json(output_dir / "segment_model_context.json", default={}) or {}

    ctx_limitations = [str(item) for item in (model_context.get("limitations") or []) if str(item).strip()]
    lv_limitations: list[str] = list(ctx_limitations)
    generated_notes: list[str] = []

    # Expected layer count: user-supplied config.json wins over the
    # segment-stage model context (fingerprint catalog / operator match).
    expected_layers: int | None = None
    expected_source = "unknown"
    config_layers = _config_num_layers(bundle["csvs"]["model_config_overview"])
    if analysis_context.get("model_config") and config_layers is not None:
        expected_layers = config_layers
        expected_source = "config"
    else:
        ctx_layers = to_int(model_context.get("expected_layers"), default=0)
        if ctx_layers > 0:
            expected_layers = ctx_layers
            expected_source = _model_context_source_bucket(str(model_context.get("source") or ""))
    if expected_layers is None:
        generated_notes.append("expected layer count unknown (no config.json and no model-context layer count)")
        lv_limitations.append(generated_notes[-1])

    # Detected per-rank main layer counts. ``rank_summary.csv`` carries the
    # segment-stage inventory (main_layer_count over complete step segments,
    # so companion layers and unclassified islands are already excluded).
    inventories: dict[str, tuple[int, ...]] = {}
    for row in bundle["csvs"]["rank_summary"]:
        rank_id = str(row.get("rank_id") or "")
        raw = parse_jsonish(row.get("layer_count_inventory"), [])
        values = tuple(sorted({to_int(item, default=0) for item in (raw or []) if to_int(item, default=0) > 0}))
        if rank_id and values:
            inventories[rank_id] = values
    if inventories:
        all_counts = [count for values in inventories.values() for count in values]
        detected_min: int | None = min(all_counts)
        detected_max: int | None = max(all_counts)
    else:
        detected_min = detected_max = None
        generated_notes.append("rank_summary.csv has no layer_count_inventory; detected_layers is null")
        lv_limitations.append(generated_notes[-1])

    per_rank_consistent: bool | None = None
    outliers: list[dict[str, Any]] = []
    if inventories:
        tuple_counts = Counter(inventories.values())
        modal_inventory = tuple_counts.most_common(1)[0][0]
        per_rank_consistent = len(tuple_counts) == 1
        outliers = [
            {"rank_id": rank_id, "layer_count_inventory": list(values)}
            for rank_id, values in sorted(inventories.items())
            if values != modal_inventory
        ][:PER_RANK_OUTLIER_LIMIT]

    if expected_layers is None or not inventories:
        layers_match: bool | None = None
    else:
        layers_match = any(expected_layers in values for values in inventories.values())

    modes = {
        _SEGMENTATION_MODE_MAP.get(
            str((rank.get("segmentation_strategy") or {}).get("mode") or ""),
            str((rank.get("segmentation_strategy") or {}).get("mode") or ""),
        )
        for rank in (segment_manifest.get("rank_summaries") or [])
        if isinstance(rank, Mapping) and (rank.get("segmentation_strategy") or {}).get("mode")
    }
    modes.discard("")
    if len(modes) == 1:
        segmentation_mode: str | None = next(iter(modes))
    elif modes:
        segmentation_mode = "mixed"
    else:
        segmentation_mode = None
        generated_notes.append("segment_manifest.rank_summaries missing segmentation_strategy; segmentation_mode is null")
        lv_limitations.append(generated_notes[-1])

    confidence = model_context.get("confidence") if model_context.get("available") else None
    if confidence is None:
        generated_notes.append("model context confidence unavailable")
        lv_limitations.append(generated_notes[-1])

    if not inventories and expected_layers is None:
        status = "unknown"
    elif (
        "exact_cover_knowledge_miss" in modes
        or layers_match is False
        or per_rank_consistent is False
    ):
        status = "degraded"
    else:
        status = "ok"

    limitations.extend(f"layer_validation: {item}" for item in generated_notes)
    return {
        "status": status,
        "expected_layers": expected_layers,
        "expected_source": expected_source,
        "detected_layers": {
            "min": detected_min,
            "max": detected_max,
            "per_rank_outliers": outliers,
        },
        "layers_match": layers_match,
        "per_rank_consistent": per_rank_consistent,
        "segmentation_mode": segmentation_mode,
        "confidence": confidence,
        "limitations": lv_limitations,
    }


# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------


def _kpis(bundle: Mapping[str, Any], limitations: list[str]) -> dict[str, Any]:
    csvs = bundle["csvs"]
    step_rows = [row for row in csvs["step_summary"] if row.get("segment_type") == "step"]
    if step_rows:
        wall = [to_float(row.get("wall_ms")) for row in step_rows]
        step_wall_ms: dict[str, Any] | None = {
            "count": len(wall),
            "mean": round(_mean(wall), 6),
            "p50": round(_quantile_nearest(wall, 0.5), 6),
            "p90": round(_quantile_nearest(wall, 0.9), 6),
        }
        step_wall_total = sum(wall)
        underfeed_total = sum(to_float(row.get("underfeed_ms")) for row in step_rows)
        bubble_share: float | None = round(underfeed_total / step_wall_total, 6) if step_wall_total > 0 else None
    else:
        step_wall_ms = None
        bubble_share = None
        limitations.append("step_summary.csv has no complete step rows; step_wall_ms and bubble_share_of_wall are null")

    anatomy_rows = csvs["step_anatomy"]
    if anatomy_rows:
        step_anatomy_ms_mean: dict[str, Any] | None = {
            "head": round(_mean([to_float(row.get("head_wall_ms")) for row in anatomy_rows]), 6),
            "main": round(_mean([to_float(row.get("main_wall_ms")) for row in anatomy_rows]), 6),
            "tail": round(_mean([to_float(row.get("tail_wall_ms")) for row in anatomy_rows]), 6),
            "bubble": round(_mean([to_float(row.get("step_bubble_ms")) for row in anatomy_rows]), 6),
        }
    else:
        step_anatomy_ms_mean = None
        limitations.append("step_anatomy.csv missing; step_anatomy_ms_mean is null")

    rank_wall_total = sum(to_float(row.get("wall_ms")) for row in csvs["rank_summary"])
    if rank_wall_total <= 0:
        rank_wall_total = 0.0
        limitations.append("rank_summary.csv missing wall_ms; comm/operator share_of_wall denominators unavailable")

    operator_rows = csvs["operator_class_summary"]
    family_wall: dict[str, float] = {}
    for row in operator_rows:
        family = str(row.get("bound_family") or "unknown")
        family_wall[family] = family_wall.get(family, 0.0) + to_float(row.get("duration_sum_us")) / 1000.0
    bound_family_wall_ms = {family: round(value, 6) for family, value in sorted(family_wall.items())} or None
    if bound_family_wall_ms is None:
        limitations.append("operator_class_summary.csv missing; bound_family_wall_ms and top_operator_classes are null")

    comm_ms = sum(
        to_float(row.get("duration_sum_us")) / 1000.0
        for row in operator_rows
        if str(row.get("op_type") or "") in _COMM_OP_TYPES
    )
    comm_share: float | None = round(comm_ms / rank_wall_total, 6) if rank_wall_total > 0 else None

    step_class_scored = sorted(
        csvs["step_class_summary"],
        key=lambda row: -(to_float(row.get("wall_ms_mean")) * to_float(row.get("member_count"))),
    )
    top_step_classes = [
        {
            "class_id": row.get("step_class_id"),
            "members": to_int(row.get("member_count")),
            "wall_ms_mean": round(to_float(row.get("wall_ms_mean")), 6),
            "bubble_ratio_mean": round(to_float(row.get("bubble_ratio_mean")), 6),
        }
        for row in step_class_scored[:TOP_STEP_CLASS_LIMIT]
    ]

    compute_rows = [
        row
        for row in operator_rows
        if str(row.get("op_type") or "") not in _NON_COMPUTE_OP_TYPES
    ]
    compute_rows.sort(key=lambda row: -to_float(row.get("duration_sum_us")))
    top_operator_classes = [
        {
            "name": row.get("name"),
            "duration_ms_sum": round(to_float(row.get("duration_sum_us")) / 1000.0, 6),
            "share_of_wall": round(to_float(row.get("duration_sum_us")) / 1000.0 / rank_wall_total, 6)
            if rank_wall_total > 0
            else None,
            "bound_family": row.get("bound_family"),
        }
        for row in compute_rows[:TOP_OPERATOR_CLASS_LIMIT]
    ]

    hccl_rows = sorted(csvs["hccl_class_summary"], key=lambda row: -to_float(row.get("duration_sum_us")))
    top_hccl_kinds = [
        {
            "kind": row.get("hccl_op_kind"),
            "duration_ms_sum": round(to_float(row.get("duration_sum_us")) / 1000.0, 6),
            "rank_skew_ratio": round(to_float(row.get("rank_skew_ratio")), 6),
        }
        for row in hccl_rows[:TOP_HCCL_KIND_LIMIT]
    ]

    return {
        "step_wall_ms": step_wall_ms,
        "step_anatomy_ms_mean": step_anatomy_ms_mean,
        "bubble_share_of_wall": bubble_share,
        "comm_share_of_wall": comm_share,
        "bound_family_wall_ms": bound_family_wall_ms,
        "top_step_classes": top_step_classes,
        "top_operator_classes": top_operator_classes,
        "top_hccl_kinds": top_hccl_kinds,
    }


# ---------------------------------------------------------------------------
# findings rollup
# ---------------------------------------------------------------------------


def _finding_limitations(finding: Mapping[str, Any]) -> list[str]:
    raw = finding.get("limitations")
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw if str(item).strip()]
    return []


def rollup_findings(
    findings: Sequence[Mapping[str, Any]],
    *,
    limit: int = ROLLUP_GROUP_LIMIT,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Group findings by ``(finding_type, severity, normalized_summary)``.

    Summaries carry per-instance interpolated numbers (durations, skews),
    which would fragment one logical issue into hundreds of groups (measured
    on dsv3.1 TP8: 5114 ``communication_collective_slow`` findings -> 160
    groups). For grouping we normalize digit runs to ``#``; the displayed
    ``summary`` stays the first member's verbatim text, with
    ``summary_variants`` counting distinct raw summaries in the group.

    Returns ``(groups, counts)`` where ``groups`` is sorted by severity
    (critical > high > medium > low > info) then occurrence count and capped
    at ``limit`` entries; ``counts`` carries the un-grouped totals plus
    ``rollup_groups`` (total group count) and ``rollup_overflow`` (groups not
    embedded because of the cap).
    """
    grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for finding in findings:
        raw_summary = str(finding.get("summary") or "")
        key = (
            str(finding.get("finding_type") or "unknown"),
            str(finding.get("severity") or "info"),
            _SUMMARY_NUM_RE.sub("#", raw_summary),
        )
        grouped.setdefault(key, []).append(finding)

    groups: list[dict[str, Any]] = []
    for (finding_type, severity, _norm_summary), members in grouped.items():
        rank_ids: list[str] = []
        segment_ids: list[str] = []
        evidence_sample: list[str] = []
        best_confidence: str | None = None
        group_limitations: list[str] = []
        summary_variants = len({str(member.get("summary") or "") for member in members})
        for member in members:
            for rank_id in member.get("rank_ids") or []:
                text = str(rank_id)
                if text and text not in rank_ids and len(rank_ids) < ROLLUP_RANK_LIMIT:
                    rank_ids.append(text)
            metrics = member.get("metrics")
            if isinstance(metrics, Mapping):
                segment_id = str(metrics.get("segment_id") or "")
                if segment_id and segment_id not in segment_ids and len(segment_ids) < ROLLUP_SEGMENT_SAMPLE_LIMIT:
                    segment_ids.append(segment_id)
            for ref in list(member.get("evidence_ids") or []) + list(member.get("alignment_ids") or []):
                text = str(ref)
                if text and text not in evidence_sample and len(evidence_sample) < ROLLUP_EVIDENCE_SAMPLE_LIMIT:
                    evidence_sample.append(text)
            confidence = str(member.get("confidence") or "")
            if confidence and (
                best_confidence is None
                or _CONFIDENCE_ORDER.get(confidence, -1) > _CONFIDENCE_ORDER.get(best_confidence, -1)
            ):
                best_confidence = confidence
            for item in _finding_limitations(member):
                if item not in group_limitations and len(group_limitations) < ROLLUP_LIMITATION_LIMIT:
                    group_limitations.append(item)
        groups.append(
            {
                "finding_type": finding_type,
                "severity": severity,
                "occurrences": len(members),
                # Representative verbatim summary (first member); the group
                # may fold several numeric variants of the same template.
                "summary": str(members[0].get("summary") or ""),
                "summary_variants": summary_variants,
                "affected": {"rank_ids": rank_ids, "segment_ids": segment_ids},
                "evidence_sample": evidence_sample,
                "confidence": best_confidence,
                "limitations": group_limitations,
                # Placeholder: enriched by the wrapper in a later phase.
                "knowledge_refs": [],
            }
        )

    groups.sort(
        key=lambda item: (
            _SEVERITY_ORDER.get(str(item.get("severity")), 9),
            -int(item.get("occurrences") or 0),
            str(item.get("finding_type")),
        )
    )
    overflow = max(0, len(groups) - max(limit, 0))
    counts = {
        "total": len(findings),
        "by_type": dict(Counter(str(item.get("finding_type") or "unknown") for item in findings)),
        "by_severity": dict(Counter(str(item.get("severity") or "info") for item in findings)),
        "rollup_groups": len(groups),
        "rollup_overflow": overflow,
    }
    return groups[: max(limit, 0)], counts


# ---------------------------------------------------------------------------
# model / hardware briefs
# ---------------------------------------------------------------------------


def _model_brief(bundle: Mapping[str, Any]) -> dict[str, Any]:
    csvs = bundle["csvs"]
    # summary_manifest carries the union of profile-observed features; the
    # per-feature CSV only lists features with dedicated evidence rows.
    manifest_features = (bundle["manifests"]["summary"].get("model_insights") or {}).get("features") or []
    features: list[str] = []
    for feature in list(manifest_features) + [row.get("feature") for row in csvs["model_feature_summary"]]:
        text = str(feature or "")
        if text and text not in features:
            features.append(text)
    inferred_layers: int | None = None
    for row in csvs["model_inferred_config"]:
        if str(row.get("field")) == "num_hidden_layers":
            value = to_int(row.get("inferred_value"), default=0)
            inferred_layers = value if value > 0 else None
            break
    candidate_names = [
        str(row.get("model_name"))
        for row in sorted(csvs["model_candidate_summary"], key=lambda row: -to_float(row.get("score")))
        if row.get("model_name")
    ][:MODEL_CANDIDATE_LIMIT]
    return {
        "features": features,
        "inferred_layers": inferred_layers,
        "candidate_names": candidate_names,
    }


def _hardware_brief(bundle: Mapping[str, Any]) -> dict[str, Any]:
    summary = {
        str(row.get("key")): row.get("value")
        for row in bundle["csvs"]["hardware_summary"]
        if row.get("key") not in (None, "")
    }
    hardware_model = summary.get("hardware_model") or None
    peaks = []
    for key, label in (
        ("fp16_tflops_theoretical", "fp16_tflops"),
        ("bf16_tflops_theoretical", "bf16_tflops"),
        ("int8_tops_theoretical", "int8_tops"),
    ):
        value = summary.get(key)
        if value not in (None, ""):
            peaks.append(f"{label}={value}")
    if peaks:
        source = summary.get("theoretical_peak_source") or "unknown"
        note = "theoretical peaks " + ", ".join(peaks) + f" (source={source})"
    else:
        note = None
    return {"hardware_model": hardware_model, "theoretical_peaks_note": note}


# ---------------------------------------------------------------------------
# top-level builder
# ---------------------------------------------------------------------------


def _fallback_stage_timings(output_dir: Path) -> list[dict[str, Any]] | None:
    """Stage timings from a previous full-pipeline ``manifest.json``.

    Used when ``render_report`` runs standalone (``--only-stage report`` or
    the report CLI): the top-level manifest from the earlier run is still on
    disk. During a fresh full-pipeline run the timings are handed in
    directly by ``analyze.analyze_profile`` instead.
    """
    try:
        manifest = read_json(output_dir / "manifest.json", default={}) or {}
    except Exception:  # noqa: BLE001 - malformed manifest must not break reporting
        return None
    timings = manifest.get("stage_timings")
    return list(timings) if isinstance(timings, list) else None


def build_analysis_summary(
    output_dir: Path,
    *,
    bundle: Mapping[str, Any] | None = None,
    html_status: str | None = None,
    report_mode: str | None = None,
    skip_xlsx: bool = False,
    stage_timings: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the compact agent-first summary for one analysis output dir.

    ``bundle`` is the already-loaded report bundle (``report`` passes its
    own); standalone callers may omit it, in which case it is loaded lazily
    here to avoid a module-level import cycle with ``report``.
    """
    output_dir = Path(output_dir)
    if bundle is None:
        try:
            from .report import _load_report_bundle
        except ImportError:  # pragma: no cover
            import sys

            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from report import _load_report_bundle  # type: ignore[no-redef]
        bundle = _load_report_bundle(output_dir)

    limitations: list[str] = []
    normalize_manifest = bundle["manifests"]["normalize"]
    summary_manifest = bundle["manifests"]["summary"]
    findings = [item for item in bundle["findings"] if isinstance(item, Mapping)]

    identity = _identity(output_dir, normalize_manifest, bundle["csvs"]["model_candidate_summary"], limitations)
    layer_validation = _layer_validation(output_dir, bundle, limitations)
    kpis = _kpis(bundle, limitations)
    groups, finding_counts = rollup_findings(findings)
    model_brief = _model_brief(bundle)
    hardware_brief = _hardware_brief(bundle)
    if hardware_brief["hardware_model"] is None:
        limitations.append("hardware_summary.csv missing; hardware_brief is null")

    host_trace = summary_manifest.get("host_trace") or {}
    if host_trace.get("status") == "skipped":
        limitations.append(
            "host-trace bubble attribution skipped (--skip-host-trace); bubble soft_attribution is null"
        )
    if skip_xlsx:
        limitations.append("report.xlsx skipped (--skip-xlsx); use the CSV artifacts and analysis_summary.json")

    timings = list(stage_timings) if stage_timings is not None else _fallback_stage_timings(output_dir)
    if timings is None:
        limitations.append("stage_timings unavailable (no in-run timings and no prior manifest.json)")

    return {
        "schema_version": ANALYSIS_SUMMARY_SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "generated_at": utc_now(),
        "identity": identity,
        "layer_validation": layer_validation,
        "kpis": kpis,
        "findings": groups,
        "finding_counts": finding_counts,
        "model_brief": model_brief,
        "hardware_brief": hardware_brief,
        "artifacts": {
            "report_md": "report/report.md",
            "report_html": "report/report.html" if html_status == "ok" else None,
            "analysis_summary": "report/analysis_summary.json",
            "diagnosis_findings": "diagnosis_findings.json",
            "output_dir": str(output_dir),
        },
        "report_mode": report_mode,
        "stage_timings": timings,
        "limitations": limitations,
    }
