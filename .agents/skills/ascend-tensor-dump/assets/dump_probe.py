"""Bounded tensor dump probe for locating numerical divergence on Ascend NPU.

Copy this file into the package under test (for example
``vllm_ascend/dump_probe.py``) and call the helpers from the stages you want to
observe. The probe is deliberately small: it has no schema negotiation, no
checksum verification, and no self-audit. Its whole job is to answer one
question cheaply -- *which stage goes wrong first* -- and then, only when you
ask for it, hand you the actual tensors at that stage.

Design rules baked in
---------------------
1. Off by default. When ``DUMP_PROBE`` is unset every entry point is a cheap
   no-op, so instrumented code can be left in a branch between experiments.
2. Summaries first, tensors on request. Full clones only happen for stages
   matching ``DUMP_PROBE_TENSOR``. Summaries are computed on device and read
   back exactly once, in ``finish()``.
3. One device-to-host copy per armed forward. Nothing calls ``.cpu()``,
   ``.item()`` or ``.tolist()`` during the forward pass, because a dump that
   synchronizes can complete an asynchronous copy that the bug depends on and
   make the symptom disappear.
4. Graph mode uses pre-allocated slots. See ``graph_slot`` /
   ``capture_graph``: the copy happens inside the graph, the read-back happens
   after replay.
5. Physical layout is recorded next to the numbers. ``stride``,
   ``storage_ptr`` and the NPU format survive into the manifest, because
   ``contiguous().cpu()`` erases exactly the evidence that cache-aliasing bugs
   leave behind.

Environment contract
--------------------
``DUMP_PROBE``             ``1`` enables the probe. Anything else disables it.
``DUMP_PROBE_DIR``         Output directory. Required when enabled.
``DUMP_PROBE_RANKS``       Comma-separated global ranks to arm. Default ``0``.
``DUMP_PROBE_MATCH``       1-based index of the *distinct label* to arm.
                           Default ``1``.
``DUMP_PROBE_OCCURRENCE``  1-based call index within that label. Default ``1``.
                           Chunked prefill calls one label several times; this
                           is how you pick a later chunk instead of the first.
``DUMP_PROBE_SUMMARY``     Regex of stages that get device-side statistics.
                           Empty means every captured stage. Default empty.
``DUMP_PROBE_TENSOR``      Regex of stages that get a full tensor clone.
                           Empty means none. Default empty -- this is the cost
                           gate, set it only once summaries point somewhere.
``DUMP_PROBE_ROWS``        Clone at most this many rows along dim 0.
                           ``0`` means no limit. Default ``0``.
``DUMP_PROBE_ENABLE_FILE`` Optional sentinel path. When set, the probe stays
                           disabled until the file exists, so a long-running
                           service can be armed without a restart.

Usage sketch
------------
Eager path::

    from vllm_ascend import dump_probe

    # once per forward, at the scheduling boundary
    dump_probe.arm(request_id, num_computed_tokens=n_computed)
    ...
    dump_probe.capture("layers.23.self_attn.kv", kv)
    ...
    dump_probe.finish()          # after logits exist

Graph path::

    # at module __init__, never inside forward
    self._slot = dump_probe.graph_slot("layers.0.attn.out", (32, 4096), dtype)
    ...
    # inside forward: baked into the captured graph as a device-to-device copy
    dump_probe.capture_graph("layers.0.attn.out", out)
    ...
    # outside the graph, after replay
    dump_probe.finish()

Single-operator path::

    dump_probe.capture_inputs(
        "gmm1", hidden=hidden, weight=w, group_list=group_list, trans_b=True
    )

Then replay the saved inputs standalone with ``assets/replay_op.py``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PROBE_ID = "ascend-tensor-dump/1"

STAT_NAMES = ("nan_count", "inf_count", "max_abs", "min", "max", "mean")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_ranks() -> set[int]:
    raw = _env("DUMP_PROBE_RANKS", "0")
    ranks: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ranks.add(int(part))
        except ValueError:
            continue
    return ranks or {0}


def _is_tensor(value: Any) -> bool:
    """Duck-typed tensor check so this module imports without torch."""
    return hasattr(value, "detach") and hasattr(value, "shape")


class _Probe:
    """All probe state. One instance per process, held in ``_PROBE``."""

    def __init__(self) -> None:
        self.armed = False
        self.label: str | None = None
        self.rank = 0
        self.match_index = 0
        self.occurrence = 0
        self.metadata: dict[str, Any] = {}
        self.records: list[dict[str, Any]] = []
        self.stat_tensors: list[Any] = []
        self.tensors: list[tuple[str, Any]] = []
        self.input_sets: list[tuple[str, dict[str, Any]]] = []
        self.graph_slots: dict[str, Any] = {}
        self.graph_meta: dict[str, dict[str, Any]] = {}
        self.label_order: dict[str, int] = {}
        self.label_calls: dict[str, int] = {}
        self.dumped_labels: set[str] = set()

    def reset_capture(self) -> None:
        self.armed = False
        self.label = None
        self.metadata = {}
        self.records = []
        self.stat_tensors = []
        self.tensors = []
        self.input_sets = []


_PROBE = _Probe()


def enabled() -> bool:
    """True when the master switch and optional sentinel both allow dumping."""
    if _env("DUMP_PROBE") != "1":
        return False
    sentinel = _env("DUMP_PROBE_ENABLE_FILE")
    if sentinel and not os.path.exists(sentinel):
        return False
    return True


def armed() -> bool:
    """True while a forward is armed. Use this to gate expensive callers."""
    return _PROBE.armed


def _resolve_rank() -> int:
    try:
        import torch.distributed as dist

        if dist.is_available() and dist.is_initialized():
            return int(dist.get_rank())
    except Exception:
        pass
    for name in ("RANK", "LOCAL_RANK"):
        raw = os.getenv(name, "").strip()
        if raw:
            try:
                return int(raw)
            except ValueError:
                continue
    return 0


def arm(label: str, rank: int | None = None, **metadata: Any) -> bool:
    """Arm one forward for ``label``. Returns True when this call is armed.

    ``label`` should identify the *unit of work*, normally a request id. The
    probe counts distinct labels for ``DUMP_PROBE_MATCH`` and counts repeated
    calls of the same label for ``DUMP_PROBE_OCCURRENCE``. Chunked prefill
    therefore selects one request rather than being mistaken for three.
    """
    probe = _PROBE
    if probe.armed:
        return False
    if not enabled():
        return False

    resolved_rank = _resolve_rank() if rank is None else int(rank)
    if resolved_rank not in _env_ranks():
        return False

    key = str(label)
    if key not in probe.label_order:
        probe.label_order[key] = len(probe.label_order) + 1
    probe.label_calls[key] = probe.label_calls.get(key, 0) + 1

    if key in probe.dumped_labels:
        return False
    if probe.label_order[key] != _env_int("DUMP_PROBE_MATCH", 1):
        return False
    if probe.label_calls[key] != _env_int("DUMP_PROBE_OCCURRENCE", 1):
        return False

    probe.reset_capture()
    probe.armed = True
    probe.label = key
    probe.rank = resolved_rank
    probe.match_index = probe.label_order[key]
    probe.occurrence = probe.label_calls[key]
    probe.metadata = dict(metadata)
    return True


def _layout(tensor: Any) -> dict[str, Any]:
    """Record logical shape and physical placement side by side."""
    info: dict[str, Any] = {
        "shape": list(tensor.shape),
        "dtype": str(tensor.dtype),
        "numel": int(tensor.numel()),
    }
    try:
        info["device"] = str(tensor.device)
    except Exception:
        pass
    try:
        info["stride"] = list(tensor.stride())
        info["contiguous"] = bool(tensor.is_contiguous())
    except Exception:
        pass
    try:
        info["storage_ptr"] = int(tensor.untyped_storage().data_ptr())
        info["storage_offset"] = int(tensor.storage_offset())
    except Exception:
        pass
    try:
        import torch_npu

        info["npu_format"] = int(torch_npu.get_npu_format(tensor))
    except Exception:
        pass
    return info


def _device_stats(tensor: Any) -> Any:
    """Stack six statistics into one device tensor. No host read-back here."""
    import torch

    flat = tensor.reshape(-1).to(torch.float32)
    isnan = torch.isnan(flat)
    isinf = torch.isinf(flat)
    finite = ~(isnan | isinf)
    safe = torch.where(finite, flat, torch.zeros_like(flat))
    finite_count = finite.sum().to(torch.float32).clamp(min=1.0)
    return torch.stack(
        [
            isnan.sum().to(torch.float32),
            isinf.sum().to(torch.float32),
            safe.abs().max(),
            safe.min(),
            safe.max(),
            safe.sum() / finite_count,
        ]
    )


def _row_limited(tensor: Any) -> Any:
    rows = _env_int("DUMP_PROBE_ROWS", 0)
    if rows > 0 and tensor.dim() >= 1 and tensor.shape[0] > rows:
        return tensor[:rows]
    return tensor


def capture(stage: str, tensor: Any) -> None:
    """Record one intermediate tensor at ``stage``.

    Cheap when not armed. When armed, always records layout, records device
    statistics for stages matching ``DUMP_PROBE_SUMMARY``, and clones only for
    stages matching ``DUMP_PROBE_TENSOR``.

    Do not build the argument lazily at the call site -- Python evaluates
    arguments even when the probe is disabled. Write
    ``capture("q", q)``, not ``capture("q", q.index_select(0, idx))``.
    """
    probe = _PROBE
    if not probe.armed or not _is_tensor(tensor):
        return

    detached = tensor.detach()
    record = {"stage": stage, "order": len(probe.records)}
    record.update(_layout(detached))

    summary_pattern = _env("DUMP_PROBE_SUMMARY")
    if detached.numel() > 0 and (
        not summary_pattern or re.search(summary_pattern, stage)
    ):
        try:
            probe.stat_tensors.append(_device_stats(detached))
            record["summary_index"] = len(probe.stat_tensors) - 1
        except Exception as exc:  # a broken stat must not kill the run
            record["summary_error"] = str(exc)

    tensor_pattern = _env("DUMP_PROBE_TENSOR")
    if tensor_pattern and re.search(tensor_pattern, stage):
        probe.tensors.append((stage, _row_limited(detached).contiguous().clone()))
        record["tensor_saved"] = True

    probe.records.append(record)


def capture_inputs(stage: str, **values: Any) -> None:
    """Record a full named input set for one operator call.

    This is the entry point for single-operator localization: it always clones
    the tensors (row-limited by ``DUMP_PROBE_ROWS``) and keeps non-tensor
    arguments verbatim, so the call can be replayed standalone.
    """
    probe = _PROBE
    if not probe.armed:
        return

    payload: dict[str, Any] = {}
    record = {"stage": stage, "order": len(probe.records), "inputs": {}}
    for name, value in values.items():
        if _is_tensor(value):
            detached = value.detach()
            payload[name] = _row_limited(detached).contiguous().clone()
            record["inputs"][name] = _layout(detached)
        else:
            payload[name] = value
            record["inputs"][name] = {"value": repr(value)}

    probe.input_sets.append((stage, payload))
    record["input_set_saved"] = True
    probe.records.append(record)


def graph_slot(name: str, shape: tuple[int, ...], dtype: Any, device: Any = None) -> Any:
    """Allocate a persistent buffer for a graph-captured copy.

    Call this at module construction time, never inside forward. The returned
    buffer is written by ``capture_graph`` through a device-to-device copy that
    becomes part of the captured graph, and is read back by ``finish()`` after
    replay.
    """
    import torch

    if device is None:
        device = torch.npu.current_device() if hasattr(torch, "npu") else "cpu"
    buffer = torch.zeros(tuple(shape), dtype=dtype, device=device)
    _PROBE.graph_slots[name] = buffer
    _PROBE.graph_meta[name] = {"shape": list(shape), "dtype": str(dtype)}
    return buffer


def capture_graph(name: str, tensor: Any) -> None:
    """Copy ``tensor`` into a pre-allocated slot from inside the graph.

    Unlike ``capture``, this is *not* gated on arming: a graph copy node is
    baked in at capture time, so gating it at runtime would either be ignored
    or change the graph. The cost is one fixed device-to-device copy per
    replay. Remove the call once the bug is located.
    """
    slot = _PROBE.graph_slots.get(name)
    if slot is None or not _is_tensor(tensor):
        return
    source = tensor.detach()
    rows = min(slot.shape[0], source.shape[0]) if source.dim() >= 1 else 0
    if rows == 0:
        return
    try:
        view = source[:rows].reshape(rows, -1)
        target = slot[:rows].reshape(rows, -1)
        cols = min(view.shape[1], target.shape[1])
        target[:, :cols].copy_(view[:, :cols], non_blocking=True)
    except Exception:
        return


def _stream_capturing() -> bool:
    try:
        import torch

        npu = getattr(torch, "npu", None)
        if npu is not None and hasattr(npu, "is_current_stream_capturing"):
            return bool(npu.is_current_stream_capturing())
    except Exception:
        pass
    return False


def _synchronize() -> None:
    try:
        import torch

        npu = getattr(torch, "npu", None)
        if npu is not None and hasattr(npu, "synchronize"):
            npu.synchronize()
    except Exception:
        pass


def _safe_stem(label: str, rank: int) -> str:
    return f"{re.sub(r'[^A-Za-z0-9_.-]+', '_', label)}-rank{rank}"


def finish() -> str | None:
    """Read back one armed forward and write the manifest. Returns its path.

    This is the only place that synchronizes and copies to host. Call it after
    the values you dumped already have a consumer -- typically after logits
    exist -- and always outside graph capture.
    """
    probe = _PROBE
    if not probe.armed:
        return None
    if _stream_capturing():
        # Reading back mid-capture would change the graph being measured.
        return None

    import torch

    _synchronize()

    dump_dir = Path(_env("DUMP_PROBE_DIR") or ".")
    dump_dir.mkdir(parents=True, exist_ok=True)
    stem = _safe_stem(probe.label or "unlabeled", probe.rank)

    # Single device-to-host transfer for every statistic in this forward.
    stat_rows: list[list[float]] = []
    if probe.stat_tensors:
        stat_rows = torch.stack(probe.stat_tensors).cpu().tolist()
    for record in probe.records:
        index = record.pop("summary_index", None)
        if index is not None and index < len(stat_rows):
            record["summary"] = dict(zip(STAT_NAMES, stat_rows[index]))

    graph_payload = {
        name: buffer.detach().cpu() for name, buffer in probe.graph_slots.items()
    }

    manifest: dict[str, Any] = {
        "probe": PROBE_ID,
        "label": probe.label,
        "rank": probe.rank,
        "match_index": probe.match_index,
        "occurrence": probe.occurrence,
        "metadata": probe.metadata,
        "stats": list(STAT_NAMES),
        "records": probe.records,
        "graph_slots": {
            name: dict(meta) for name, meta in sorted(probe.graph_meta.items())
        },
    }

    has_tensors = bool(probe.tensors or probe.input_sets or graph_payload)
    if has_tensors:
        manifest["tensor_file"] = f"{stem}.pt"
        torch.save(
            {
                "probe": PROBE_ID,
                "label": probe.label,
                "rank": probe.rank,
                "metadata": probe.metadata,
                "tensors": [(stage, value.cpu()) for stage, value in probe.tensors],
                "inputs": [
                    (
                        stage,
                        {
                            name: (value.cpu() if _is_tensor(value) else value)
                            for name, value in payload.items()
                        },
                    )
                    for stage, payload in probe.input_sets
                ],
                "graph_slots": graph_payload,
            },
            dump_dir / f"{stem}.pt",
        )

    manifest_path = dump_dir / f"{stem}.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    if probe.label is not None:
        probe.dumped_labels.add(probe.label)
    probe.reset_capture()
    return str(manifest_path)
