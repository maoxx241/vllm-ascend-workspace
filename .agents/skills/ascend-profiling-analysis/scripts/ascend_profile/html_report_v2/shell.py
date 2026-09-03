#!/usr/bin/env python3
"""Thin-shell HTML assembly for the v2 report.

``report.html`` carries only: inline CSS/JS (vanilla, zero dependency), the
asset manifest, a small overview JSON, and the *static* L1 overview view.
Everything heavier (per-class L2, per-layer L3 operator cards, per-rank
timelines, findings detail) lives in ``assets/*.json.gz`` and is fetched +
inflated + rendered by the browser on demand — see ``app.js``.

First-paint payload target: <= 1 MB.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

try:
    from ascend_profile import html_report as hr  # type: ignore
except ImportError:  # pragma: no cover
    import html_report as hr  # type: ignore[no-redef]

_PKG_DIR = Path(__file__).resolve().parent

SEVERITY_BADGE = {
    "critical": "b-danger",
    "high": "b-danger",
    "medium": "b-warn",
    "low": "b-success",
    "info": "b-info",
}


def _esc(v: Any) -> str:
    return html.escape(str(v))


def _json_script(value: Any) -> str:
    """JSON for inline <script> embedding (</script>-safe)."""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _fmt_ms(v: Any) -> str:
    return hr.fmt_ms(v)


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge {cls}">{_esc(text)}</span>'


# ---------------------------------------------------------------------------
# L1 static sections
# ---------------------------------------------------------------------------


def _kpi_strip(ov: dict) -> str:
    kpi = ov["kpis"]
    ep = kpi["ep"]
    comp = kpi["companion"]
    lv = ov["layer_validation"]

    if ep["available"]:
        ep_val = f"{ep['peak_to_mean']:.2f}×"
        ep_color = "var(--danger)" if ep["peak_to_mean"] >= 1.10 else "var(--success)"
        ep_sub = f"peak {ep['peak_ms']:.1f} ms / mean {ep['mean_ms']:.1f} ms"
    else:
        ep_val, ep_color, ep_sub = "—", "var(--muted)", "无 GroupedMatmul 事件"

    comp_color = "var(--warn)" if comp["n_companion"] > 0 else "var(--success)"
    comp_msg = "存在 real ↔ dummy 错位" if comp["n_companion"] > 0 else "所有 rank 同步"

    lv_status = lv.get("status", "unknown")
    lv_badge = {
        "ok": _badge("OK", "b-success"),
        "degraded": _badge("Degraded", "b-warn"),
        "unknown": _badge("Unknown", "b-info"),
    }.get(lv_status, _badge(lv_status, "b-info"))

    return (
        '<div class="kpi-strip">'
        f'<div class="kpi"><div class="label">参与 Rank</div><div class="value">{kpi["rank_count"]}</div>'
        f'<div class="sub">{kpi["step_count"]} step · 平均 wall {_fmt_ms(kpi["avg_wall_ms"])} ms / rank</div></div>'
        f'<div class="kpi"><div class="label">EP 峰均比 (GMM) <span class="ui-only-pill" title="UI-only heuristic — 非 diagnosis finding">UI-only</span></div>'
        f'<div class="value" style="color:{ep_color}">{ep_val}</div><div class="sub">{_esc(ep_sub)}</div></div>'
        f'<div class="kpi"><div class="label">DP 陪跑步数 <span class="ui-only-pill" title="UI-only heuristic — 非 diagnosis finding">UI-only</span></div>'
        f'<div class="value" style="color:{comp_color}">{comp["n_companion"]} / {comp["n_total_aligned"]}</div>'
        f'<div class="sub">{_esc(comp_msg)}</div></div>'
        f'<div class="kpi"><div class="label">Findings</div><div class="value">{kpi["findings_count"]}</div>'
        f'<div class="sub">最频 {_esc(kpi["findings_freq"])}</div></div>'
        f'<div class="kpi"><div class="label">层数校验</div><div class="value" style="font-size:16px;margin-top:8px">{lv_badge}</div>'
        f'<div class="sub">详见下方 Layer Validation 卡</div></div>'
        '</div>'
        '<div class="muted" style="margin-top:6px;font-size:11px">'
        '<span class="ui-only-pill" style="margin-right:6px">UI-only</span>'
        '标签项为 UI 推断信号（EP 峰均比 / DP 陪跑 / Layer composition / 模型结构猜测），'
        '不会写入 <code>diagnosis_findings.json</code>，也不参与 evidence-chain 校验。'
        '</div>'
    )


def _layer_validation_card(ov: dict) -> str:
    lv = ov["layer_validation"]
    status = lv.get("status", "unknown")
    badge_cls = {"ok": "b-success", "degraded": "b-warn"}.get(status, "b-info")
    det = lv.get("detected_layers") or {}
    expected = lv.get("expected_layers")
    detected_min, detected_max = det.get("min"), det.get("max")
    detected_txt = (
        f"{detected_min}" if detected_min == detected_max and detected_min is not None
        else (f"{detected_min} – {detected_max}" if detected_min is not None else "—")
    )
    match = lv.get("layers_match")
    match_txt = {True: _badge("一致", "b-success"), False: _badge("不一致", "b-danger")}.get(match, _badge("无法判定", "b-info"))
    consistent = lv.get("per_rank_consistent")
    consistent_txt = {True: _badge("一致", "b-success"), False: _badge("不一致", "b-danger")}.get(consistent, _badge("—", "b-info"))

    rows = [
        ("检测层数 (min–max)", detected_txt),
        ("预期层数", f"{expected}（{_esc(lv.get('expected_source') or 'unknown')}）" if expected is not None else "未知"),
        ("检测 vs 预期", match_txt),
        ("Rank 间一致性", consistent_txt),
        ("分段模式", _esc(lv.get("segmentation_mode") or "—")),
        ("模型上下文置信度", _esc(lv.get("confidence") or "—")),
        ("数据来源", "analysis_summary.json" if lv.get("source") == "analysis_summary" else "segment_manifest + rank_summary 现算"),
    ]
    body = "".join(f'<div class="kv-line"><span class="muted">{k}</span><span>{v}</span></div>' for k, v in rows)

    outliers = det.get("per_rank_outliers") or []
    outliers_html = ""
    if outliers:
        items = "".join(
            f'<li><code>{_esc(o.get("rank_id"))}</code> → {_esc(o.get("layer_count_inventory"))}</li>'
            for o in outliers
        )
        outliers_html = f'<div style="margin-top:6px"><b>离群 rank</b><ul style="margin:4px 0 0 18px">{items}</ul></div>'

    limitations = [x for x in (lv.get("limitations") or []) if str(x).strip()]
    lim_html = ""
    if limitations:
        lim_html = '<div class="muted" style="font-size:11px;margin-top:6px">' + "<br>".join(
            f"⚠ {_esc(x)}" for x in limitations[:6]
        ) + "</div>"

    return (
        '<div class="card" style="margin-top:14px">'
        f'<h3 style="margin-top:0">Layer Validation · {_badge(status, badge_cls)}</h3>'
        '<div class="muted" style="font-size:11.5px;margin-bottom:6px">'
        '检测层数 vs 预期层数 + 每 rank 一致性。数据取自 <code>analysis_summary.json</code>；'
        '该文件不存在时（如 report 首次渲染）由 layer_segments / segment_manifest 现算，口径一致。'
        '</div>'
        f'<div class="kv-grid">{body}</div>'
        f'{outliers_html}{lim_html}'
        '</div>'
    )


def _cross_rank_card(ov: dict) -> str:
    rows = []
    for r in ov["ranks"]:
        speed_badge = {"slow": _badge("慢卡", "b-slow"), "fast": _badge("轻卡", "b-fast")}.get(
            r["speed"], _badge("normal", "b-success"))
        gmm = f"{r['gmm_ms']:.1f} ms" if r["gmm_ms"] is not None else "—"
        rows.append(
            "<tr>"
            f"<td><b>{_esc(r['rank_short'])}</b><div class='muted' style='font-size:10px'>{_esc(r['rank_id'])}</div></td>"
            f"<td class='num'>{r['step_count']}</td>"
            f"<td class='num'>{_fmt_ms(r['wall_ms'])}</td>"
            f"<td class='num'>{_fmt_ms(r['busy_ms'])}</td>"
            f"<td class='num'>{r['busy_diff_pct']:+.1f}%</td>"
            f"<td class='num'>{gmm}</td>"
            f"<td class='num'>{r['underfeed_pct']:.1f}%</td>"
            f"<td>{speed_badge}</td>"
            f"<td><span class='badge {r['workload_class']}'>{_esc(r['workload_label'])}</span></td>"
            "</tr>"
        )
    if not rows:
        return ""
    return (
        '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">跨 Rank 总览</h3>'
        '<div class="scroll-x"><table><thead><tr><th>Rank</th><th class="num">Steps</th>'
        '<th class="num">Wall ms</th><th class="num">Busy ms</th><th class="num">Busy vs 均值</th>'
        '<th class="num">GMM 总 ms</th><th class="num">Underfeed</th><th>Speed</th><th>Workload</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
        '<div class="muted" style="margin-top:6px;font-size:11px">'
        'Speed：busy 比组均值 ±30% 时报警 · Workload：real = attention+moe 占比 &gt; 80% · companion = ≥ 50% 步是 moe-only/dummy'
        '</div></div>'
    )


def _ep_card(ov: dict) -> str:
    ep = ov["kpis"]["ep"]
    if not ep["available"]:
        return ""
    by_rank = ep.get("by_rank_ms") or {}
    peak = max(by_rank.values(), default=0.0)
    mean = ep["mean_ms"] or 1.0
    rows = []
    for rid, v in sorted(by_rank.items(), key=lambda kv: -kv[1]):
        dev = (v - mean) / mean if mean else 0.0
        color = "var(--danger)" if dev > 0.10 else ("var(--success)" if abs(dev) < 0.05 else "var(--accent)")
        bar_pct = (v / peak * 100) if peak else 0.0
        rows.append(
            "<tr>"
            f"<td><b>{_esc(hr.short_rank_label(rid))}</b></td>"
            f"<td class='num'>{v:.2f}</td>"
            "<td class='bar-cell' style='min-width:160px'>"
            f"<div class='bar' style='width:{bar_pct:.1f}%;background:{color}'></div>"
            f"<div class='label'>{dev * 100:+.1f}% vs mean</div></td>"
            "</tr>"
        )
    verdict = (
        _badge("EP imbalance", "b-danger") if ep["peak_to_mean"] >= 1.10
        else _badge("EP balanced", "b-success")
    )
    return (
        '<div class="card" style="margin-top:14px">'
        f'<h3 style="margin-top:0">EP 负载（GroupedMatmul wall）· {verdict}</h3>'
        '<div class="muted" style="font-size:11.5px;margin-bottom:6px">'
        f'峰均比 = max / mean = <b>{ep["peak_to_mean"]:.3f}</b> · spread = <b>{ep["spread_pct"]:.1f}%</b>'
        ' · 经验阈值：&gt; 1.10 视为 EP 不均（GroupedMatmul 是 MoE expert dispatch 的核心 kernel，'
        '每 rank 的 GMM 总耗时直接反映分到的 token 量）'
        '</div>'
        '<table><thead><tr><th>Rank</th><th class="num">GMM 总耗时 ms</th><th>Deviation vs mean</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div>'
    )


def _companion_card(ov: dict) -> str:
    comp = ov["kpis"]["companion"]
    if not comp["n_companion"]:
        return ""
    rows = "".join(
        f"<tr><td><b>{_esc(', '.join(p['real']))}</b></td>"
        f"<td><span class='muted'>陪跑</span> <b>{_esc(', '.join(p['dummy']))}</b></td>"
        f"<td class='num'>{p['count']}</td></tr>"
        for p in comp["pairs"]
    )
    return (
        '<div class="card" style="margin-top:14px">'
        f'<h3 style="margin-top:0">DP 陪跑判定 · {_badge("存在错位", "b-warn")}</h3>'
        '<div class="muted" style="font-size:11.5px;margin-bottom:6px">'
        f'在 {comp["n_total_aligned"]} 个对齐 step 中，有 <b>{comp["n_companion"]}</b> 个 step 出现：'
        '部分 rank 跑真实数据（attention+moe / attention+dense），另一部分 rank 跑 moe-only / ffn-only / 空 dummy。'
        '这通常意味着 prefill 阶段或 schedule 不均。'
        '</div>'
        '<table><thead><tr><th>真实数据 rank</th><th>陪跑 rank</th><th class="num">出现步数</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def _hist_block(title: str, rows: list, note: str) -> str:
    if not rows:
        return ""
    bars = []
    for item in rows:
        bars.append(
            '<div class="hist-row">'
            f'<div class="hist-k" title="{_esc(item["key"])}">{_esc(item["key"])}</div>'
            '<div class="bar-host">'
            f'<div class="bar-fill" style="width:{min(item["pct"], 100):.2f}%;background:{_esc(item["color"])}"></div>'
            f'<div class="bar-lbl">{item["ms"]:,.1f} ms · {item["pct"]:.1f}%</div>'
            '</div></div>'
        )
    return (
        f'<div style="flex:1 1 420px;min-width:320px"><h4 style="margin:0 0 6px">{_esc(title)}</h4>'
        + "".join(bars)
        + f'<div class="muted" style="font-size:11px;margin-top:4px">{note}</div></div>'
    )


def _histograms_card(ov: dict) -> str:
    inner = (
        _hist_block("按 op_type 聚合", ov["op_type_hist"],
                    "所有 device 事件（去 redundant）按 op_type 汇总 duration。")
        + _hist_block("按 bound_family 聚合", ov["bound_family_hist"],
                      "每个事件按 pipeline 耗时最长 stage 归类（communication/aicpu 直接归类）后汇总 duration。")
    )
    if not inner:
        return ""
    return (
        '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">算子构成直方图 · 全 capture</h3>'
        f'<div style="display:flex;flex-wrap:wrap;gap:24px">{inner}</div></div>'
    )


def _class_table(title: str, rows: list, columns: list[tuple[str, str]], note: str, *,
                 link_to_l2: bool = False) -> str:
    if not rows:
        return ""
    head = "".join(f"<th class='{cls}'>{_esc(label)}</th>" for label, cls in columns)
    body_rows = []
    for row in rows:
        cells = []
        for label, cls in columns:
            key = _COLUMN_KEYS[label]
            v = row.get(key)
            if key == "id":
                cells.append(f"<td><code>{_esc(str(v or '')[:24])}</code></td>")
            elif key == "share_pct":
                cells.append(f"<td class='num'>{v:.2f}%</td>" if v is not None else "<td class='num'>—</td>")
            elif isinstance(v, float):
                cells.append(f"<td class='num'>{v:,.2f}</td>")
            elif v is None:
                cells.append("<td class='num'>—</td>")
            else:
                cells.append(f"<td class='num'>{_esc(v)}</td>" if cls == "num" else f"<td>{_esc(v)}</td>")
        attrs = (
            f" class='clickable' data-route='l2' data-cls='{_esc(row['id'])}'"
            if link_to_l2 else ""
        )
        body_rows.append(f"<tr{attrs}>" + "".join(cells) + "</tr>")
    return (
        f'<h4 style="margin:14px 0 6px">{_esc(title)} <span class="muted" style="font-weight:400">（{len(rows)} 类）</span></h4>'
        '<div class="scroll-x"><table>'
        f'<thead><tr>{head}</tr></thead><tbody>{"".join(body_rows)}</tbody></table></div>'
        f'<div class="muted" style="font-size:11px;margin-top:4px">{note}</div>'
    )


_COLUMN_KEYS = {
    "Class": "id",
    "Family": "step_family",
    "Block 组合": "block_kinds",
    "Kind": "block_kind",
    "Bound family": "bound_family",
    "层数": "main_layer_count",
    "成员数": "members",
    "Rank 数": "ranks",
    "Wall 合计 ms": "wall_ms_sum",
    "占比(外推)": "share_pct",
    "Wall 均值 ms": "wall_ms_mean",
    "P50 ms": "wall_ms_p50",
    "P90 ms": "wall_ms_p90",
    "Bubble 均值 ms": "bubble_ms_mean",
    "Comm share 均值": "comm_share_mean",
}


def _class_rollup_card(ov: dict) -> str:
    parts = []
    if ov["step_classes"]:
        parts.append(_class_table(
            "Step classes", ov["step_classes"],
            [("Class", ""), ("Family", ""), ("层数", "num"), ("成员数", "num"), ("Rank 数", "num"),
             ("Wall 合计 ms", "num"), ("占比(外推)", "num"), ("Wall 均值 ms", "num"),
             ("P50 ms", "num"), ("P90 ms", "num"), ("Bubble 均值 ms", "num")],
            "占比 = 该 class 成员 wall_ms_sum / 全 rank wall 合计（成员实测汇总外推至全 capture）。点击 class 行进入 L2 视图。",
            link_to_l2=True))
    if ov["layer_classes"]:
        parts.append(_class_table(
            "Layer classes", ov["layer_classes"],
            [("Class", ""), ("Block 组合", ""), ("成员数", "num"), ("Rank 数", "num"),
             ("Wall 合计 ms", "num"), ("占比(外推)", "num"), ("Wall 均值 ms", "num"),
             ("P50 ms", "num"), ("P90 ms", "num"), ("Bubble 均值 ms", "num")],
            "成员 = 归属该结构类的 layer 实例；占比按成员 wall_ms_sum 外推。"))
    if ov["block_classes"]:
        parts.append(_class_table(
            "Block classes", ov["block_classes"],
            [("Class", ""), ("Kind", ""), ("Bound family", ""), ("成员数", "num"), ("Rank 数", "num"),
             ("Wall 合计 ms", "num"), ("占比(外推)", "num"), ("Wall 均值 ms", "num"),
             ("P50 ms", "num"), ("P90 ms", "num"), ("Comm share 均值", "num")],
            "成员 = 归属该结构类的 block 实例；comm share 为成员均值。"))
    if not parts:
        return ""
    return (
        '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">Class Rollup · step / layer / block</h3>'
        + "".join(parts) + "</div>"
    )


def _findings_panel(ov: dict) -> str:
    groups = ov["findings_groups"]
    if not groups:
        return (
            '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">Findings</h3>'
            '<div class="muted">无 diagnosis findings。</div></div>'
        )
    rows = []
    for i, g in enumerate(groups):
        sev = str(g.get("severity") or "info")
        rows.append(
            f"<tr class='clickable' data-route='findings' data-group='{i}'>"
            f"<td><code>{_esc(g.get('finding_type'))}</code></td>"
            f"<td>{_badge(sev, SEVERITY_BADGE.get(sev, 'b-info'))}</td>"
            f"<td class='num'>{g.get('occurrences', 0)}</td>"
            f"<td>{_esc((g.get('summary') or '')[:120])}</td>"
            "</tr>"
        )
    loaded = ov.get("analysis_summary_loaded")
    refs_note = (
        "knowledge_refs 来自 analysis_summary.json（未加载 — 本次渲染时该文件尚不存在，区块隐藏）。"
        if not loaded else "knowledge_refs 来自 analysis_summary.json；为空的组不显示该区。"
    )
    return (
        '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">Findings · rollup 分组</h3>'
        '<div class="muted" style="font-size:11.5px;margin-bottom:6px">'
        '按 (type, severity, summary) 分组；点击行查看该组全部 finding 与 evidence 链接（按需加载 findings.json.gz）。'
        + refs_note + '</div>'
        '<div class="scroll-x"><table><thead><tr><th>Type</th><th>Severity</th>'
        '<th class="num">Occurrences</th><th>Summary</th></tr></thead>'
        f'<tbody>{"".join(rows)}</tbody></table></div></div>'
    )


def _knowledge_refs_section(ov: dict) -> str:
    refs = ov.get("knowledge_refs") or []
    if not refs:
        return ""
    items = []
    for ref in refs[:50]:
        if isinstance(ref, dict):
            label = ref.get("title") or ref.get("id") or json.dumps(ref, ensure_ascii=False)[:120]
            url = ref.get("url") or ""
        else:
            label, url = str(ref), ""
        link = f' <a href="{_esc(url)}" target="_blank" rel="noreferrer">链接</a>' if url else ""
        items.append(f"<li>{_esc(label)}{link}</li>")
    return (
        '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">Knowledge refs</h3>'
        f'<ul style="margin:4px 0 0 18px">{"".join(items)}</ul></div>'
    )


def _gantt_card(ov: dict) -> str:
    gantt = ov["gantt"]
    if not gantt["steps"] or gantt["max_wall_ms"] <= 0:
        return '<div class="muted">无 step 数据</div>'

    width, margin_l, margin_r = 1320, 130, 20
    row_h, gap, label_h = 32, 5, 24
    plot_w = width - margin_l - margin_r
    ranks = gantt["ranks"]
    plot_h = label_h + len(ranks) * (row_h + gap)
    height = plot_h + 20
    max_wall = gantt["max_wall_ms"]

    def x_of(ms: float) -> float:
        return margin_l + (ms / max_wall) * plot_w

    parts = [
        '<defs><pattern id="bubble-pattern" patternUnits="userSpaceOnUse" width="6" height="6" patternTransform="rotate(45)">'
        '<rect width="6" height="6" fill="transparent"/>'
        '<line x1="0" y1="0" x2="0" y2="6" stroke="rgba(248,81,73,.55)" stroke-width="2"/>'
        '</pattern></defs>'
    ]
    grid_step_ms = 2000
    tick = 0
    while tick <= max_wall + grid_step_ms:
        x = x_of(tick)
        parts.append(f'<line class="gridline" x1="{x:.1f}" y1="{label_h}" x2="{x:.1f}" y2="{plot_h}"/>')
        parts.append(f'<text class="axis-text" x="{x:.1f}" y="{label_h - 6}" text-anchor="middle">{tick / 1000:.1f}s</text>')
        tick += grid_step_ms
    for ri, rlabel in enumerate(ranks):
        row_top = label_h + ri * (row_h + gap)
        parts.append(
            f'<text class="rank-label" x="{margin_l - 8:.1f}" y="{row_top + row_h / 2 + 4:.1f}" text-anchor="end">{_esc(rlabel)}</text>')
        parts.append(
            f'<rect x="{margin_l}" y="{row_top}" width="{plot_w}" height="{row_h}" fill="var(--gantt-track)" rx="3"/>')
    for st in gantt["steps"]:
        row_top = label_h + st["r"] * (row_h + gap)
        x0, x1 = x_of(st["t0"]), x_of(st["t1"])
        w = max(1.0, x1 - x0)
        tooltip = (
            f"{hr.family_label(st['fam'], st['layers'])} · wall {st['wall']:.1f}ms · bubble {st['bubble_ms']:.1f}ms"
            f" · 点击进入 L2"
        )
        parts.append(
            f'<rect class="seg" x="{x0:.1f}" y="{row_top + 4}" width="{w:.1f}" height="{row_h - 8}" '
            f'fill="{_esc(st["color"])}" rx="2" data-route="l2" data-cls="{_esc(st["cls"])}" data-seg="{_esc(st["seg"])}">'
            f'<title>{_esc(tooltip)}</title></rect>'
        )
        if st["bubble_ms"] > 0 and st["wall"] > 0:
            bw = w * (st["bubble_ms"] / st["wall"])
            parts.append(
                f'<rect x="{x0:.1f}" y="{row_top + 4}" width="{bw:.1f}" height="{row_h - 8}" '
                'fill="url(#bubble-pattern)" rx="2" pointer-events="none"/>'
            )

    legend = "".join(
        f'<span style="display:inline-flex;align-items:center;gap:4px;margin-right:14px">'
        f'<span style="display:inline-block;width:12px;height:12px;background:{_esc(f["color"])};border-radius:2px"></span>'
        f'{_esc(f["label"])}</span>'
        for f in gantt["families"]
    )
    legend += (
        '<span style="display:inline-flex;align-items:center;gap:4px;margin-right:14px">'
        '<span style="display:inline-block;width:12px;height:12px;background-image:repeating-linear-gradient(45deg,rgba(248,81,73,.55) 0 2px,transparent 2px 6px);background-color:var(--gantt-track);border-radius:2px"></span>'
        'bubble (idle)</span>'
    )
    return (
        '<div class="card scroll-x">'
        f'<svg class="gantt-svg" width="{width}" height="{height}" xmlns="http://www.w3.org/2000/svg">'
        + "".join(parts)
        + '</svg>'
        f'<div style="margin-top:8px;font-size:11px">{legend}</div></div>'
    )


def render_overview_html(ov: dict) -> str:
    """The static L1 view embedded in the shell."""
    return "".join([
        _kpi_strip(ov),
        _layer_validation_card(ov),
        _cross_rank_card(ov),
        _ep_card(ov),
        _companion_card(ov),
        _histograms_card(ov),
        _class_rollup_card(ov),
        _findings_panel(ov),
        _knowledge_refs_section(ov),
        '<div style="margin-top:14px"><h3 style="margin:0 0 8px 0">每 Rank Step 时间线 · 点击任一 step 进入 L2</h3>',
        _gantt_card(ov),
        '</div>',
    ])


# ---------------------------------------------------------------------------
# shell assembly
# ---------------------------------------------------------------------------


def render_shell(
    *,
    title: str,
    overview_html: str,
    overview_data: dict[str, Any],
    manifest: dict[str, Any],
    field_docs: dict[str, str],
    embedded_assets: dict[str, str] | None = None,
) -> str:
    """Assemble ``report.html``: inline CSS/JS + static L1 + manifest."""
    css = (_PKG_DIR / "app.css").read_text(encoding="utf-8")
    js = (_PKG_DIR / "app.js").read_text(encoding="utf-8")

    boot = (
        f"window.__ASSET_MANIFEST__={_json_script(manifest)};\n"
        f"window.__OVERVIEW__={_json_script(overview_data)};\n"
        f"window.__FIELD_DOCS__={_json_script(field_docs)};\n"
        f"window.__EMBEDDED_ASSETS__={_json_script(embedded_assets) if embedded_assets is not None else 'null'};\n"
    )

    return "".join([
        '<!doctype html><html lang="zh-cn"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f'<title>{_esc(title)}</title>',
        f'<style>{css}</style></head><body>',
        # protocol / capability banners (JS unhides when triggered)
        '<div id="file-banner" class="banner banner-warn hidden">',
        '当前以 <code>file://</code> 协议打开，浏览器禁止按需加载 <code>assets/*.json.gz</code> 明细数据；'
        '仅 L1 总览可用。请在本目录运行 <code>python3 -m http.server</code>（或任意静态服务器）后通过 '
        '<code>http://localhost:8000/report.html</code> 访问；或使用 <code>--html-single-file</code> 生成单文件版。',
        '</div>',
        '<div id="decomp-banner" class="banner banner-warn hidden">',
        '当前浏览器不支持 <code>DecompressionStream</code>，无法在线解压 gzip 资产；'
        '可直接下载下方对应 <code>.json.gz</code> 文件自行解压查看（<code>gzip -d</code>）。',
        '<div id="decomp-links" style="margin-top:4px"></div>',
        '</div>',
        '<div id="error-banner" class="banner banner-danger hidden"></div>',
        '<header class="app-chrome">',
        '<button id="back-btn" class="back-btn" disabled>← 上一级</button>',
        '<nav class="breadcrumb" id="breadcrumb"><span class="crumb active" data-route="l1">总览 · L1</span></nav>',
        f'<span class="chrome-title">{_esc(title)}</span>',
        '<span class="chrome-meta">Backspace 返回 · 点击 step / layer 进入下一级 · 数据按需加载</span>',
        '<button id="theme-toggle" class="back-btn" title="切换浅色 / 深色主题">🌙 深色</button>',
        '</header>',
        '<main>',
        '<section class="view active" id="view-l1" data-level="1">',
        overview_html,
        '</section>',
        '<section class="view" id="view-dynamic"></section>',
        '</main>',
        f'<script>{boot}</script>',
        f'<script>{js}</script>',
        '</body></html>',
    ])
