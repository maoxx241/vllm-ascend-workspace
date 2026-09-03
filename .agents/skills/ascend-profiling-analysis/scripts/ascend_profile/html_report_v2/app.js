/* v2 report browser app — vanilla JS, zero dependencies.
 *
 * Data model: report.html embeds the asset manifest + static L1 overview;
 * detail views are gzipped JSON assets fetched and inflated on demand
 * (DecompressionStream). Single-file builds embed the same payloads as
 * base64 in window.__EMBEDDED_ASSETS__ so no fetch is needed at all.
 */
(function () {
"use strict";

var MANIFEST = window.__ASSET_MANIFEST__ || { assets: {} };
var OVERVIEW = window.__OVERVIEW__ || {};
var FIELD_DOCS = window.__FIELD_DOCS__ || {};
var EMBEDDED = window.__EMBEDDED_ASSETS__ || null;
var SINGLE_FILE = !!EMBEDDED;

// ---------------------------------------------------------------------------
// utils
// ---------------------------------------------------------------------------

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function fmtNum(v, d) {
  if (v == null || isNaN(v)) return "—";
  if (d == null) d = 2;
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}
function fmtMs(us) { return fmtNum((us || 0) / 1000.0, 2); }
function pct(v, d) { return fmtNum(v, d == null ? 1 : d) + "%"; }
function shortOpName(name) {
  if (!name) return "";
  if (name.indexOf("aclnn") === 0) return name.split("_")[0];
  if (name.indexOf("hcom_") === 0) { var i = name.indexOf("__"); return i >= 0 ? name.slice(0, i) : name; }
  return name;
}
function el(id) { return document.getElementById(id); }

var OP_TYPE_COLOR = {
  aic: "#79c0ff", aiv: "#d2a8ff", mix_cv: "#ffa657", mix_comm_aiv: "#f0883e",
  communication: "#f85149", aicpu: "#a371f7", dsa: "#7ee787", unknown: "#8b949e"
};
var BOUND_FAMILY_COLOR = {
  cube: "#79c0ff", vector: "#d2a8ff", aic_mte: "#58a6ff", aiv_mte: "#bc8cff",
  scalar: "#ffa657", mixed: "#f0883e", communication: "#f85149",
  comm_aiv_mix: "#ff7b72", aicpu: "#a371f7", dsa: "#7ee787", unknown: "#8b949e"
};
function colorOf(table, key) { return table[key] || "#8b949e"; }
function typeBadge(opType) {
  var c = colorOf(OP_TYPE_COLOR, opType);
  return '<span class="badge" style="background:' + c + '33;color:' + c + '">' + esc(opType) + '</span>';
}
function famBadge(fam) {
  var c = colorOf(BOUND_FAMILY_COLOR, fam);
  return '<span class="badge" style="background:' + c + '33;color:' + c + '">' + esc(fam) + '</span>';
}
function infoBtn(key) {
  if (!FIELD_DOCS[key]) return "";
  return '<span class="info-btn" data-doc-key="' + esc(key) + '" title="点击查看字段说明">ⓘ</span>';
}

// ---------------------------------------------------------------------------
// theme (light default; localStorage remembers)
// ---------------------------------------------------------------------------

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  var btn = el("theme-toggle");
  if (btn) btn.textContent = theme === "dark" ? "☀️ 浅色" : "🌙 深色";
  try { localStorage.setItem("ascend-report-theme", theme); } catch (e) {}
}
function initTheme() {
  var saved = null;
  try { saved = localStorage.getItem("ascend-report-theme"); } catch (e) {}
  applyTheme(saved === "dark" ? "dark" : "light");
  var btn = el("theme-toggle");
  if (btn) btn.addEventListener("click", function () {
    applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
  });
}

// ---------------------------------------------------------------------------
// capability detection: file:// protocol + DecompressionStream
// ---------------------------------------------------------------------------

function initBanners() {
  var isFile = location.protocol === "file:";
  var hasDecomp = typeof DecompressionStream !== "undefined";
  if (isFile && !SINGLE_FILE) {
    el("file-banner").classList.remove("hidden");
  }
  if (!hasDecomp) {
    el("decomp-banner").classList.remove("hidden");
    var links = [];
    var assets = MANIFEST.assets || {};
    for (var key in assets) {
      if (assets.hasOwnProperty(key)) {
        links.push('<a href="' + esc(assets[key].file) + '" download>' + esc(assets[key].file) + '</a>');
      }
    }
    el("decomp-links").innerHTML = links.slice(0, 40).join(" · ");
  }
}
function showError(msg) {
  var b = el("error-banner");
  b.textContent = msg;
  b.classList.remove("hidden");
}

// ---------------------------------------------------------------------------
// asset loader: fetch + DecompressionStream('gzip'), or embedded base64
// ---------------------------------------------------------------------------

var _assetCache = {};

function _b64ToBytes(b64) {
  var bin = atob(b64);
  var len = bin.length;
  var bytes = new Uint8Array(len);
  for (var i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
  return bytes;
}

function _gunzip(bytes) {
  var ds = new DecompressionStream("gzip");
  var stream = new Blob([bytes]).stream().pipeThrough(ds);
  return new Response(stream).text();
}

function loadAsset(key) {
  if (_assetCache[key]) return _assetCache[key];
  var entry = (MANIFEST.assets || {})[key];
  if (!entry) {
    return Promise.reject(new Error("manifest 中不存在资产: " + key));
  }
  var p;
  if (EMBEDDED && EMBEDDED[entry.file]) {
    p = Promise.resolve(_b64ToBytes(EMBEDDED[entry.file])).then(_gunzip);
  } else if (EMBEDDED) {
    p = Promise.reject(new Error("内嵌资产缺失: " + entry.file));
  } else if (location.protocol === "file:") {
    p = Promise.reject(new Error("file:// 协议下无法加载 " + entry.file + " — 请用 python3 -m http.server 打开"));
  } else {
    p = fetch(encodeURI(entry.file)).then(function (resp) {
      if (!resp.ok) throw new Error("加载失败 " + entry.file + ": HTTP " + resp.status);
      return resp.arrayBuffer();
    }).then(function (buf) { return _gunzip(new Uint8Array(buf)); });
  }
  _assetCache[key] = p.then(function (text) { return JSON.parse(text); });
  return _assetCache[key];
}

// ---------------------------------------------------------------------------
// router (L1 static / L2 class / L3 layer / timeline / findings)
// ---------------------------------------------------------------------------

var _history = [{ name: "l1" }];

function routeTitle(route) {
  if (route.name === "l1") return "总览 · L1";
  return route._title || "详情";
}
function currentRoute() { return _history[_history.length - 1]; }

function navigate(route, opts) {
  opts = opts || {};
  if (route.name === "l1") {
    activate(document.getElementById("view-l1"));
    if (opts.replace) { if (_history.length > 1) _history[_history.length - 1] = route; }
    else if (currentRoute().name !== "l1") _history.push(route);
    updateChrome();
    window.scrollTo(0, 0);
    return Promise.resolve();
  }
  if (opts.replace && _history.length > 1) _history[_history.length - 1] = route;
  else _history.push(route);
  updateChrome();
  return renderDynamic(route);
}

function renderDynamic(route) {
  var dyn = el("view-dynamic");
  dyn.innerHTML = '<div class="loading"><span class="spinner"></span> 加载数据资产…</div>';
  activate(dyn);
  var render;
  if (route.name === "l2") render = renderL2;
  else if (route.name === "l3") render = renderL3;
  else if (route.name === "timeline") render = renderTimeline;
  else if (route.name === "findings") render = renderFindings;
  else return Promise.reject(new Error("未知视图: " + route.name));
  return render(route).then(function (title) {
    route._title = title;
    updateChrome();
    window.scrollTo(0, 0);
  }).catch(function (err) {
    dyn.innerHTML = '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">视图加载失败</h3>' +
      '<pre style="color:var(--danger);white-space:pre-wrap">' + esc(err && err.message || err) + '</pre>' +
      '<div class="muted">可返回上一级或总览。</div></div>';
    showError("视图加载失败: " + (err && err.message || err));
    route._title = "加载失败";
    updateChrome();
  });
}

function activate(section) {
  var views = document.querySelectorAll(".view");
  for (var i = 0; i < views.length; i++) views[i].classList.remove("active");
  section.classList.add("active");
}

function goBack() {
  if (_history.length <= 1) return;
  _history.pop();
  var route = currentRoute();
  if (route.name === "l1") {
    activate(document.getElementById("view-l1"));
    updateChrome();
    window.scrollTo(0, 0);
    return;
  }
  renderDynamic(route);
}

function updateChrome() {
  var btn = el("back-btn");
  if (btn) btn.disabled = _history.length <= 1;
  var crumb = el("breadcrumb");
  if (!crumb) return;
  var route = currentRoute();
  var html = '<span class="crumb' + (route.name === "l1" ? " active" : "") + '" data-route="l1">总览 · L1</span>';
  if (route.name !== "l1") {
    html += '<span class="sep">›</span><span class="crumb active">' + esc(routeTitle(route)) + '</span>';
  }
  crumb.innerHTML = html;
}

function routeFromDataset(ds) {
  var name = ds.route;
  if (name === "l1") return { name: "l1" };
  if (name === "l2") return { name: "l2", cls: ds.cls, seg: ds.seg || null, layer: ds.layer || null };
  if (name === "l3") return { name: "l3", cls: ds.cls, key: ds.key };
  if (name === "timeline") return { name: "timeline", rank: ds.rank, seg: ds.seg || null };
  if (name === "findings") return { name: "findings", group: ds.group != null ? Number(ds.group) : null };
  return null;
}

document.addEventListener("click", function (e) {
  var t = e.target.closest("[data-route]");
  if (!t) return;
  var route = routeFromDataset(t.dataset);
  if (!route) return;
  e.preventDefault();
  navigate(route);
});
document.addEventListener("keydown", function (e) {
  if (e.key === "Backspace") {
    var ae = document.activeElement;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "SELECT" || ae.tagName === "TEXTAREA")) return;
    if (_history.length > 1) { e.preventDefault(); goBack(); }
  }
});

