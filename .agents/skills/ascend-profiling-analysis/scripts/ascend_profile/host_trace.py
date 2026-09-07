#!/usr/bin/env python3
"""Host-side soft attribution for device bubble windows.

Salvaged from the retired user-level ``ascend-profiling-anomaly`` skill
(``scripts/reference_host_gap_branch.py:soft_attribution_for_bubble`` and
``references/rulebook.md`` §11). The idea: a device bubble (a gap between
merged device-busy segments) is matched against host-side events parsed
from ``trace_view.json`` (Chrome trace format: ``cpu_op`` /
``python_function`` / ``AscendCL`` categories), and annotated with
*candidate* root-cause labels -- sync/copy calls, communication waits,
host launch lag, or untraced host blocking. Labels are soft evidence,
never asserted root causes.

``trace_view.json`` can reach gigabytes, so parsing is streaming: a
brace-matching state machine extracts one event object at a time from
either CANN's top-level event array or a Chrome trace object wrapper,
and only events overlapping the bubble windows are retained.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

# Host-side categories documented in the retired skill's
# ``kernel_data_guide.md`` §1.3. Device-side categories (``kernel``,
# ``communication``) are deliberately excluded: the bubble is by
# construction free of device activity, and host coverage is the signal.
HOST_CATEGORIES = ("cpu_op", "python_function", "ascendcl")

# Substring prefilter so ``json.loads`` only runs on objects that can be
# host events at all (the file is dominated by device kernel events).
_HOST_CAT_TOKENS = tuple(f'"{cat}"' for cat in HOST_CATEGORIES) + ('"AscendCL"',)

# Fast-path scanner for token-free chunks: one JSON string (escape-aware)
# or one structural brace per match. Strings are skipped; braces drive the
# depth state machine. A trailing ``""`` sentinel is appended to the
# scanned text so a string dangling at the chunk boundary still matches.
_SCAN_RE = re.compile(r'"[^"\\]*(?:\\.[^"\\]*)*"|[{}]')

# Marker matchers for rulebook §11 families. Conservative on purpose:
# ``aten::to`` is matched exactly because the substring would also match
# ``aten::topk``; bare ``broadcast`` is excluded because it matches
# compute ops such as ``aten::broadcast_to``.
_SYNC_EXACT_NAMES = {"aten::to", "aten::to_copy"}
_SYNC_SUBSTRINGS = ("synchronize", "memcpy", "aten::copy_", "aten::_to_copy")
_COMM_SUBSTRINGS = (
    "hcom",
    "hccl",
    "allreduce",
    "all_reduce",
    "allgather",
    "all_gather",
    "reducescatter",
    "reduce_scatter",
    "alltoall",
    "all_to_all",
    "c10d",
)

# Rulebook §11 thresholds.
SYNC_OVERLAP_RATIO = 0.2
COMM_OVERLAP_RATIO = 0.2
UNTRACED_HOST_COVERAGE = 0.05
LAUNCH_LAG_HOST_COVERAGE = 0.1
# ``host_thread_count < 1.2`` == at most one host thread active in the
# window; same boundary as the retired skill's ``host_parallelism_hint``.
SINGLE_THREAD_HINT = 1.2

# Hard cap on retained host events per rank so a pathological
# trace_view.json cannot exhaust analysis-host memory. Attribution only
# needs events overlapping the bubble windows, so this is generous.
MAX_HOST_EVENTS = 200_000


@dataclass(frozen=True)
class HostEvent:
    name: str
    cat: str
    ts_us: float
    dur_us: float
    pid: Any = None
    tid: str = ""

    @property
    def end_us(self) -> float:
        return self.ts_us + self.dur_us


def _iter_trace_objects(path: Path, *, chunk_size: int = 1 << 20) -> Iterator[str]:
    """Yield raw ``{...}`` object texts nested inside the trace document.

    CANN emits both a top-level event array and a Chrome trace object
    wrapper. We walk the text with a brace-depth state machine
    (string/escape aware, so braces inside ``args`` strings do not
    confuse it) and yield every outermost event candidate below the
    root. Memory stays bounded by the largest single event object,
    independent of file size.

    Chunk-level prefilter: the file is dominated by device kernel events,
    so a whole 1 MiB chunk that contains no host-category token is
    scanned with a regex state machine (whole-string / brace matches
    only, never per-char Python) that never builds object text. A
    complete object inside such a chunk provably cannot be a host event
    (its category token would be in the chunk); it is reported as an
    empty string so callers still count one scanned object per event
    object. An object *carried* across the boundary (opened in an earlier
    chunk) keeps appending: its buffered prefix may hold a token, so when
    it closes the full text is yielded as usual. An object that opens
    inside a filtered chunk and stays open at the boundary is carried as
    a raw slice, so an event whose token lands in a later chunk —
    including one split exactly across the boundary — is still yielded
    intact.
    """

    depth = 0
    root_token: str | None = None
    obj_depth: int | None = None
    buf: list[str] = []
    in_string = False
    escape = False
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            if not any(token in chunk for token in _HOST_CAT_TOKENS):
                # Fast path for token-free chunks. No object fully inside
                # this chunk can be a host event (its category token would
                # be in the chunk), so complete objects are reported as ""
                # — one placeholder per object, keeping ``objects_scanned``
                # counting exact without building any object text.
                #
                # ``appending`` marks a carried object (opened in an earlier
                # chunk; its prefix lives in ``buf`` and may hold a token,
                # so it is finished in full and yielded verbatim when it
                # closes). An object that opens here and stays open at the
                # boundary is carried as a raw slice (``obj_start``), so an
                # event whose token lands in a later chunk — including one
                # split exactly across the boundary — is reassembled intact.
                if root_token is None:
                    for probe in chunk:
                        if not (probe.isspace() or probe == "\ufeff"):
                            root_token = probe
                            break
                event_depth = 1 if root_token == "[" else 2
                appending = obj_depth is not None
                obj_start = -1
                i = 0
                n = len(chunk)
                if in_string:
                    # String continuation from the previous chunk (a carried
                    # object sliced mid-string lands here).
                    j = 1 if escape else 0
                    while True:
                        k = chunk.find('"', j)
                        if k < 0:
                            if appending:
                                buf.append(chunk)
                            backslashes = 0
                            p = n - 1
                            while p >= j and chunk[p] == "\\":
                                backslashes += 1
                                p -= 1
                            escape = backslashes % 2 == 1
                            i = n
                            break
                        backslashes = 0
                        p = k - 1
                        while p >= j and chunk[p] == "\\":
                            backslashes += 1
                            p -= 1
                        if backslashes % 2 == 1:
                            j = k + 1
                            continue
                        if appending:
                            buf.append(chunk[: k + 1])
                        in_string = False
                        escape = False
                        i = k + 1
                        break
                # Regex scan: whole strings (escape-aware) or single
                # braces, so Python only touches matches, never raw chars.
                # The trailing ``""`` sentinel terminates a dangling string
                # so braces inside it are not mistaken for structure; a
                # match starting inside the sentinel (``s >= n``) is an
                # artifact and ends the scan. ``cursor`` tracks the
                # appended prefix of a carried object.
                cursor = i
                for match in _SCAN_RE.finditer(chunk + '""', i):
                    s, e = match.span()
                    if s >= n:
                        break
                    ch = chunk[s]
                    if ch == '"':
                        if e <= n:
                            continue  # complete string inside the chunk
                        # Dangling string running to the chunk end; the
                        # sentinel supplied its closing quote.
                        in_string = True
                        backslashes = 0
                        p = n - 1
                        while p > s and chunk[p] == "\\":
                            backslashes += 1
                            p -= 1
                        escape = backslashes % 2 == 1
                        break
                    if ch == "{":
                        depth += 1
                        if depth >= event_depth and obj_depth is None:
                            obj_depth = depth
                            obj_start = s
                        continue
                    # ch == "}"
                    if obj_depth is not None and depth == obj_depth:
                        if appending:
                            buf.append(chunk[cursor : s + 1])
                            yield "".join(buf)
                        else:
                            yield ""
                        obj_depth = None
                        buf = []
                        appending = False
                        obj_start = -1
                    depth = max(0, depth - 1)
                if appending:
                    buf.append(chunk[cursor:])
                if obj_depth is not None and not appending:
                    buf.append(chunk[obj_start:])
                continue
            for ch in chunk:
                if root_token is None and not (ch.isspace() or ch == "\ufeff"):
                    root_token = ch
                if in_string:
                    if obj_depth is not None:
                        buf.append(ch)
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                    if obj_depth is not None:
                        buf.append(ch)
                    continue
                if ch == "{":
                    depth += 1
                    # A top-level array contains event objects at brace
                    # depth 1. A wrapped Chrome trace keeps them below
                    # the root object at brace depth 2.
                    event_depth = 1 if root_token == "[" else 2
                    if depth >= event_depth and obj_depth is None:
                        obj_depth = depth
                        buf.append("{")
                    elif obj_depth is not None:
                        buf.append(ch)
                    continue
                if ch == "}":
                    if obj_depth is not None:
                        buf.append(ch)
                        if depth == obj_depth:
                            yield "".join(buf)
                            obj_depth = None
                            buf = []
                    depth = max(0, depth - 1)
                    continue
                if obj_depth is not None:
                    buf.append(ch)


def _host_event_from_object(text: str) -> HostEvent | None:
    if not any(token in text for token in _HOST_CAT_TOKENS):
        return None
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    cat = str(obj.get("cat") or "").lower()
    if cat not in HOST_CATEGORIES:
        return None
    # Only complete (``ph == "X"``) events carry a duration; B/E pairs
    # and instant markers cannot feed interval overlap.
    if obj.get("ph") != "X":
        return None
    try:
        ts_us = float(obj.get("ts"))
        dur_us = float(obj.get("dur"))
    except (TypeError, ValueError):
        return None
    if dur_us <= 0:
        return None
    return HostEvent(
        name=str(obj.get("name") or ""),
        cat=cat,
        ts_us=ts_us,
        dur_us=dur_us,
        pid=obj.get("pid"),
        tid=str(obj.get("tid") or ""),
    )


def host_marker_kind(event: HostEvent) -> str | None:
    """Classify a host event as a ``sync`` / ``comm`` marker, or None."""

    name = event.name.lower()
    if event.name in _SYNC_EXACT_NAMES or any(token in name for token in _SYNC_SUBSTRINGS):
        return "sync"
    if any(token in name for token in _COMM_SUBSTRINGS):
        return "comm"
    return None


def collect_host_events(
    path: Path,
    windows: Sequence[tuple[float, float]],
    *,
    max_events: int = MAX_HOST_EVENTS,
    chunk_size: int = 1 << 20,
) -> tuple[list[HostEvent], dict[str, Any]]:
    """Stream ``trace_view.json`` and retain host events overlapping the
    bounding span of ``windows`` ((start_us, end_us) bubble pairs).

    Returns ``(events, stats)``; ``stats["truncated"]`` is True when the
    retention cap fired, in which case coverage ratios may undercount.
    ``chunk_size`` is the streaming read size; it exists so tests can
    exercise cross-chunk boundary behaviour without gigabyte fixtures.
    """

    stats: dict[str, Any] = {
        "objects_scanned": 0,
        "host_events_seen": 0,
        "retained": 0,
        "truncated": False,
    }
    if not windows:
        return [], stats
    lower = min(start for start, _ in windows)
    upper = max(end for _, end in windows)
    retained: list[HostEvent] = []
    for raw in _iter_trace_objects(path, chunk_size=chunk_size):
        stats["objects_scanned"] += 1
        event = _host_event_from_object(raw)
        if event is None:
            continue
        stats["host_events_seen"] += 1
        if event.ts_us >= upper or event.end_us <= lower:
            continue
        if len(retained) >= max_events:
            stats["truncated"] = True
            continue
        retained.append(event)
    retained.sort(key=lambda event: (event.ts_us, event.end_us))
    stats["retained"] = len(retained)
    return retained, stats


def _union_overlap_us(start_us: float, end_us: float, events: Iterable[HostEvent]) -> float:
    """Union of ``[start_us, end_us]`` overlap against event intervals."""

    clipped: list[tuple[float, float]] = []
    for event in events:
        if event.ts_us >= end_us:
            break  # events are sorted by ts_us
        left = max(start_us, event.ts_us)
        right = min(end_us, event.end_us)
        if right > left:
            clipped.append((left, right))
    total = 0.0
    cur_start: float | None = None
    cur_end = 0.0
    for left, right in clipped:
        if cur_start is None:
            cur_start, cur_end = left, right
        elif left <= cur_end:
            cur_end = max(cur_end, right)
        else:
            total += cur_end - cur_start
            cur_start, cur_end = left, right
    if cur_start is not None:
        total += cur_end - cur_start
    return total


def soft_attribution_for_window(
    start_us: float,
    end_us: float,
    host_events: Sequence[HostEvent],
) -> dict[str, Any]:
    """Rulebook §11: map one bubble window to soft root-cause candidates.

    Thresholds are exactly the retired skill's: marker overlap >= 0.2,
    untraced host coverage < 0.05, launch-lag coverage floor 0.1, and the
    single-host-thread hint boundary 1.2 (here derived as the count of
    distinct host threads with events overlapping the window).
    """

    duration_us = max(0.0, end_us - start_us)
    if duration_us <= 0:
        return {
            "host_visible_coverage_ratio": 0.0,
            "sync_marker_overlap_ratio": 0.0,
            "comm_marker_overlap_ratio": 0.0,
            "host_thread_count": 0,
            "soft_root_cause_labels": ["insufficient_evidence"],
        }
    overlapping = [
        event
        for event in host_events
        if event.ts_us < end_us and event.end_us > start_us
    ]
    sync_events = [event for event in overlapping if host_marker_kind(event) == "sync"]
    comm_events = [event for event in overlapping if host_marker_kind(event) == "comm"]
    host_cov = _union_overlap_us(start_us, end_us, overlapping) / duration_us
    sync_cov = _union_overlap_us(start_us, end_us, sync_events) / duration_us
    comm_cov = _union_overlap_us(start_us, end_us, comm_events) / duration_us
    thread_count = len({event.tid for event in overlapping})

    labels: list[str] = []
    if sync_cov >= SYNC_OVERLAP_RATIO:
        labels.append("possible_sync_or_h2d")
    if comm_cov >= COMM_OVERLAP_RATIO:
        labels.append("possible_comm_wait")
    if host_cov < UNTRACED_HOST_COVERAGE:
        labels.append("possible_untraced_host_blocking")
    if not labels and host_cov >= LAUNCH_LAG_HOST_COVERAGE:
        labels.append("possible_host_launch_lag")
    if not labels and thread_count < SINGLE_THREAD_HINT:
        labels.append("possible_python_serialization_or_lock")
    if not labels:
        labels.append("insufficient_evidence")
    return {
        "host_visible_coverage_ratio": round(host_cov, 6),
        "sync_marker_overlap_ratio": round(sync_cov, 6),
        "comm_marker_overlap_ratio": round(comm_cov, 6),
        "host_thread_count": thread_count,
        "soft_root_cause_labels": labels,
    }


def trace_view_paths_by_rank(source_index: Mapping[str, Any]) -> dict[str, Path]:
    """First registered ``trace_view.json`` per rank from ``source_index.json``.

    ``normalize.supplemental_sources`` registers every glob match in
    sorted order; attributing against the first keeps the choice
    deterministic.
    """

    out: dict[str, Path] = {}
    for source in source_index.get("sources") or []:
        if not isinstance(source, Mapping):
            continue
        if source.get("kind") != "trace_view_json":
            continue
        rank_id = str(source.get("rank_id") or "")
        path_text = str(source.get("path") or "")
        if not rank_id or not path_text or rank_id in out:
            continue
        out[rank_id] = Path(path_text)
    return out


def attribute_bubbles(
    bubbles: list[dict[str, Any]],
    trace_paths_by_rank: Mapping[str, Path],
    *,
    max_events: int = MAX_HOST_EVENTS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Attach ``soft_attribution`` to ``bubble_windows.jsonl`` rows.

    Graceful degradation: ranks without a registered/readable
    ``trace_view.json`` keep ``soft_attribution = None`` and are listed
    in the status limitations; the bubble facts themselves are untouched.
    """

    rows_by_rank: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in bubbles:
        rows_by_rank[str(row.get("rank_id") or "")].append(row)
    for row in bubbles:
        row["soft_attribution"] = None

    status: dict[str, Any] = {
        "status": "no_bubbles" if not bubbles else "missing",
        "bubbles_total": len(bubbles),
        "bubbles_attributed": 0,
        "ranks_with_trace": [],
        "ranks_with_host_events": [],
        "ranks_without_trace": [],
        "ranks_without_host_events": [],
        "trace_objects_scanned": 0,
        "host_events_seen": 0,
        "host_events_retained": 0,
        "truncated": False,
        "limitations": [],
    }
    if not bubbles:
        return bubbles, status

    for rank_id, rows in sorted(rows_by_rank.items()):
        path = trace_paths_by_rank.get(rank_id)
        if path is None or not path.is_file():
            status["ranks_without_trace"].append(rank_id)
            continue
        windows = [(float(row.get("start_us") or 0.0), float(row.get("end_us") or 0.0)) for row in rows]
        events, stats = collect_host_events(path, windows, max_events=max_events)
        status["ranks_with_trace"].append(rank_id)
        status["trace_objects_scanned"] += int(stats["objects_scanned"])
        status["host_events_seen"] += int(stats["host_events_seen"])
        status["host_events_retained"] += int(stats["retained"])
        status["truncated"] = bool(status["truncated"] or stats["truncated"])
        if not stats["host_events_seen"]:
            status["ranks_without_host_events"].append(rank_id)
            continue
        for row, (start_us, end_us) in zip(rows, windows):
            attribution = soft_attribution_for_window(start_us, end_us, events)
            attribution["source_path"] = str(path)
            row["soft_attribution"] = attribution
        status["bubbles_attributed"] += len(rows)
        status["ranks_with_host_events"].append(rank_id)

    if status["ranks_with_host_events"]:
        status["status"] = (
            "partial"
            if status["ranks_without_trace"] or status["ranks_without_host_events"]
            else "ok"
        )
    if status["ranks_without_trace"]:
        status["limitations"].append(
            "trace_view.json not available for rank(s) "
            + ", ".join(status["ranks_without_trace"])
            + "; bubble soft attribution skipped there and host-side root causes are not asserted."
        )
    if status["ranks_without_host_events"]:
        status["limitations"].append(
            "no complete host events were parsed from trace_view.json for rank(s) "
            + ", ".join(status["ranks_without_host_events"])
            + "; bubble soft attribution skipped there because an empty host-event set cannot distinguish missing host data from untraced host blocking."
        )
    if status["truncated"]:
        status["limitations"].append(
            f"host event retention capped at {max_events} per rank; soft-attribution coverage ratios may undercount very large captures."
        )
    return bubbles, status
