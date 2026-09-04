"""Boundary tests for the ``host_trace`` chunk-level token prefilter.

``_iter_trace_objects`` skips object-text building for whole chunks that
contain no host-category token, while carrying cross-chunk fragments.
These tests pin the exact-parse contract (events + stats) under tiny
chunk sizes, a category token split exactly across a chunk boundary, and
stats fidelity on fully token-free (device-only) content.
"""

from __future__ import annotations

import json

import conftest  # noqa: F401 — registers sys.path

from ascend_profile import host_trace


def _mixed_events() -> list[dict]:
    head = [
        {"ph": "X", "ts": 50.0, "dur": 10.0, "name": "aten::early", "cat": "cpu_op", "pid": 1, "tid": 1},
        {"ph": "X", "ts": 1000.0, "dur": 500.0, "name": "MatMulV2", "cat": "kernel", "pid": 0, "tid": 2},
        # Braces inside a string arg must not confuse the brace matcher.
        {
            "ph": "X",
            "ts": 1100.0,
            "dur": 600.0,
            "name": "aclrtSynchronizeStream",
            "cat": "AscendCL",
            "pid": 1,
            "tid": 7,
            "args": {"Call Stack": "a.py:1 -> {weird}"},
        },
        {"ph": "X", "ts": 1200.0, "dur": 100.0, "name": "aten::linear", "cat": "cpu_op", "pid": 1, "tid": 1},
        {"ph": "X", "ts": 1300.0, "dur": 100.0, "name": "train.py:step", "cat": "python_function", "pid": 1, "tid": 1},
        {"ph": "i", "name": "ProfilerStep#1", "cat": "user_annotation", "pid": 1, "tid": 1, "ts": 1000.0},
        {"ph": "M", "name": "process_name", "pid": 1, "tid": 0, "args": {"name": "python"}},
    ]
    # Pure device traffic so several small chunks in a row take the fast path.
    kernels = [
        {"ph": "X", "ts": 2000.0 + index * 10, "dur": 5.0, "name": f"Kernel{index}", "cat": "kernel", "pid": 0, "tid": 2}
        for index in range(50)
    ]
    tail = [
        {"ph": "X", "ts": 9000.0, "dur": 42.0, "name": "aten::relu", "cat": "cpu_op", "pid": 1, "tid": 3},
    ]
    return head + kernels + tail


def test_small_chunks_match_large_chunk_parse_wrapped(tmp_path) -> None:
    trace = tmp_path / "trace_view.json"
    trace.write_text(
        json.dumps({"traceEvents": _mixed_events(), "deviceProperties": [{"id": 0}]}),
        encoding="utf-8",
    )
    windows = [(900.0, 10000.0)]
    big_events, big_stats = host_trace.collect_host_events(trace, windows)
    assert [event.name for event in big_events] == [
        "aclrtSynchronizeStream",
        "aten::linear",
        "train.py:step",
        "aten::relu",
    ]
    for chunk_size in (7, 64, 1024):
        events, stats = host_trace.collect_host_events(trace, windows, chunk_size=chunk_size)
        assert events == big_events, f"chunk_size={chunk_size}: events diverged"
        assert stats == big_stats, f"chunk_size={chunk_size}: stats diverged"


def test_small_chunks_match_large_chunk_parse_cann_array(tmp_path) -> None:
    trace = tmp_path / "trace_view.json"
    trace.write_text(json.dumps(_mixed_events()), encoding="utf-8")
    windows = [(900.0, 10000.0)]
    big_events, big_stats = host_trace.collect_host_events(trace, windows)
    for chunk_size in (13, 4096):
        events, stats = host_trace.collect_host_events(trace, windows, chunk_size=chunk_size)
        assert events == big_events, f"chunk_size={chunk_size}: events diverged"
        assert stats == big_stats, f"chunk_size={chunk_size}: stats diverged"


def test_category_token_split_across_chunk_boundary(tmp_path) -> None:
    """The ``"cpu_op"`` token is split exactly across two chunks, so
    neither chunk contains it whole. The event must still parse: the
    object carrying the token spans the boundary and the fast path keeps
    the fragment."""
    trace = tmp_path / "trace_view.json"
    text = json.dumps(
        [
            {"ph": "X", "ts": 10.0, "dur": 5.0, "name": "MatMulV2", "cat": "kernel", "pid": 0, "tid": 2},
            {"ph": "X", "ts": 100.0, "dur": 50.0, "name": "aten::relu", "cat": "cpu_op", "pid": 1, "tid": 1},
        ]
    )
    trace.write_text(text, encoding="utf-8")
    boundary = text.index('"cpu_op"') + 3  # cut inside the token
    events, stats = host_trace.collect_host_events(trace, [(0.0, 1000.0)], chunk_size=boundary)
    assert [event.name for event in events] == ["aten::relu"]
    assert stats["objects_scanned"] == 2
    assert stats["host_events_seen"] == 1


def test_fragment_carried_when_token_arrives_in_later_chunk(tmp_path) -> None:
    """The host object opens inside a fully token-free chunk; its
    ``"cpu_op"`` token only appears in the next chunk. The carried
    fragment must rebuild the object exactly."""
    trace = tmp_path / "trace_view.json"
    text = json.dumps(
        [
            {"ph": "X", "ts": 10.0, "dur": 5.0, "name": "MatMulV2", "cat": "kernel", "pid": 0, "tid": 2},
            {"ph": "X", "ts": 100.0, "dur": 50.0, "name": "aten::relu", "cat": "cpu_op", "pid": 1, "tid": 1},
        ]
    )
    trace.write_text(text, encoding="utf-8")
    boundary = text.index('"aten::relu"') + 5  # chunk 1: token-free, host object open
    assert not any(token in text[:boundary] for token in host_trace._HOST_CAT_TOKENS)
    events, stats = host_trace.collect_host_events(trace, [(0.0, 1000.0)], chunk_size=boundary)
    assert [event.name for event in events] == ["aten::relu"]
    assert stats["objects_scanned"] == 2
    assert stats["host_events_seen"] == 1