// ---------------------------------------------------------------------------
// L2: step class view (step list + selected step structure)
// ---------------------------------------------------------------------------

var L3_KIND_NOTE = {
  self: "本 step 即代表步",
  class_rep: "结构同代表步；layer 跳到代表步的 L3 详情",
  top1_fallback: "本 step 的 step_class 不在 top-3（L3 仅生成 top-3 by wall_ms_sum）；layer 跳到 top-1 代表步的同 layer_index",
  none: "未找到 L3 目标"
};

function renderL2(route) {
  return loadAsset("l2/" + route.cls).then(function (data) {
    var steps = data.steps || [];
    var selSeg = route.seg || data.rep_segment_id || (steps[0] && steps[0].seg);
    var detail = (data.step_detail || {})[selSeg];
    var selStep = null;
    for (var i = 0; i < steps.length; i++) if (steps[i].seg === selSeg) selStep = steps[i];

    var html = '';
    // class header
    html += '<div class="card"><div style="display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;align-items:center">' +
      '<div><h1 style="margin:0">' + esc(data.family_label || data.class_id) + '</h1>' +
      '<div class="muted" style="font-size:11.5px">step_class <code>' + esc(data.class_id) + '</code> · ' +
      data.member_count + ' 成员 · ' + data.rank_count + ' rank · wall 合计 <b>' + fmtNum(data.wall_ms_sum) + '</b> ms' +
      ' · 均值 ' + fmtNum(data.wall_ms_mean) + ' / P50 ' + fmtNum(data.wall_ms_p50) + ' / P90 ' + fmtNum(data.wall_ms_p90) + ' ms' +
      ' · bubble 均值 ' + fmtNum(data.bubble_ms_mean) + ' ms' +
      (data.has_l3 ? ' · <span class="badge b-success">有 L3 代表步</span>' : ' · <span class="badge b-info">无 L3（非 top-3）</span>') +
      '</div></div></div></div>';

    // step list card
    html += '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">Step 列表 · 点击选择查看单步结构</h3>' +
      '<div class="filter-bar">' +
      '<label class="muted">排序</label><select id="l2-sort">' +
      '<option value="idx">执行顺序</option><option value="wall">wall 降序</option><option value="bubble">bubble 降序</option></select>' +
      '<input id="l2-filter" type="text" placeholder="筛选 family / rank / segment …">' +
      '<span class="muted" id="l2-count"></span></div>' +
      '<div id="l2-step-list" style="max-height:340px;overflow-y:auto"></div></div>';

    // selected step detail
    html += '<div id="l2-detail">';
    if (detail && selStep) html += l2DetailHTML(data, selStep, detail, route.layer);
    else html += '<div class="card" style="margin-top:14px"><div class="muted">未找到该 step 的明细。</div></div>';
    html += '</div>';

    el("view-dynamic").innerHTML = html;
    wireL2List(data, selSeg);
    wireInfoButtons();
    if (route.layer) highlightLayerRow(route.layer);
    return (data.family_label || "step class") + " · L2";
  });
}

function l2StepRowsHTML(data, steps, selSeg) {
  var rows = ['<div class="kernel-row head" style="grid-template-columns:70px 1fr 0.7fr 0.6fr 0.6fr 0.6fr 0.8fr">' +
    '<div>Rank</div><div>Family</div><div class="num">#idx</div><div class="num">Wall ms</div>' +
    '<div class="num">Bubble %</div><div class="num">Layers</div><div></div></div>'];
  for (var i = 0; i < steps.length; i++) {
    var s = steps[i];
    var sel = s.seg === selSeg;
    var style = "grid-template-columns:70px 1fr 0.7fr 0.6fr 0.6fr 0.6fr 0.8fr" + (sel ? ";background:rgba(9,105,218,.10)" : "");
    rows.push(
      '<div class="kernel-row clickable" style="' + style + '"' +
      ' data-route="l2" data-cls="' + esc(data.class_id) + '" data-seg="' + esc(s.seg) + '">' +
      '<div><b>' + esc(s.rank) + '</b></div>' +
      '<div>' + esc(s.fam || "—") + (s.rep ? ' <span class="badge b-success">代表步</span>' : '') + '</div>' +
      '<div class="num">' + (s.idx + 1) + '</div>' +
      '<div class="num">' + fmtNum(s.wall) + '</div>' +
      '<div class="num">' + fmtNum(s.bubble_pct, 1) + '</div>' +
      '<div class="num">' + s.layers + '</div>' +
      '<div class="muted" style="text-align:right;font-size:10.5px">' + (sel ? "当前" : "选择") + '</div>' +
      '</div>'
    );
  }
  return rows.join("");
}

function wireL2List(data, selSeg) {
  var all = (data.steps || []).slice();
  var sortSel = el("l2-sort"), filterIn = el("l2-filter");
  var listHost = el("l2-step-list");
  function apply() {
    var steps = all.slice();
    var q = (filterIn.value || "").toLowerCase();
    if (q) {
      steps = steps.filter(function (s) {
        return (s.fam || "").toLowerCase().indexOf(q) >= 0 ||
               (s.rank || "").toLowerCase().indexOf(q) >= 0 ||
               (s.seg || "").toLowerCase().indexOf(q) >= 0;
      });
    }
    var mode = sortSel.value;
    if (mode === "wall") steps.sort(function (a, b) { return b.wall - a.wall; });
    else if (mode === "bubble") steps.sort(function (a, b) { return b.bubble_pct - a.bubble_pct; });
    else steps.sort(function (a, b) { return (a.rank + ":" + String(a.idx).padStart(6, "0")).localeCompare(b.rank + ":" + String(b.idx).padStart(6, "0")); });
    listHost.innerHTML = l2StepRowsHTML(data, steps, selSeg);
    el("l2-count").textContent = steps.length + " / " + all.length + " step";
  }
  sortSel.addEventListener("change", apply);
  filterIn.addEventListener("input", apply);
  apply();
}

