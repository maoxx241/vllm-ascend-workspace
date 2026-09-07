#!/usr/bin/env python3
"""v2 HTML report renderer — thin shell + gzipped data assets + on-demand rendering.

Public entry points:

  - ``build_html_report_v2(analysis_root, output_path, *, events=None,
    single_file=False, single_file_max_bytes=...)`` — the renderer used by
    ``report.py --html-renderer v2`` (default).
  - CLI: ``python -m ascend_profile.html_report_v2 <root> <out.html> [--single-file]``

Output layout (assets mode, the default)::

    report/
      report.html            # thin shell: inline CSS/JS + static L1 overview
      assets/
        manifest.json        # asset index: logical key → file/schema/bytes
        findings.json.gz
        l2/<step_class_id>.json.gz
        l3/<step_class_id>/<layer_key>.json.gz   # representative steps only
        timeline/<rank_id>.json.gz

The legacy single-file SPA remains available as ``--html-renderer legacy``
(``html_report.build_html_report``), untouched.

The report consumes ``report/analysis_summary.json`` when present (layer
validation + findings knowledge_refs); the file is written *after* the HTML
stage inside ``render_report``, so first-run renders fall back to computing
layer validation from layer_segments / segment_manifest with the same
inputs. Nothing here hard-depends on that file.
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

try:
    from ascend_profile import html_report as hr  # type: ignore
    from ascend_profile import store  # type: ignore
    from ascend_profile.html_report_v2 import assets, payloads, shell  # type: ignore
except ImportError:  # pragma: no cover - allow running from scripts/ directly
    import html_report as hr  # type: ignore[no-redef]
    import store  # type: ignore[no-redef]
    from html_report_v2 import assets, payloads, shell  # type: ignore[no-redef]

RENDERER_ID = "v2"


def _read_analysis_summary(report_dir: Path) -> dict[str, Any] | None:
    data = hr.load_json(report_dir / "analysis_summary.json")
    return data if isinstance(data, dict) else None


def _collect_knowledge_refs(analysis_summary: dict[str, Any] | None) -> list[Any]:
    refs: list[Any] = []
    for group in (analysis_summary or {}).get("findings") or []:
        if not isinstance(group, dict):
            continue
        for ref in group.get("knowledge_refs") or []:
            if ref not in refs:
                refs.append(ref)
    return refs


def build_html_report_v2(
    analysis_root: Path | str,
    output_path: Path | str,
    *,
    events=None,
    single_file: bool = False,
    single_file_max_bytes: int = assets.DEFAULT_SINGLE_FILE_MAX_BYTES,
) -> Path:
    """Render the v2 report for ``analysis_root`` into ``output_path``.

    Assets land in ``<output_path>.parent/assets/`` unless ``single_file``
    embeds them (base64+gzip) into the HTML itself; embedding is refused with
    :class:`assets.SingleFileTooLargeError` when the estimated final HTML
    exceeds ``single_file_max_bytes``.
    """
    root = Path(analysis_root)
    output = Path(output_path)
    report_dir = output.parent
    report_dir.mkdir(parents=True, exist_ok=True)

    b = hr.load_bundle(root, events=events)
    title = f"Ascend Profiling · {os.path.basename(str(root).rstrip('/'))}"

    analysis_summary = _read_analysis_summary(report_dir)
    layer_validation = payloads.compute_layer_validation(root, b, analysis_summary)
    findings_groups = payloads.rollup_findings(b.findings)
    knowledge_refs = _collect_knowledge_refs(analysis_summary)

    plan = payloads.l3_plan(b)
    rep_from_loader = set(hr._l3_rep_seg_ids(b))
    if rep_from_loader != set(plan["rep_step_per_class"].values()):
        # The lazy raw-row loader and the L3 asset builder must agree on the
        # representative set or operator cards lose their raw rows.
        print("  WARN: L3 rep-step selection drifted from _raw_rows_needed loader", file=sys.stderr)

    # per-rank step ordering (step_index within rank) — same as legacy L2
    by_rank: dict[str, list] = defaultdict(list)
    for s in b.step_summary:
        by_rank[s["rank_id"]].append(s)
    for rid in by_rank:
        by_rank[rid].sort(key=lambda x: hr.safe_float(x["start_us"]))
    seg_idx_in_rank = {
        s["segment_id"]: i
        for rows in by_rank.values()
        for i, s in enumerate(rows)
    }

    entries: dict[str, dict[str, Any]] = {}
    serialized: dict[str, bytes] = {}

    def emit(key: str, rel_file: str, payload: dict) -> None:
        if single_file:
            data = assets.serialize_asset(payload)
            serialized[rel_file] = data
            entries[key] = {"file": rel_file, "bytes": len(data), "schema": payload.get("schema", "")}
        else:
            entries[key] = assets.write_asset(report_dir, rel_file, payload)
        print(f"  asset {rel_file}: {entries[key]['bytes']:,} bytes", file=sys.stderr)

    emit("findings", "assets/findings.json.gz",
         payloads.build_findings_payload(b, root, findings_groups, analysis_summary))

    for cls in plan["classes_sorted"]:
        cls_id = cls["step_class_id"]
        emit(f"l2/{cls_id}", f"assets/l2/{cls_id}.json.gz",
             payloads.build_l2_class_payload(b, cls, plan,
                                             seg_idx_in_rank=seg_idx_in_rank, by_rank=by_rank))

    # Steps without a step_class_id still need an L2 view (the legacy
    # renderer emits one per step regardless of classification). They share
    # one synthetic bucket; their layer rows route to the top-1 class rep's
    # L3 via the same ``l3_target_for_step`` fallback.
    unclassified = [s for s in b.step_summary if not s.get("step_class_id")]
    if unclassified:
        payload = payloads.build_l2_class_payload(
            b,
            {"step_class_id": payloads.UNCLASSIFIED_CLASS_ID, "step_family": "",
             "member_count": len(unclassified), "rank_count": len(by_rank)},
            plan,
            seg_idx_in_rank=seg_idx_in_rank,
            by_rank=by_rank,
            members=unclassified,
        )
        payload["family_label"] = "未分类 step（无 step_class_id）"
        emit(f"l2/{payloads.UNCLASSIFIED_CLASS_ID}",
             f"assets/l2/{payloads.UNCLASSIFIED_CLASS_ID}.json.gz", payload)

    for cls_id, seg_id in plan["rep_step_per_class"].items():
        for layer_key, payload in payloads.build_l3_assets_for_rep(b, cls_id, seg_id).items():
            emit(f"l3/{cls_id}/{layer_key}", f"assets/l3/{cls_id}/{layer_key}.json.gz", payload)

    for rid in payloads.rank_ids(b):
        emit(f"timeline/{rid}", f"assets/timeline/{rid}.json.gz",
             payloads.build_timeline_payload(b, rid))

    manifest = assets.build_manifest(
        title=title,
        renderer=RENDERER_ID,
        generated_at=store.utc_now(),
        entries=entries,
        single_file=single_file,
    )
    if not single_file:
        assets.write_manifest(report_dir, manifest)

    overview = payloads.build_overview(
        b,
        layer_validation=layer_validation,
        findings_groups=findings_groups,
        knowledge_refs=knowledge_refs,
        analysis_summary_loaded=analysis_summary is not None,
    )
    overview_html = shell.render_overview_html(overview)
    # Small JS-side slice: navigation + status only (the visible L1 numbers
    # are already in the static HTML above).
    overview_js = {
        "kpis": overview["kpis"],
        "layer_validation": layer_validation,
        "step_classes": [
            {"class_id": c["id"], "family": c.get("step_family") or "",
             "members": c["members"], "wall_ms_sum": c["wall_ms_sum"]}
            for c in overview["step_classes"]
        ],
        "findings_group_count": len(findings_groups),
        "single_file": single_file,
    }

    if single_file:
        draft = shell.render_shell(
            title=title, overview_html=overview_html, overview_data=overview_js,
            manifest=manifest, field_docs=hr.FIELD_DOC, embedded_assets=None)
        estimated = assets.estimate_single_file_bytes(len(draft.encode("utf-8")), serialized)
        if estimated > single_file_max_bytes:
            raise assets.SingleFileTooLargeError(estimated, single_file_max_bytes)
        html_out = shell.render_shell(
            title=title, overview_html=overview_html, overview_data=overview_js,
            manifest=manifest, field_docs=hr.FIELD_DOC,
            embedded_assets=assets.embed_assets(serialized))
    else:
        html_out = shell.render_shell(
            title=title, overview_html=overview_html, overview_data=overview_js,
            manifest=manifest, field_docs=hr.FIELD_DOC, embedded_assets=None)

    output.write_text(html_out, encoding="utf-8")
    return output.resolve()


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("analysis_root")
    parser.add_argument("output_html")
    parser.add_argument("--single-file", action="store_true",
                        help="embed all assets (base64+gzip) into one HTML file")
    parser.add_argument("--single-file-max-mb", type=float, default=20.0,
                        help="refuse single-file output above this estimated size (default: 20 MB)")
    args = parser.parse_args(argv)

    path = build_html_report_v2(
        Path(args.analysis_root),
        Path(args.output_html),
        single_file=bool(args.single_file),
        single_file_max_bytes=int(args.single_file_max_mb * 1024 * 1024),
    )
    size_kb = path.stat().st_size / 1024
    print(f"wrote {path} ({size_kb:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