def test_token_free_trace_still_counts_scanned_objects(tmp_path) -> None:
    """Device-only trace: every chunk takes the fast path, objects are
    dropped as empty candidates, and ``objects_scanned`` stays exact."""
    trace = tmp_path / "trace_view.json"
    text = json.dumps(
        [
            {"ph": "X", "ts": 1.0 + index, "dur": 1.0, "name": f"Kernel{index}", "cat": "kernel", "pid": 0, "tid": 2}
            for index in range(20)
        ]
    )
    trace.write_text(text, encoding="utf-8")
    events, stats = host_trace.collect_host_events(trace, [(0.0, 1000.0)], chunk_size=17)
    assert events == []
    assert stats["objects_scanned"] == 20
    assert stats["host_events_seen"] == 0


def test_fast_path_reports_empty_candidate_per_dropped_object(tmp_path) -> None:
    """A fully token-free chunk drops every complete object as ``""`` —
    one placeholder per object, so ``objects_scanned`` keeps counting
    scanned objects exactly as the full state machine did."""
    trace = tmp_path / "trace_view.json"
    text = json.dumps(
        [
            {"ph": "X", "ts": 1.0 + index, "dur": 1.0, "name": f"Kernel{index}", "cat": "kernel", "pid": 0, "tid": 2}
            for index in range(20)
        ]
    )
    trace.write_text(text, encoding="utf-8")
    # Whole file fits in one token-free chunk (default 1 MiB chunk).
    raw = list(host_trace._iter_trace_objects(trace))
    assert raw == [""] * 20


def test_fuzz_nasty_strings_match_reference_parse(tmp_path) -> None:
    """Randomized traces with braces/quotes/backslashes inside string
    values: parsing at any chunk size must match the whole-document
    ``json.loads`` reference, byte-identical host events and stats."""
    import random

    nasty_fragments = [
        "{weird}",
        '"}}{{"',
        "\\\\",
        '\\"',
        "a\\b}c",
        'quote"inside',
        "{[()]}",
        "cpu_op" ,  # token text inside a *kernel* name: must not matter (cat decides)
        "\n\t混合 utf8",
    ]
    for seed in range(8):
        rng = random.Random(seed)
        events = []
        for index in range(rng.randrange(30, 60)):
            kind = rng.choice(["kernel", "kernel", "kernel", "cpu_op", "python_function", "AscendCL", "user_annotation"])
            name = f"op{index}_" + "".join(rng.choice(nasty_fragments) for _ in range(rng.randrange(0, 3)))
            event = {
                "ph": rng.choice(["X", "X", "X", "i", "M"]),
                "ts": float(rng.randrange(0, 100000)),
                "name": name,
                "cat": kind,
                "pid": rng.randrange(0, 3),
                "tid": str(rng.randrange(0, 5)),
                "args": {"note": "".join(rng.choice(nasty_fragments) for _ in range(rng.randrange(0, 2)))},
            }
            if event["ph"] == "X":
                event["dur"] = float(rng.randrange(1, 500))
            events.append(event)
        wrapped = seed % 2 == 0
        doc = {"traceEvents": events, "deviceProperties": []} if wrapped else events
        text = json.dumps(doc)
        trace = tmp_path / f"trace_{seed}.json"
        trace.write_text(text, encoding="utf-8")

        # Reference: whole-document parse with the same filters as
        # ``_host_event_from_object`` and the window retention rule.
        host_seen = sum(
            1
            for item in events
            if str(item.get("cat") or "").lower() in host_trace.HOST_CATEGORIES
            and item.get("ph") == "X"
            and float(item.get("dur", 0) or 0) > 0
        )
        windows = [(100.0, 90000.0)]
        reference = sorted(
            (
                host_trace.HostEvent(
                    name=str(item.get("name") or ""),
                    cat=str(item.get("cat") or "").lower(),
                    ts_us=float(item["ts"]),
                    dur_us=float(item["dur"]),
                    pid=item.get("pid"),
                    tid=str(item.get("tid") or ""),
                )
                for item in events
                if str(item.get("cat") or "").lower() in host_trace.HOST_CATEGORIES
                and item.get("ph") == "X"
                and float(item.get("dur", 0) or 0) > 0
                and float(item["ts"]) < 90000.0
                and float(item["ts"]) + float(item["dur"]) > 100.0
            ),
            key=lambda event: (event.ts_us, event.end_us),
        )
        for chunk_size in (1 << 20, 1, 5, 37, 256, 4096):
            parsed, stats = host_trace.collect_host_events(trace, windows, chunk_size=chunk_size)
            assert parsed == reference, f"seed={seed} chunk_size={chunk_size}: events diverged"
            assert stats["objects_scanned"] == len(events), f"seed={seed} chunk_size={chunk_size}"
            assert stats["host_events_seen"] == host_seen, f"seed={seed} chunk_size={chunk_size}"


if __name__ == "__main__":
    test_fast_path_reports_empty_candidate_per_dropped_object(__import__("pathlib").Path("."))
    print("ok")