function l2DetailHTML(data, step, d, highlightLayer) {
  var ph = d.phase || {};
  var wall = d.wall_ms || 0;
  function pctOf(ms) { return wall > 0 ? (ms / wall * 100) : 0; }
  var html = '<div class="card" style="margin-top:14px">' +
    '<div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">' +
    '<div><h3 style="margin:0">' + esc(step.fam || "step") + ' · rank ' + esc(step.rank) + ' · step #' + (step.idx + 1) + '</h3>' +
    '<div class="muted" style="font-size:11.5px">wall <b>' + fmtNum(wall) + '</b> ms · bubble <b style="color:var(--danger)">' +
    fmtNum((d.bubble_ratio || 0) * 100, 1) + '%</b> · window <code>' + fmtMs(d.start_us) + ' → ' + fmtMs(d.end_us) + ' ms</code> · segment <code>' +
    esc(step.seg) + '</code></div></div>' +
    (d.model ? '<div class="model-pill"><span class="lbl">模型反推</span> <b>' + esc(d.model) + '</b>' +
      '<span class="muted" style="font-size:10px">(' + d.layers_count + 'L' + (d.has_attention ? "+attn" : "") + (d.has_moe ? "+moe" : "") + ')</span></div>' : '') +
    '<button class="back-btn" data-route="timeline" data-rank="' + esc(step.rank_id) + '" data-seg="' + esc(step.seg) + '">查看时间轴</button>' +
    '</div></div>';

  // phase split
  html += '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">阶段分区</h3><div class="phase-split">' +
    '<div class="cell main"><div class="name">主体 (main)</div><div class="val">' + fmtNum(ph.main_ms) + ' ms</div>' +
    '<div class="sub">' + pct(pctOf(ph.main_ms)) + ' · main bubble ' + fmtNum(ph.main_bubble_ms) + ' ms</div></div>' +
    '<div class="cell spec"><div class="name">投机解码 (spec)' + infoBtn("speculative_layer") + '</div><div class="val">' + fmtNum(ph.spec_ms) + ' ms</div>' +
    '<div class="sub">' + pct(pctOf(ph.spec_ms)) + ' · ' + (ph.spec_layer_count || 0) + ' spec layers</div></div>' +
    '<div class="cell tail"><div class="name">尾部小算子+空泡 (tail)</div><div class="val">' + fmtNum(ph.tail_ms) + ' ms</div>' +
    '<div class="sub">' + pct(pctOf(ph.tail_ms)) + ' · tail bubble ' + fmtNum(ph.tail_bubble_ms) + ' ms</div></div>' +
    '<div class="cell bubble"><div class="name">空泡总计 (bubble)</div><div class="val" style="color:var(--danger)">' + fmtNum(ph.bubble_ms) + ' ms</div>' +
    '<div class="sub">' + pct(pctOf(ph.bubble_ms)) + ' · head ' + fmtNum(ph.head_bubble_ms, 1) + ' / main ' + fmtNum(ph.main_bubble_ms, 1) + ' / tail ' + fmtNum(ph.tail_bubble_ms, 1) + ' ms</div></div>' +
    '</div><div class="muted" style="font-size:11px;margin-top:6px">主体 = main layer 内事件 · 投机 = layer_role=spec 内事件 · 尾部 = tail 段事件 · 空泡 = step wall − active union（与 legacy 同口径）</div></div>';

  // cross-rank compare
  var xr = ['<div class="xrank-row head"><div>Rank</div><div class="num">Wall ms</div><div class="num">Bubble %</div><div class="num">Δ vs 本步</div><div></div></div>'];
  (d.xrank || []).forEach(function (r) {
    var color = r.diff_pct > 5 ? "var(--danger)" : (r.diff_pct < -5 ? "var(--accent)" : "var(--muted)");
    xr.push('<div class="xrank-row"><div><b>' + esc(r.rank) + '</b> ' + (r.self ? '<span class="badge b-real">本步</span>' : '') +
      '<div class="muted" style="font-size:10px">' + esc(r.fam || "") + '</div></div>' +
      '<div class="num">' + fmtNum(r.wall) + '</div><div class="num">' + fmtNum(r.bubble_pct, 1) + '</div>' +
      '<div class="num" style="color:' + color + '">' + (r.diff_pct > 0 ? "+" : "") + fmtNum(r.diff_pct, 1) + '%</div>' +
      '<div>' + (r.self ? "" : '<button class="back-btn" style="padding:2px 8px" data-route="l2" data-cls="' + esc(r.cls) + '" data-seg="' + esc(r.seg) + '">查看</button>') + '</div></div>');
  });
  html += '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">跨 Rank 同步对比</h3>' + xr.join("") + '</div>';

  // kernel rollup
  var krows = ['<div class="kernel-row head" style="grid-template-columns:1.5fr 0.5fr 0.4fr 1.6fr 0.5fr 0.7fr">' +
    '<div>Kernel</div><div>Op type</div><div class="num">Calls</div><div>Σ (in this step) · % of step active</div><div>Bound family</div><div>Bound stage</div></div>'];
  (d.kernels || []).forEach(function (k) {
    krows.push('<div class="kernel-row" data-kname="' + esc(k.k.toLowerCase()) + '" style="grid-template-columns:1.5fr 0.5fr 0.4fr 1.6fr 0.5fr 0.7fr">' +
      '<div class="name" title="' + esc(k.k) + '">' + esc(k.k) + '</div>' +
      '<div>' + typeBadge(k.ot) + '</div>' +
      '<div class="num">' + k.n + '</div>' +
      '<div class="bar-host" title="union of all calls of this kernel in this step"><div class="bar-fill" style="width:' + Math.min(k.pct, 100) + '%;background:' + colorOf(OP_TYPE_COLOR, k.ot) + '"></div>' +
      '<div class="bar-lbl">' + fmtNum(k.u_ms) + ' ms · ' + pct(k.pct) + '</div></div>' +
      '<div>' + famBadge(k.bf) + '</div>' +
      '<div class="muted" style="font-size:10.5px">' + esc(k.bs) + '</div></div>');
  });
  html += '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">Kernel 占比 · 按耗时降序</h3>' +
    '<div class="filter-bar"><input id="l2-kfilter" type="text" placeholder="按 kernel 名过滤…"><span class="muted">仅显示前 30 / 共 ' + (d.kernels_total || 0) + ' 种 kernel</span></div>' +
    '<div class="kernel-rollup" id="l2-kernel-host">' + krows.join("") + '</div>' +
    '<div class="muted" style="font-size:11px;margin-top:6px">分母 = 本 step 在本 rank 上所有 device 事件 (去 redundant) 的 active union = <b>' + fmtNum(ph.busy_ms) + ' ms</b>（step wall = ' + fmtNum(wall) + ' ms，差额 = bubble）</div></div>';

  // layer list
  var l3t = d.l3_target || {};
  var lrows = ['<div class="kernel-row head" style="grid-template-columns:50px 1.2fr 0.6fr 1.0fr 0.5fr 0.4fr 0.4fr">' +
    '<div>idx</div><div>Composition <span class="ui-only-pill" title="UI-only heuristic — block 组合由 block_segments 推断">UI-only</span></div>' +
    '<div class="num">Active ms</div><div class="num">% of step active</div><div class="num">Events</div><div>Role</div><div></div></div>'];
  (d.layers || []).forEach(function (ls) {
    var clickable = !!ls.l3;
    var hint = "";
    if (ls.l3k === "class_rep") hint = ' <span class="muted" style="font-size:10px">(on class rep)</span>';
    else if (ls.l3k === "top1_fallback") hint = ' <span class="muted" style="font-size:10px">(on top-1 rep)</span>';
    var pctColor = ls.pct > 5 ? "var(--success)" : (ls.pct > 2 ? "var(--warn)" : "var(--accent)");
    var attrs = clickable
      ? ' class="kernel-row clickable l3-link" data-route="l3" data-cls="' + esc(ls.l3c) + '" data-key="' + esc(ls.l3) + '" data-layer-key="' + esc(ls.idx + '-' + ls.role) + '"'
      : ' class="kernel-row" style="opacity:.55"';
    lrows.push('<div' + attrs + ' style="grid-template-columns:50px 1.2fr 0.6fr 1.0fr 0.5fr 0.4fr 0.4fr;' + (clickable ? "" : "") + '">' +
      '<div><span class="muted">L' + esc(ls.idx) + '</span></div>' +
      '<div><b style="color:var(--accent)">' + esc(ls.comp) + '</b>' + hint + '</div>' +
      '<div class="num">' + fmtNum(ls.ms) + '</div>' +
      '<div class="bar-host"><div class="bar-fill" style="width:' + Math.min(ls.pct, 100) + '%;background:' + pctColor + ';opacity:.6"></div>' +
      '<div class="bar-lbl">' + pct(ls.pct, 2) + '</div></div>' +
      '<div class="num muted">' + ls.n + '</div>' +
      '<div><span class="chip">' + esc(ls.role) + '</span></div>' +
      '<div class="muted" style="font-size:10.5px;text-align:right">' + (clickable ? "→ L3" : "—") + '</div></div>');
  });
  html += '<div class="card" style="margin-top:14px"><h3 style="margin-top:0">Layer 顺序 · 点击任一 layer 进入 L3</h3>' +
    '<div class="filter-bar"><label class="muted">Role</label><select id="l2-role-filter"><option value="">全部</option><option value="main">main</option><option value="spec">spec/speculative</option><option value="other">其它</option></select></div>' +
    '<div class="kernel-rollup" id="l2-layer-host">' + lrows.join("") + '</div>' +
    '<div class="muted" style="font-size:11px;margin-top:6px">分母 = 本 step 本 rank 所有 device 事件（去 redundant）的 active union；分子 = 本 layer 同口径 active union（跨流取并集，AIC/AIV 同时活跃不双计）。' +
    esc(L3_KIND_NOTE[l3t.kind] || "") + '</div></div>';

  setTimeout(function () { wireL2DetailFilters(); }, 0);
  return html;
}

