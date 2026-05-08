# Acceptance Checks

A collection run is "good" when **all** of the following hold. The agent should verify these before reporting success.

## 1. Top-level status

```jsonc
{
  "status": "ok",
  "analysis_status": "ok",
  ...
}
```

`status == "failed"` or `analysis_status != "ok"` ⇒ collection is not usable. Re-collect.

## 2. Service did become ready

```jsonc
"service_result": { "status": "ready", "port": <int>, "runtime_dir": "..." }
```

If `service_result.status` is anything else (`failed`, `needs_input`, ...) the run did not actually capture anything; the manifest will already be `status=failed` but verify this is the cause before retrying.

## 3. Profile window opened and closed cleanly

```jsonc
"start_profile": { "ok": true, "status": 200, ... },
"stop_profile":  { "ok": true, "status": 200, ... }
```

A non-2xx status here means the profiler window was never actually open, even if other steps succeeded. The captured trace may still exist but is meaningless.

## 4. Workload actually executed during the window

```jsonc
"workload_status": {
  "status": "ok",
  "bench_total": <int>,
  "bench_ok":    <int>,
  "bench_success_rate": <float>,
  "bench_threshold":    <float>,
  "followup_ok": true
}
```

`workload_status.status != "ok"` is a top-level hard gate (orchestrator already sets `status=failed`). Possible non-ok values:

- `followup_failed` — the single tail request failed; the trace ends in an error path, not in steady-state decode
- `benchmark_below_threshold` — fewer than `--benchmark-success-threshold` of the benchmark wave succeeded; the model was not under load for most of the window
- `no_benchmark_requests` — defensive: the orchestrator was asked to run zero benchmark requests

Inspect `benchmark_results[*].error` / `body` to decide whether the request shape (`--prompt-tokens`, `--benchmark-output-tokens`, `--max-model-len`) was wrong for the model before re-collecting.

## 5. Every rank produced kernel_details.csv

```jsonc
"remote_profile_dirs": [
  {
    "path": "...rank.../ascend_pt",
    "outputs": {
      "kernel_details_csv": { "exists": true, ... },
      "trace_view_json":    { "exists": true, ... }
    },
    "analysis_status": "ok"
  },
  ...
]
```

Any per-rank `analysis_status` other than `ok` invalidates the whole root for downstream analysis. The orchestrator will already have set the top-level `analysis_status` accordingly.

## 6. Number of rank dirs matches expected topology

```jsonc
"expected_ranks": <tp * (dp or 1)>,
"rank_count":     <observed>,
"analysis_status": "ok"   // not "rank_count_mismatch"
```

The orchestrator passes `--expected-ranks = tp * (dp or 1)` to `run_remote_analyse.py` and the script sets `analysis_status = "rank_count_mismatch"` (and the top-level `status = failed`) when the count is off — even if every directory that *did* land was complete. Mismatch usually means one rank's profiler thread crashed or its `*_ascend_pt` directory landed somewhere unexpected. SSH into the container and inspect `<remote_profile_root>` directly before re-collecting.

## 7. Service was stopped cleanly

```jsonc
"stop_result": { "status": "stopped", ... }
```

If `stop_result.status` is `failed`, an orphan vLLM process may still be holding NPUs. Run `serve_stop.py --machine <alias> --force` and re-check `serve_probe_npus.py` before launching the next collection.

## When you need to re-collect (not re-analyse)

The following symptoms cannot be fixed offline:

- `analysis_status == "missing_kernel_details"` on any rank
- `analysis_status == "rank_count_mismatch"` (a rank failed to dump anything)
- `workload_status.status != "ok"` (no real model traffic during the window)
- `*_ascend_pt/PROF_*/device_*/data` is suspiciously small (kilobytes vs. expected MB)
- `FRAMEWORK/torch.op_range` missing
- profile window was shorter than the actual model warmup (rare but seen with first-call lazy compilation)

These all originate at capture time. `run_remote_analyse.py` cannot recover them.

## When re-analyse is enough

- The capture finished cleanly but `analyse()` was never run (rare; the orchestrator always runs it).
- An old root collected by the now-removed prototype script (under `.vaws-local/service-torch-profiler/`) needs to be re-verified under the new output contract.

In those cases, point `run_remote_analyse.py --profile-root` at the root and let it run. Verification will tell you whether the data was salvageable.