function wireL2DetailFilters() {
  var kf = el("l2-kfilter");
  if (kf) kf.addEventListener("input", function () {
    var q = kf.value.toLowerCase();
    var rows = document.querySelectorAll("#l2-kernel-host .kernel-row[data-kname]");
    for (var i = 0; i < rows.length; i++) {
      rows[i].style.display = (!q || rows[i].getAttribute("data-kname").indexOf(q) >= 0) ? "" : "none";
    }
  });
  var rf = el("l2-role-filter");
  if (rf) rf.addEventListener("change", function () {
    var v = rf.value;
    var rows = document.querySelectorAll("#l2-layer-host .kernel-row:not(.head)");
    for (var i = 0; i < rows.length; i++) {
      var chip = rows[i].querySelector(".chip");
      var role = chip ? chip.textContent : "";
      var show = !v || (v === "main" && role === "main") ||
        (v === "spec" && (role === "spec" || role === "speculative" || role === "spec_layer")) ||
        (v === "other" && role !== "main" && role !== "spec" && role !== "speculative" && role !== "spec_layer");
      rows[i].style.display = show ? "" : "none";
    }
  });
}

function highlightLayerRow(layerKey) {
  setTimeout(function () {
    var rows = document.querySelectorAll(".l3-link");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-layer-key") === layerKey) {
        rows[i].scrollIntoView({ block: "center" });
        rows[i].style.boxShadow = "0 0 0 2px var(--accent)";
        setTimeout(function (row) { row.style.boxShadow = ""; }, 1600, rows[i]);
        break;
      }
    }
  }, 50);
}

// ---------------------------------------------------------------------------
// L3: layer view (operator cards + bubble axis)
// ---------------------------------------------------------------------------

var L3_CHUNK = 300;

function renderL3(route) {
  return loadAsset("l3/" + route.cls + "/" + route.key).then(function (data) {
    var title = "Layer " + data.layer_index + " · " + data.role + " · active " + fmtMs(data.layer_busy_us) + " ms · " + data.events.length + " ops";
    var html = '<div class="card"><h2 style="margin:0">' + esc(title) + '</h2>' +
      '<div class="muted" style="font-size:11.5px">step_class <code>' + esc(data.class_id) + '</code> 代表步 <code>' + esc(data.rep_segment_id) + '</code> · rank ' +
      esc(data.rank_short) + ' · window <code>' + fmtMs(data.start_us) + ' → ' + fmtMs(data.end_us) + ' ms</code> · ' +
      '按执行顺序排列；点击任一算子展开 46 字段算子卡 / pipeline ratio / IR 签名</div></div>';

    html += '<div class="card" style="margin-top:12px">' +
      '<h3 style="margin-top:0">Bubble tracing axis</h3>' + bubbleAxisHTML(data) + '</div>';

    html += '<div class="card" style="margin-top:12px">' +
      '<div class="filter-bar">' +
      '<input id="l3-filter" type="text" placeholder="按算子名过滤（子串）…">' +
      '<select id="l3-otype"><option value="">全部 op_type</option></select>' +
      '<select id="l3-bfamily"><option value="">全部 bound_family</option></select>' +
      '<span class="muted" id="l3-count"></span></div>' +
      '<div class="op-list" id="l3-list"></div>' +
      '<div id="l3-sentinel" class="muted" style="padding:8px;text-align:center"></div></div>';

    el("view-dynamic").innerHTML = html;
    wireL3(data);
    wireInfoButtons();
    return title;
  });
}

function bubbleAxisHTML(data) {
  var s = data.start_us, e = data.end_us;
  var span = Math.max(e - s, 1);
  var w = 1200, h = 26;
  var parts = ['<svg width="' + w + '" height="' + h + '" viewBox="0 0 ' + w + ' ' + h + '" xmlns="http://www.w3.org/2000/svg">'];
  parts.push('<rect x="0" y="8" width="' + w + '" height="10" fill="var(--gantt-track)" rx="2"/>');
  var bub = data.bubbles || [];
  var total = 0;
  for (var i = 0; i < bub.length; i++) {
    var g = bub[i];
    total += g.d;
    var x = (g.s - s) / span * w;
    var gw = Math.max(g.d / span * w, 1.2);
    parts.push('<rect x="' + x.toFixed(1) + '" y="8" width="' + gw.toFixed(1) + '" height="10" fill="var(--danger)" opacity=".75" rx="1">' +
      '<title>bubble ' + fmtNum(g.d, 1) + ' μs @ ' + fmtMs(g.s) + ' ms</title></rect>');
  }
  parts.push('</svg>');
  return '<div class="bubble-axis"><div class="muted" style="font-size:11px;margin-bottom:4px">layer 窗口内 active union 的空隙（≥1μs）：共 ' +
    bub.length + ' 段，合计 <b style="color:var(--danger)">' + fmtNum(total / 1000, 3) + ' ms</b>（红）</div>' + parts.join("") + '</div>';
}

function wireL3(data) {
  var state = { filtered: data.events.slice(), rendered: 0 };
  var listHost = el("l3-list");
  var sentinel = el("l3-sentinel");

  // populate filter selects
  var otSet = {}, bfSet = {};
  data.events.forEach(function (e) {
    otSet[e.t] = true;
    if (e.card && e.card.bf) bfSet[e.card.bf] = true;
  });
  var otSel = el("l3-otype"), bfSel = el("l3-bfamily");
  Object.keys(otSet).sort().forEach(function (k) { otSel.innerHTML += '<option value="' + esc(k) + '">' + esc(k) + '</option>'; });
  Object.keys(bfSet).sort().forEach(function (k) { bfSel.innerHTML += '<option value="' + esc(k) + '">' + esc(k) + '</option>'; });

  function headerRow() {
    return '<div class="op-list-row head"><div class="ix">#</div><div class="nm">Operator</div><div>Op type</div>' +
      '<div class="num">Stream</div><div class="num">Duration μs</div><div class="num">% of layer</div><div>Bound</div><div></div></div>';
  }

  function rowHTML(e, i) {
    var c = colorOf(OP_TYPE_COLOR, e.t);
    var bf = e.card ? e.card.bf : "unknown";
    var bc = colorOf(BOUND_FAMILY_COLOR, bf);
    var pctL = data.layer_busy_us > 0 ? (e.d / data.layer_busy_us * 100) : 0;
    return '<div class="op-list-row" data-ei="' + i + '">' +
      '<div class="ix">' + (i + 1) + '</div>' +
      '<div class="nm" title="' + esc(e.n) + '">' + esc(e.s) + '</div>' +
      '<div><span class="badge" style="background:' + c + '33;color:' + c + '">' + esc(e.t) + '</span></div>' +
      '<div class="num muted" style="font-family:\'SF Mono\',Menlo,Consolas,monospace;font-size:10.5px">' + esc(e.st || "—") + '</div>' +
      '<div class="num">' + fmtNum(e.d) + '</div>' +
      '<div class="num">' + pct(pctL, 2) + '</div>' +
      '<div><span class="badge" style="background:' + bc + '33;color:' + bc + '">' + esc(bf) + '</span></div>' +
      '<div style="text-align:right;color:var(--muted);font-size:14px">▾</div></div>' +
      '<div class="op-card-host hidden" data-host="' + i + '"></div>';
  }

  function renderChunk() {
    var start = state.rendered;
    var end = Math.min(start + L3_CHUNK, state.filtered.length);
    if (start >= end) return;
    var html = "";
    if (start === 0) html += headerRow();
    for (var i = start; i < end; i++) html += rowHTML(state.filtered[i], i);
    if (start === 0) listHost.innerHTML = html;
    else listHost.insertAdjacentHTML("beforeend", html);
    state.rendered = end;
    sentinel.textContent = end < state.filtered.length ?
      "已渲染 " + end + " / " + state.filtered.length + " — 滚动加载更多" :
      "共 " + state.filtered.length + " 个算子";
  }

  function applyFilter() {
    var q = (el("l3-filter").value || "").toLowerCase();
    var ot = otSel.value, bf = bfSel.value;
    state.filtered = data.events.filter(function (e) {
      if (q && e.n.toLowerCase().indexOf(q) < 0 && e.s.toLowerCase().indexOf(q) < 0) return false;
      if (ot && e.t !== ot) return false;
      if (bf && (!e.card || e.card.bf !== bf)) return false;
      return true;
    });
    state.rendered = 0;
    listHost.innerHTML = "";
    el("l3-count").textContent = state.filtered.length + " / " + data.events.length + " ops";
    renderChunk();
  }

  el("l3-filter").addEventListener("input", applyFilter);
  otSel.addEventListener("change", applyFilter);
  bfSel.addEventListener("change", applyFilter);

  var observer = new IntersectionObserver(function (entries) {
    if (entries[0].isIntersecting) renderChunk();
  });
  observer.observe(sentinel);

  // op-card lazy render on row click
  listHost.addEventListener("click", function (e) {
    var row = e.target.closest(".op-list-row[data-ei]");
    if (!row) return;
    var i = Number(row.getAttribute("data-ei"));
    var host = listHost.querySelector('[data-host="' + i + '"]');
    if (!host) return;
    if (host.classList.contains("hidden")) {
      if (!host.dataset.filled) {
        var ev = state.filtered[i];
        host.innerHTML = opCardHTML(ev, data);
        host.dataset.filled = "1";
        wireInfoButtons(host);
      }
      host.classList.remove("hidden");
    } else {
      host.classList.add("hidden");
    }
  });

  applyFilter();
}

// ---------------------------------------------------------------------------
// operator card (JS twin of legacy render_operator_card)
// ---------------------------------------------------------------------------

var RAW_KD_FIELDS = [
  "Device_id", "Model ID", "Task ID", "Stream ID", "Name", "Type", "OP State",
  "Accelerator Core", "Start Time(us)", "Duration(us)", "Wait Time(us)",
  "Block Dim", "Mix Block Dim", "HF32 Eligible",
  "Input Shapes", "Input Data Types", "Input Formats",
  "Output Shapes", "Output Data Types", "Output Formats", "Context ID",
  "aicore_time(us)", "aic_total_cycles",
  "aic_mac_time(us)", "aic_mac_ratio", "aic_scalar_time(us)", "aic_scalar_ratio",
  "aic_mte1_time(us)", "aic_mte1_ratio", "aic_mte2_time(us)", "aic_mte2_ratio",
  "aic_fixpipe_time(us)", "aic_fixpipe_ratio", "aic_icache_miss_rate",
  "aiv_time(us)", "aiv_total_cycles",
  "aiv_vec_time(us)", "aiv_vec_ratio", "aiv_scalar_time(us)", "aiv_scalar_ratio",
  "aiv_mte2_time(us)", "aiv_mte2_ratio", "aiv_mte3_time(us)", "aiv_mte3_ratio",
  "aiv_icache_miss_rate", "cube_utilization(%)"
];

function _splitSemi(value) {
  if (!value) return [];
  var v = String(value).trim();
  while (v.length >= 2 && v.charAt(0) === '"' && v.charAt(v.length - 1) === '"') {
    v = v.slice(1, -1);
    if (!v) break;
  }
  if (!v) return [];
  var toks = v.split(";");
  for (var i = 0; i < toks.length; i++) toks[i] = toks[i].trim();
  return toks;
}
function _fmtShape(s) {
  s = String(s || "").trim().replace(/^"|"$/g, "").trim();
  if (!s) return "";
  if (s.charAt(0) === "[" && s.charAt(s.length - 1) === "]") return s;
  return "[" + s + "]";
}

function irSignatureHTML(raw, short) {
  var inShapes = _splitSemi(raw["Input Shapes"]);
  var inDtypes = _splitSemi(raw["Input Data Types"]);
  var inFormats = _splitSemi(raw["Input Formats"]);
  var outShapes = _splitSemi(raw["Output Shapes"]);
  var outDtypes = _splitSemi(raw["Output Data Types"]);
  var outFormats = _splitSemi(raw["Output Formats"]);
  if (!inShapes.length && !outShapes.length) return "";

  function row(idx, shape, dtype, fmt, kind) {
    var isUndef = ((!shape || shape === "()" || shape === "[]") &&
      (!dtype || dtype === "DT_UNDEFINED" || dtype === "UNDEFINED"));
    if (isUndef) {
      return '<div class="ir-row ir-undef"><span class="ir-pname">' + kind + '_' + idx + '</span>' +
        '<span class="ir-colon">:</span><span class="muted" style="font-style:italic">undefined</span></div>';
    }
    var chips = "";
    if (dtype && dtype !== "DT_UNDEFINED") chips += '<span class="ir-dtype">' + esc(dtype) + '</span>';
    if (fmt && fmt !== "ND" && fmt !== "NULL") chips += '<span class="ir-fmt">' + esc(fmt) + '</span>';
    return '<div class="ir-row"><span class="ir-pname">' + kind + '_' + idx + '</span><span class="ir-colon">:</span>' +
      chips + '<code class="ir-shape">' + esc(_fmtShape(shape)) + '</code></div>';
  }
  var nIn = Math.max(inShapes.length, inDtypes.length, inFormats.length);
  var inRows = [], nDefined = 0;
  for (var i = 0; i < nIn; i++) {
    var r = row(i, inShapes[i] || "", inDtypes[i] || "", inFormats[i] || "", "in");
    if (r.indexOf("ir-undef") < 0) nDefined++;
    inRows.push(r);
  }
  var nOut = Math.max(outShapes.length, outDtypes.length, outFormats.length);
  var outRows = [];
  for (var j = 0; j < nOut; j++) outRows.push(row(j, outShapes[j] || "", outDtypes[j] || "", outFormats[j] || "", "out"));

  var summary = nDefined + " input" + (nDefined !== 1 ? "s" : "");
  if (nIn > nDefined) summary += ' <span class="muted">(+' + (nIn - nDefined) + ' undefined)</span>';
  return '<div class="ir-signature"><div class="ir-head"><span class="ir-fname">' + esc(short) + '</span>' +
    '<span class="muted">(</span> <span class="muted" style="font-size:10.5px">' + summary + '</span></div>' +
    '<div class="ir-block">' + inRows.join("") + '</div>' +
    '<div class="ir-tail"><span class="muted">) →</span></div>' +
    '<div class="ir-block">' + outRows.join("") + '</div></div>';
}

function opCardHTML(e, layer) {
  var c = e.card || {};
  var raw = c.raw || {};
  var short = e.s || shortOpName(e.n);
  var waitRatio = e.d > 0 ? e.w / e.d : 0;
  var hostWarn = waitRatio > 0.3;
  var totalWithWait = Math.max(e.d + e.w, 1);

  var chips = "";
  if (c.bd && c.bd !== "0") {
    chips += '<span class="chip" title="' + esc(FIELD_DOCS["Block Dim"] || "") + '">block_dim=' + esc(c.bd) +
      (c.mbd && c.mbd !== "0" ? "/" + esc(c.mbd) : "") + '</span>';
  }
  if (hostWarn) chips += '<span class="badge b-warn" title="wait_us / duration_us > 30% → 该算子很可能 host bound 或上游同步等待">host bound suspected</span>';

  // pipeline stages
  var stageRows = "";
  if (e.t === "communication") {
    stageRows = '<div class="muted" style="font-size:11px;margin-top:6px">communication op — 无 AIC/AIV pipeline stage（全 0）</div>';
  } else if (c.stages && c.stages.length) {
    var rows = [];
    c.stages.forEach(function (st) {
      var color = colorOf(BOUND_FAMILY_COLOR, st.fam);
      var marker = st.hot ? '<span style="color:var(--danger);margin-right:2px;font-weight:600">🔥</span>' : '<span style="display:inline-block;width:14px"></span>';
      var labelStyle = st.hot ? "color:var(--danger);font-weight:600" : "";
      var ratioLabel = st.r != null ? (st.r * 100).toFixed(1) + "%" : "—";
      var ratioField = st.k.replace("_time(", "_ratio(").replace("_time", "_ratio");
      rows.push('<div class="stage-row">' + marker +
        '<span class="stage-name" style="' + labelStyle + '">' + esc(st.k.replace("_time", "")) + '</span>' + infoBtn(st.k) +
        '<div class="stage-bar-track"><div class="stage-bar-fill" style="width:' + (st.r != null ? Math.min(st.r * 100, 100) : 0) + '%;background:' + color + '"></div></div>' +
        '<span class="stage-ratio">' + ratioLabel + '</span>' + (st.r != null ? infoBtn(ratioField) : "") +
        '<span class="stage-v">' + fmtNum(st.us) + ' μs</span></div>');
    });
    stageRows = '<div class="stage-list">' + rows.join("") + '</div>';
  }

  // decision narrative
  var decision = "";
  if (c.bound) {
    var bfColor = colorOf(BOUND_FAMILY_COLOR, c.bf);
    if (c.basis === "ratio" && c.br != null) {
      var ratioFieldName = c.bound.replace("_time", "_ratio");
      var candCount = 0;
      (c.stages || []).forEach(function (st) { if (st.r != null) candCount++; });
      decision = '<div class="decision-block"><span>🔥</span> <b>判定 bound_stage</b>' + infoBtn("bound_stage") + ' = ' +
        '<span style="color:var(--danger);font-weight:600;font-family:monospace">' + esc(c.bound.replace("_time", "")) + '</span>' +
        ' → <b>bound_family</b>' + infoBtn("bound_family") + ' = <span class="badge" style="background:' + bfColor + '33;color:' + bfColor + '">' + esc(c.bf) + '</span>' +
        '<div class="muted" style="font-size:11.5px;margin-top:5px"><b>依据</b>：CANN 报告的 <code>' + esc(ratioFieldName) + '</code>' + infoBtn(ratioFieldName) +
        ' = <span style="color:var(--warn);font-weight:600">' + (c.br * 100).toFixed(1) + '%</span>（在 ' + candCount + ' 个候选 stage 中 ratio 最高）</div></div>';
    } else {
      var t = 0;
      (c.stages || []).forEach(function (st) { if (st.k === c.bound) t = st.us; });
      decision = '<div class="decision-block"><span>🔥</span> <b>判定 bound_stage</b>' + infoBtn("bound_stage") + ' = ' +
        '<span style="color:var(--danger)">' + esc(c.bound.replace("_time", "")) + '</span> → <b>bound_family</b>' + infoBtn("bound_family") +
        ' = <span class="badge" style="background:' + bfColor + '33;color:' + bfColor + '">' + esc(c.bf) + '</span>' +
        '<div class="muted" style="font-size:11.5px;margin-top:5px"><b>依据</b>：绝对耗时最大（<span style="color:var(--warn)">' + fmtNum(t) + ' μs</span>，ratio 字段在 raw row 中缺失，退化判断）</div></div>';
    }
  }

  // extras (utilization / icache)
  var extras = "";
  (c.ex || []).forEach(function (pair) {
    var key = pair[0], v = pair[1];
    var display = key.indexOf("%") >= 0 ? v.toFixed(1) + "%" : (v * 100).toFixed(2) + "%";
    extras += '<div class="kv-row"><span class="kv-k">' + esc(key) + '</span>' + infoBtn(key) +
      '<div class="kv-bar-track"><div class="kv-bar-fill" style="width:' + Math.min(v <= 1 ? v * 100 : v, 100) + '%;background:#ffa657"></div></div>' +
      '<span class="kv-v">' + display + '</span></div>';
  });

  // raw 46-field dump
  var rawRows = [];
  for (var i = 0; i < RAW_KD_FIELDS.length; i++) {
    var f = RAW_KD_FIELDS[i];
    var v = raw[f];
    rawRows.push('<tr><td class="raw-k">' + esc(f) + infoBtn(f) + '</td><td class="raw-v">' +
      (v == null ? '<span class="muted">—</span>' : esc(v)) + '</td></tr>');
  }
  var rawTable = rawRows.length && Object.keys(raw).length
    ? '<table class="raw-fields">' + rawRows.join("") + '</table>'
    : '<div class="muted">无法 join 回原始 kernel_details.csv 行（source 缺失）</div>';

  return '<div class="op-card">' +
    '<div class="op-card-head"><div style="display:flex;align-items:center;flex-wrap:wrap;gap:6px">' +
    '<span class="op-name" title="' + esc(e.n) + '">' + esc(short) + '</span>' + typeBadge(e.t) +
    '<span class="muted" style="font-size:11px" title="' + esc(FIELD_DOCS["stream_id"] || "") + '">stream ' + esc(e.st || "—") + '</span>' +
    chips + '</div><div class="muted" style="font-size:11px">' + esc(e.tt || "") + '</div></div>' +
    irSignatureHTML(raw, short) +
    '<div class="op-meta"><div style="flex:1 1 320px;min-width:260px">' +
    '<div class="exec-wait-row"><span class="muted ew-label">execution ' + infoBtn("duration_us") + '</span>' +
    '<div class="ew-track"><div class="ew-fill" style="width:' + (e.d / totalWithWait * 100) + '%;background:var(--success)"></div></div>' +
    '<span class="ew-v">' + fmtNum(e.d) + ' μs</span></div>' +
    '<div class="exec-wait-row"><span class="muted ew-label">wait ' + infoBtn("wait_us") + '</span>' +
    '<div class="ew-track"><div class="ew-fill" style="width:' + (e.w / totalWithWait * 100) + '%;background:var(--warn)"></div></div>' +
    '<span class="ew-v">' + fmtNum(e.w) + ' μs <span class="muted">(' + (waitRatio * 100).toFixed(0) + '% of exec)</span></span></div></div>' +
    '<div class="op-shares">' +
    '<div><span class="muted">本次占 layer' + infoBtn("self_layer_pct") + '</span><span class="v">' + pct(c.sp, 2) + '</span>' +
    '<span class="muted" style="font-size:10px;display:block;margin-top:1px">' + fmtNum(e.d, 1) + ' μs / ' + fmtNum(c.lu, 0) + ' μs (layer active)</span></div>' +
    '<div><span class="muted">本类累计占 layer' + infoBtn("klayer_pct") + '</span><span class="v">' + pct(c.klp, 2) + ' <span class="muted" style="font-size:11px">(' + (c.kln || 1) + '×)</span></span>' +
    '<span class="muted" style="font-size:10px;display:block;margin-top:1px">' + fmtNum(c.klu, 0) + ' μs / ' + fmtNum(c.lu, 0) + ' μs (layer active)</span></div>' +
    '<div><span class="muted">本类累计占 step' + infoBtn("kstep_pct") + '</span><span class="v">' + pct(c.ksp, 2) + ' <span class="muted" style="font-size:11px">(' + (c.ksn || 1) + '×)</span></span>' +
    '<span class="muted" style="font-size:10px;display:block;margin-top:1px">' + fmtNum(c.ksu, 0) + ' μs / ' + fmtNum(c.su, 0) + ' μs (step active)</span></div>' +
    '</div></div>' +
    '<div class="pipe-section"><div class="muted" style="font-size:11px;text-transform:uppercase;letter-spacing:.04em;margin-bottom:4px">' +
    'Pipeline stages <span class="muted">· ratio (CANN-reported) + time · 🔥 = 判定 bound_stage</span></div>' + stageRows + '</div>' +
    decision +
    (extras ? '<div class="util-section">' + extras + '</div>' : "") +
    '<details class="raw-details"><summary>📋 原始 kernel_details.csv 全 46 字段</summary>' + rawTable + '</details>' +
    '</div>';
}

// ---------------------------------------------------------------------------
// timeline view (per rank; virtual render + zoom/pan)
// ---------------------------------------------------------------------------

var TL_LANE_H = 16;
var TL_STEP_H = 22;
var TL_MAX_RECTS = 1600;

function renderTimeline(route) {
  return loadAsset("timeline/" + route.rank).then(function (data) {
    var html = '<div class="card"><h2 style="margin:0">时间轴 · rank ' + esc(data.rank_short || data.rank_id) + '</h2>' +
      '<div class="muted" style="font-size:11.5px">' + data.events.length + ' events · ' + data.steps.length +
      ' steps · 滚轮/W/S 缩放 · 拖拽/A/D 平移 · 0/Esc 复位 · 点击顶部 step 带进入 L2</div></div>' +
      '<div class="card" style="margin-top:12px">' +
      '<div class="timeline-ctrl">' +
      '<button class="back-btn" id="tl-zi">放大 (W)</button><button class="back-btn" id="tl-zo">缩小 (S)</button>' +
      '<button class="back-btn" id="tl-left">← (A)</button><button class="back-btn" id="tl-right">→ (D)</button>' +
      '<button class="back-btn" id="tl-reset">复位 (0)</button>' +
      '<span class="timeline-status" id="tl-status"></span></div>' +
      '<div class="timeline-viewport" id="tl-vp" tabindex="0"></div></div>';
    el("view-dynamic").innerHTML = html;

    // initial window: selected step or whole capture
    var t0 = 0, t1 = 1;
    if (data.events.length) {
      t1 = 0;
      for (var i = 0; i < data.steps.length; i++) t1 = Math.max(t1, data.steps[i].e);
      if (t1 <= 0) t1 = data.events[data.events.length - 1][1] + data.events[data.events.length - 1][2];
    }
    if (route.seg) {
      for (var j = 0; j < data.steps.length; j++) {
        if (data.steps[j].seg === route.seg) {
          var pad = (data.steps[j].e - data.steps[j].s) * 0.05 + 1;
          t0 = Math.max(0, data.steps[j].s - pad);
          t1 = data.steps[j].e + pad;
          break;
        }
      }
    }
    setupTimeline(data, t0, t1);
    return "时间轴 · " + (data.rank_short || data.rank_id);
  });
}

function setupTimeline(data, winStart, winEnd) {
  var vp = el("tl-vp");
  var W = 1320;
  var totalSpan = Math.max(winEnd, 1);
  var vbX = winStart, vbW = Math.max(winEnd - winStart, 0.001);

  // lanes: top streams by event count (cap 40)
  var laneOfStream = {}, laneCounts = [];
  var ei, ev;
  for (ei = 0; ei < data.events.length; ei++) {
    var sid = data.events[ei][3];
    laneCounts[sid] = (laneCounts[sid] || 0) + 1;
  }
  var streamOrder = Object.keys(laneCounts).sort(function (a, b) { return laneCounts[b] - laneCounts[a]; }).slice(0, 40);
  streamOrder.forEach(function (sid, i) { laneOfStream[Number(sid)] = i; });
  var nLanes = streamOrder.length || 1;

  // per-lane event arrays, sorted by start (events already globally sorted)
  var laneEvents = [];
  for (var li = 0; li < nLanes; li++) laneEvents.push([]);
  for (ei = 0; ei < data.events.length; ei++) {
    ev = data.events[ei];
    var lane = laneOfStream[ev[3]];
    if (lane != null) laneEvents[lane].push(ev);
  }

  var plotH = TL_STEP_H + 6 + nLanes * (TL_LANE_H + 2) + 24;
  var marginL = 90;
  var plotW = W - marginL - 10;

  var svgNS = "http://www.w3.org/2000/svg";
  var svg = document.createElementNS(svgNS, "svg");
  svg.setAttribute("viewBox", "0 0 " + W + " " + plotH);
  svg.setAttribute("height", Math.min(plotH, 700));
  vp.appendChild(svg);

  function xOf(us) { return marginL + (us - vbX) / vbW * plotW; }
  function usOfX(px) { return vbX + (px - marginL) / plotW * vbW; }

  function redraw() {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var frag = document.createDocumentFragment();
    // grid + labels
    var ticks = 8;
    for (var g = 0; g <= ticks; g++) {
      var us = vbX + vbW * g / ticks;
      var x = xOf(us);
      var line = document.createElementNS(svgNS, "line");
      line.setAttribute("x1", x); line.setAttribute("x2", x);
      line.setAttribute("y1", TL_STEP_H + 4); line.setAttribute("y2", plotH - 18);
      line.setAttribute("class", "gridline");
      frag.appendChild(line);
      var txt = document.createElementNS(svgNS, "text");
      txt.setAttribute("x", x); txt.setAttribute("y", plotH - 6);
      txt.setAttribute("text-anchor", "middle"); txt.setAttribute("class", "axis-text");
      txt.textContent = (us / 1000).toFixed(2) + " ms";
      frag.appendChild(txt);
    }
    // step bands (top lane)
    for (var si = 0; si < data.steps.length; si++) {
      var st = data.steps[si];
      if (st.e < vbX || st.s > vbX + vbW) continue;
      var sx0 = Math.max(xOf(st.s), marginL), sx1 = Math.min(xOf(st.e), W - 10);
      if (sx1 - sx0 < 0.3) sx1 = sx0 + 0.3;
      var rect = document.createElementNS(svgNS, "rect");
      rect.setAttribute("x", sx0); rect.setAttribute("y", 2);
      rect.setAttribute("width", sx1 - sx0); rect.setAttribute("height", TL_STEP_H - 4);
      rect.setAttribute("fill", "#0969da"); rect.setAttribute("opacity", ".28");
      rect.setAttribute("rx", 2); rect.setAttribute("class", "seg");
      rect.setAttribute("data-route", "l2");
      rect.setAttribute("data-cls", st.cls); rect.setAttribute("data-seg", st.seg);
      var title = document.createElementNS(svgNS, "title");
      title.textContent = (st.fam || "step") + " #" + (st.idx + 1) + " · wall " + st.wall + " ms · bubble " + st.bub + " ms";
      rect.appendChild(title);
      frag.appendChild(rect);
    }
    // events, virtualized: only visible window; pixel-bucket decimation when dense
    var rendered = 0;
    var pxPerUs = plotW / vbW;
    var laneLabelStep = Math.ceil(nLanes / 12);
    for (var lane = 0; lane < nLanes; lane++) {
      var y = TL_STEP_H + 6 + lane * (TL_LANE_H + 2);
      if (lane % laneLabelStep === 0) {
        var lbl = document.createElementNS(svgNS, "text");
        lbl.setAttribute("x", marginL - 6); lbl.setAttribute("y", y + TL_LANE_H - 4);
        lbl.setAttribute("text-anchor", "end"); lbl.setAttribute("class", "axis-text");
        lbl.textContent = "s" + (data.streams[Number(streamOrder[lane])] || "?");
        frag.appendChild(lbl);
      }
      var evs = laneEvents[lane];
      // visible slice via binary search on start
      var lo = 0, hi = evs.length;
      while (lo < hi) { var mid = (lo + hi) >> 1; if (evs[mid][1] + evs[mid][2] < vbX) lo = mid + 1; else hi = mid; }
      var bucket = null, bucketKey = -1, bucketMax = null, bucketCount = 0;
      function flushBucket() {
        if (!bucketMax) return;
        var r = document.createElementNS(svgNS, "rect");
        var bx = Math.max(xOf(bucket), marginL);
        var bw = Math.max((bucketMax[2]) * pxPerUs, 1);
        r.setAttribute("x", bx); r.setAttribute("y", y);
        r.setAttribute("width", bw); r.setAttribute("height", TL_LANE_H);
        r.setAttribute("fill", colorOf(OP_TYPE_COLOR, data.op_types[bucketMax[4]] || "unknown"));
        r.setAttribute("opacity", bucketCount > 1 ? ".45" : ".8");
        r.setAttribute("rx", 1);
        var t = document.createElementNS(svgNS, "title");
        t.textContent = (data.names[bucketMax[0]] || "?") + " · " + bucketMax[2].toFixed(1) + " μs" +
          (bucketCount > 1 ? " · +" + (bucketCount - 1) + " 个同像素事件（缩放聚合）" : "");
        r.appendChild(t);
        frag.appendChild(r);
        rendered++;
        bucketMax = null; bucketCount = 0;
      }
      for (var k = lo; k < evs.length && rendered < TL_MAX_RECTS; k++) {
        var e2 = evs[k];
        if (e2[1] > vbX + vbW) break;
        var redundant = e2[5] & 1;
        var wPx = e2[2] * pxPerUs;
        if (wPx < 1.5) {
          // decimate: one representative per pixel column per lane
          var key = Math.floor((e2[1] - vbX) * pxPerUs);
          if (key !== bucketKey) { flushBucket(); bucketKey = key; bucket = e2[1]; }
          bucketCount++;
          if (!bucketMax || e2[2] > bucketMax[2]) bucketMax = e2;
          continue;
        }
        flushBucket();
        var r2 = document.createElementNS(svgNS, "rect");
        r2.setAttribute("x", Math.max(xOf(e2[1]), marginL));
        r2.setAttribute("y", y);
        r2.setAttribute("width", wPx);
        r2.setAttribute("height", TL_LANE_H);
        r2.setAttribute("fill", colorOf(OP_TYPE_COLOR, data.op_types[e2[4]] || "unknown"));
        r2.setAttribute("opacity", redundant ? ".3" : ".85");
        r2.setAttribute("rx", 1);
        var t2 = document.createElementNS(svgNS, "title");
        t2.textContent = (data.names[e2[0]] || "?") + " · " + e2[2].toFixed(1) + " μs" + (redundant ? " · redundant" : "");
        r2.appendChild(t2);
        frag.appendChild(r2);
        rendered++;
      }
      flushBucket();
    }
    svg.appendChild(frag);
    var status = el("tl-status");
    if (status) {
      status.textContent = "窗口 " + (vbX / 1000).toFixed(2) + " → " + ((vbX + vbW) / 1000).toFixed(2) +
        " ms · 渲染 " + rendered + " rect" + (rendered >= TL_MAX_RECTS ? "（已达上限，放大可看细节）" : "");
    }
  }

  var rafPending = false;
  function scheduleRedraw() {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(function () { rafPending = false; redraw(); });
  }

  function zoom(factor, centerUs) {
    var c = centerUs != null ? centerUs : vbX + vbW / 2;
    var newW = Math.min(Math.max(vbW * factor, 0.001), totalSpan * 1.2);
    vbX = c - (c - vbX) * (newW / vbW);
    vbW = newW;
    if (vbX < 0) vbX = 0;
    if (vbX + vbW > totalSpan * 1.05) vbX = Math.max(0, totalSpan * 1.05 - vbW);
    scheduleRedraw();
  }
  function pan(frac) {
    vbX += vbW * frac;
    if (vbX < 0) vbX = 0;
    if (vbX + vbW > totalSpan * 1.05) vbX = Math.max(0, totalSpan * 1.05 - vbW);
    scheduleRedraw();
  }

  vp.addEventListener("wheel", function (e) {
    e.preventDefault();
    var rectBox = vp.getBoundingClientRect();
    var frac = (e.clientX - rectBox.left) / rectBox.width;
    zoom(e.deltaY < 0 ? 0.8 : 1.25, vbX + vbW * frac);
  }, { passive: false });

  var dragX = null;
  vp.addEventListener("mousedown", function (e) { dragX = e.clientX; vp.classList.add("dragging"); e.preventDefault(); });
  window.addEventListener("mousemove", function (e) {
    if (dragX == null) return;
    var dx = e.clientX - dragX;
    dragX = e.clientX;
    // convert client-pixel delta to time: plotW viewBox units span vbW μs,
    // and the viewBox's W units render at vp.clientWidth CSS pixels.
    var usPerPx = vbW * W / Math.max(vp.clientWidth * plotW, 1);
    vbX -= dx * usPerPx;
    if (vbX < 0) vbX = 0;
    scheduleRedraw();
  });
  window.addEventListener("mouseup", function () { dragX = null; vp.classList.remove("dragging"); });

  var mouseInside = false;
  vp.addEventListener("mouseenter", function () { mouseInside = true; });
  vp.addEventListener("mouseleave", function () { mouseInside = false; });
  window.addEventListener("keydown", function (e) {
    if (!mouseInside && document.activeElement !== vp) return;
    var ae = document.activeElement;
    if (ae && (ae.tagName === "INPUT" || ae.tagName === "SELECT")) return;
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var k2 = e.key.toLowerCase();
    if (k2 === "w") { zoom(0.82); e.preventDefault(); }
    else if (k2 === "s") { zoom(1.22); e.preventDefault(); }
    else if (k2 === "a") { pan(-0.1); e.preventDefault(); }
    else if (k2 === "d") { pan(0.1); e.preventDefault(); }
    else if (k2 === "0" || k2 === "=" || k2 === "escape") { vbX = 0; vbW = totalSpan; scheduleRedraw(); e.preventDefault(); }
  });

  el("tl-zi").addEventListener("click", function () { zoom(0.7); });
  el("tl-zo").addEventListener("click", function () { zoom(1.4); });
  el("tl-left").addEventListener("click", function () { pan(-0.25); });
  el("tl-right").addEventListener("click", function () { pan(0.25); });
  el("tl-reset").addEventListener("click", function () { vbX = 0; vbW = totalSpan; scheduleRedraw(); });

  redraw();
}

// ---------------------------------------------------------------------------
// findings view (rollup groups → findings → evidence links)
// ---------------------------------------------------------------------------

function renderFindings(route) {
  return loadAsset("findings").then(function (data) {
    var groups = data.groups || [];
    var gi = route.group != null ? route.group : 0;
    if (gi >= groups.length) gi = 0;
    var g = groups[gi];

    var html = '<div class="card"><h2 style="margin:0">Findings · rollup 分组</h2>' +
      '<div class="muted" style="font-size:11.5px">按 (type, severity, summary) 分组 · 共 ' + groups.length +
      ' 组 / ' + (data.findings || []).length + ' 条 finding · 点击组查看明细与 evidence 链接</div></div>';

    html += '<div class="card" style="margin-top:12px"><div class="scroll-x"><table><thead><tr>' +
      '<th>Type</th><th>Severity</th><th class="num">Occurrences</th><th>Summary</th></tr></thead><tbody>';
    groups.forEach(function (gr, i) {
      var sevCls = { critical: "b-danger", high: "b-danger", medium: "b-warn", low: "b-success", info: "b-info" }[gr.severity] || "b-info";
      html += '<tr class="clickable" data-route="findings" data-group="' + i + '"' +
        (i === gi ? ' style="background:rgba(9,105,218,.08)"' : "") + '>' +
        '<td><code>' + esc(gr.finding_type) + '</code></td>' +
        '<td><span class="badge ' + sevCls + '">' + esc(gr.severity) + '</span></td>' +
        '<td class="num">' + gr.occurrences + '</td>' +
        '<td>' + esc((gr.summary || "").slice(0, 120)) + '</td></tr>';
    });
    html += '</tbody></table></div></div>';

    if (g) {
      var members = (data.findings || []).filter(function (f) {
        return String(f.finding_type || f.type || "unknown") === g.finding_type &&
               String(f.severity || "info") === g.severity &&
               String(f.summary || "") === g.summary;
      });
      html += '<div class="card" style="margin-top:12px"><h3 style="margin-top:0">组明细 · <code>' + esc(g.finding_type) + '</code> · ' +
        members.length + ' 条</h3>';
      (g.knowledge_refs && g.knowledge_refs.length) && (
        html += '<div style="margin-bottom:8px"><b>Knowledge refs</b><ul style="margin:4px 0 0 18px">' +
          g.knowledge_refs.map(function (r) {
            var label = (r && (r.title || r.id)) || String(r);
            var url = r && r.url;
            return '<li>' + esc(label) + (url ? ' <a href="' + esc(url) + '" target="_blank" rel="noreferrer">链接</a>' : "") + '</li>';
          }).join("") + '</ul></div>');
      html += findingsDetailHTML(members, data);
      html += '</div>';
    }

    el("view-dynamic").innerHTML = html;
    wireInfoButtons();
    return "Findings";
  });
}

function findingsDetailHTML(members, data) {
  if (!members.length) return '<div class="muted">该组无成员。</div>';
  var out = [];
  members.forEach(function (f, i) {
    var evIds = (f.evidence_ids || []).concat(f.alignment_ids || []);
    var evLinks = evIds.map(function (eid) {
      var ev = (data.evidence || {})[eid];
      if (!ev) return '<span class="chip" title="evidence 不在 evidence_index.csv">' + esc(eid) + '</span>';
      var cls = (data.seg_to_class || {})[ev.segment_id];
      var layerKey = null;
      if (ev.layer_id && data.layer_index && data.layer_index[ev.layer_id]) {
        var li = data.layer_index[ev.layer_id];
        layerKey = li.layer_index + "-" + li.layer_role;
      }
      if (!cls) return '<span class="chip" title="segment 无 step_class">' + esc(eid) + '</span>';
      var tip = esc(ev.kind + " · " + (ev.summary || "") + " · rows " + ev.row_start + "–" + ev.row_end);
      return '<button class="back-btn" style="padding:2px 8px;margin:2px" title="' + tip + '"' +
        ' data-route="l2" data-cls="' + esc(cls) + '" data-seg="' + esc(ev.segment_id) + '"' +
        (layerKey ? ' data-layer="' + esc(layerKey) + '"' : "") + '>↦ ' + esc(eid.slice(0, 18)) + '…</button>';
    }).join(" ");
    var metrics = f.metrics && typeof f.metrics === "object" ?
      Object.keys(f.metrics).slice(0, 10).map(function (k) {
        return '<span class="chip" title="' + esc(k) + '">' + esc(k) + '=' + esc(String(f.metrics[k]).slice(0, 40)) + '</span>';
      }).join(" ") : "";
    var limitations = (f.limitations || []).map(function (x) { return "<li>" + esc(x) + "</li>"; }).join("");
    out.push('<details' + (i === 0 ? " open" : "") + '><summary><code>' + esc(f.claim_id || f.finding_id || ("finding " + (i + 1))) + '</code> ' +
      '<span class="badge b-info">' + esc(f.confidence || "") + '</span></summary>' +
      '<div style="margin-top:6px;font-size:12px">' +
      (f.summary ? '<div style="margin-bottom:6px">' + esc(f.summary) + '</div>' : "") +
      (metrics ? '<div style="margin-bottom:6px">' + metrics + '</div>' : "") +
      (evLinks ? '<div style="margin-bottom:6px"><b>Evidence</b>：' + evLinks + '</div>' : '<div class="muted">无 evidence 链接</div>') +
      (limitations ? '<div class="muted"><b>Limitations</b><ul style="margin:4px 0 0 18px">' + limitations + '</ul></div>' : "") +
      '</div></details>');
  });
  return out.join("");
}

// ---------------------------------------------------------------------------
// info popover (field docs)
// ---------------------------------------------------------------------------

var _openPopover = null;
function _closePopover() {
  if (_openPopover) { _openPopover.remove(); _openPopover = null; }
}
function _openFieldPopover(anchor) {
  _closePopover();
  var key = anchor.getAttribute("data-doc-key");
  var doc = FIELD_DOCS[key] || "(无字段说明)";
  var pop = document.createElement("div");
  pop.className = "info-popover";
  pop.innerHTML = '<span class="pop-close">×</span><div class="pop-title">' + esc(key) + '</div><div>' + esc(doc) + '</div>';
  document.body.appendChild(pop);
  var r = anchor.getBoundingClientRect();
  var w = pop.offsetWidth, h = pop.offsetHeight;
  var left = r.left + window.scrollX;
  if (left + w > window.innerWidth - 12) left = window.innerWidth - w - 12;
  var top = r.bottom + 6 + window.scrollY;
  if (r.bottom + h > window.innerHeight - 12) top = r.top + window.scrollY - h - 6;
  pop.style.left = Math.max(8, left) + "px";
  pop.style.top = Math.max(8, top) + "px";
  pop.querySelector(".pop-close").addEventListener("click", _closePopover);
  _openPopover = pop;
}
function wireInfoButtons(rootEl) {
  // handled by the global delegation below; kept for API symmetry
}
document.addEventListener("click", function (e) {
  var btn = e.target.closest(".info-btn");
  if (btn) {
    e.stopPropagation();
    _openFieldPopover(btn);
    return;
  }
  if (_openPopover && !e.target.closest(".info-popover")) _closePopover();
});
document.addEventListener("keydown", function (e) { if (e.key === "Escape") _closePopover(); });

// ---------------------------------------------------------------------------
// boot
// ---------------------------------------------------------------------------

initTheme();
initBanners();
updateChrome();

})();
