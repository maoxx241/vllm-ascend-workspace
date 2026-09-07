# Audit: the measurement and analysis family

Scope: `.agents/skills/ascend-profiling-collection/`,
`.agents/skills/ascend-profiling-analysis/`,
`.agents/skills/ascend-memory-profiling/`.

Base: `origin/main` (`161fed1`). Audit only — no behaviour changed. All
`file:line` references are relative to the repository root at that commit.
Every path, host and identity in this document is redacted or generic.

## 0. Method and headline numbers

Read in full: the three `SKILL.md` files, every `references/` file, every
`scripts/*.py` entry point, the `ascend_profile/knowledge/` YAML/JSON
catalogues, plus targeted reads of `segment.py`, `model_context.py`,
`model_insights.py`, `summarize.py`, `classify.py`, `rules.py`,
`diagnostics.py`, `analysis_summary.py`, `mem_collect.py`,
`mem_analyze.py`, `weight_inspector.py`. Git history was read for the 52
commits that produced the current tree (`161fed1` is their squash), because
the per-model churn is only visible at that granularity.

Measured surface:

| Metric | Value |
|---|---|
| Files with an `argparse.ArgumentParser` | 19 |
| Additional runnable `main()` without argparse | 3 (`selftest_parallel_analyse.py`, `selftest_knowledge_hooks.py`, `ascend_profile/html_report.py:3156`) |
| Runnable entry points, total | 22 |
| Python lines, non-test | 30,732 |
| Python lines, tests | 9,790 |
| Python lines, total | 40,522 |
| Knowledge data lines (YAML/JSON under `ascend_profile/knowledge/`) | ~4,400 |

Per skill (non-test / test Python): collection 3,019 / 0 (its two selftests
live in `scripts/`), analysis 25,246 / 9,620, memory 2,467 / 170.

The brief's "21 entry points" is one short of the count above; the
difference is `ascend_profile/html_report.py`, whose `main()` at
`html_report.py:3156-3167` parses `sys.argv` by hand and is therefore easy
to miss with an argparse grep. It is a real entry point: it is the only way
to re-render the legacy single-file HTML without running the `report` stage.

Judgment vs mechanics, by my reading (reasoning in §4.6): roughly **6,700
non-test Python lines encode decisions an agent should be making from the
data** (≈22% of non-test Python), plus ≈4,300 test lines that pin those
decisions — about **11,000 of the 40,500 lines (≈27%)**. The remaining
≈29,500 lines are genuine mechanics: db/CSV parsing, interval arithmetic,
per-rank orchestration, artifact transport, and report rendering.

---

## 1. Capability inventory

"Called by" is from reading the code, not grep. Two dependencies are
invisible to grep and are marked **(hidden)**.

### 1.1 `ascend-profiling-collection` (5 entry points, 3,019 lines)

| # | File | Verbs / real parameters | What it does | Called by |
|---|---|---|---|---|
| 1 | `scripts/collect_torch_profile_case.py` (1,099) | one implicit verb; 37 flags declared at `:657-796`. Required: `--model --served-model-name --tp --tag --mode{enforce_eager,full_decode_only,piecewise_graph} --request-kind{text,vl} --benchmark-output-tokens` (`:663-683`). Optional: `--dp --enable-expert-parallel --speculative-tokens/--speculative-method --gpu-memory-utilization --max-model-len --max-num-seqs --max-num-batched-tokens --api-server-count --prompt-tokens --followup-output-tokens --benchmark-total-requests --benchmark-concurrency --benchmark-success-threshold --request-timeout --health-timeout --profile-control-timeout --torch-profiler-dir --torch-profiler-with-stack --analyse-export{db,text,both} --archive-dir --image-path --image-height --skip-parity --session-id/--session-file` | Whole-case orchestrator: knowledge preflight (`:894`), `serve_start.py` (`:906` via `_common.call_serve_start:253`), local `ssh -L` tunnel (`:916`), `/start_profile` (`:921`), benchmark wave + one follow-up (`:930-958`), `/stop_profile` (`:966`), 5 s flush (`:973`), `serve_stop.py` (`:976`), parallel `analyse()` + verify (`:988`), optional shared-storage archive (`:1020`), manifest write (`:1043`) | Agent (SKILL.md:56); its knowledge hooks are imported by `selftest_knowledge_hooks.py:34` **(hidden)**; its archive builder is exercised by `selftest_parallel_analyse.py` **(hidden)** |
| 2 | `scripts/profile_control.py` (194) | `--action{start_profile,stop_profile}` `--timeout` `--session-id/--session-file` | POSTs the profiler-window endpoint from *inside* the container (`:51-99`); reads the port out of the session's `serving.json` (`:136-161`) | Agent; imported by the orchestrator (`collect_torch_profile_case.py:69`) |
| 3 | `scripts/run_remote_analyse.py` (727) | `--profile-root` (required) `--expected-ranks` `--analyse-timeout` `--analyse-parallelism` `--analyse-export{db,text,both}` `--session-*` | Discovers `*_ascend_pt` dirs (`:145`), builds one bash driver that fans `analyse()` across ranks with `xargs -P` under `timeout(1)` (`:204-280`), parses the per-rank rc table (`:283-307`), verifies per-rank outputs (`:401-436`), maps presence to `analysis_status` (`:472-494`), applies worst-of priority incl. `rank_count_mismatch` (`:585-604`) | Agent; called in-process by the orchestrator (`collect_torch_profile_case.py:988`) |
| 4 | `scripts/selftest_parallel_analyse.py` (514) | no flags | Runs the generated parallel-analyse bash locally against fake rank trees; covers command shape, export modes, verification branches, rc aggregation, `timeout` behaviour, archive layout | Developer / CI by hand |
| 5 | `scripts/selftest_knowledge_hooks.py` (214) | no flags | Exercises the two knowledge hooks against a synthetic knowledge dir | Developer / CI by hand |

### 1.2 `ascend-profiling-analysis` (14 entry points, 25,246 non-test lines)

Wrappers (run locally, orchestrate the remote):

| # | File | Verbs / real parameters | What it does | Called by |
|---|---|---|---|---|
| 6 | `scripts/profile_analyze.py` (1,160) | 29 flags at `:41-186`: `--manifest` XOR `--remote-profile-root` (required group), `--tag --session-* --remote-work-dir --remote-output-dir --local-output-dir --overwrite --keep-remote-output`, mutually exclusive `--no-pull` / `--archive-output`, `--remote-timeout --skip-html --report-mode --mode{fast,full} --model-id --model-config --hardware-model --hardware-profile --no-cann-hardware-scan --from-stage --to-stage --only-stage --verbose` | Session resolve, remote-python preflight (`:865`), tar-sync of `ascend_profile/` (`:895`), streamed `python3 -m ascend_profile.analyze` (`:939-960`), stage-scoped artifact validation (`:667-709`), segment-health gate (`:711-765`), optional remote archive (`:1000`), mode-scoped pull (`:1009`), knowledge enrichment of the local summary (`:1052`), one stdout JSON (`:1127-1155`) | Agent (SKILL.md:55) |
| 7 | `scripts/profile_sweep.py` (358) | `--search-root` (repeatable, required) `--tag --limit --jobs --reuse-existing --render-html --report-mode --pull-html --remote-work-dir --remote-timeout(14400) --local-output-dir --overwrite --keep-remote-output --verbose --session-*` | Same transport, but drives `ascend_profile.sweep` over many roots and rolls the per-root summaries into `sweep_summary.json` + a layer inventory (`:153-176`) | Agent |

Framework stages (run **on the container** as
`python3 -m ascend_profile.<stage>`; each is separately runnable for
stage-level iteration):

| # | File | Verbs / real parameters | What it does | Called by |
|---|---|---|---|---|
| 8 | `ascend_profile/analyze.py` (367) | `profile_root` `--output` `--verbose --skip-html --report-mode --skip-xlsx --skip-host-trace --model-id --model-config --hardware-model --hardware-profile --no-cann-hardware-scan --from-stage --to-stage --only-stage` | The pipeline driver: 7-stage registry + resume markers (`:51-68`), stage-window resolution (`:71-87`), in-process event hand-off (`:136-160`), aggregate `manifest.json` | `profile_analyze.py:939`; `ascend_profile/sweep.py` |
| 9 | `ascend_profile/normalize.py` (527) | `profile_root` `--output --hash-sources --write-jsonl --source{auto,db,csv}` | Rank discovery + 46-column event stream from the profiler db or `kernel_details.csv`; per-rank source decision and probe notes (`:81-138`) | `analyze.py:162` |
| 10 | `ascend_profile/segment.py` (4,039) | `--output --model-id --model-config` | Layer/step segmentation: anchor candidates, layer observations, frames, model-guided plans, exact-cover fallback, layer-count invariant, exact-cover validation (`segment_profile:3884`) | `analyze.py:164` |
| 11 | `ascend_profile/classify.py` (596) | `--output` | Layer → block decomposition (`:145-227`) and shape-strict class signatures | `analyze.py:174` |
| 12 | `ascend_profile/summarize.py` (1,882) | `--output --model-id --model-config --hardware-model --hardware-profile --no-cann-hardware-scan --write-raw-index --skip-host-trace` | Step/layer/block/operator/HCCL summaries, anomaly tags, bubble windows, model fingerprint, hardware peaks | `analyze.py:182` |
| 13 | `ascend_profile/cross_rank.py` (312) | `--output` | Cross-rank alignment rows | `analyze.py:195` |
| 14 | `ascend_profile/diagnostics.py` (457) | `--output` | Turns summary/alignment rows into findings using `knowledge/diagnosis_rules.yaml` | `analyze.py:196` |
| 15 | `ascend_profile/report.py` (1,530) | `--output --skip-html --skip-xlsx --report-mode --html-renderer{v2,legacy} --html-single-file` | `report.md`, `report.xlsx`, HTML, `analysis_summary.json`, evidence-chain validation | `analyze.py:199` |
| 16 | `ascend_profile/sweep.py` (522) | `--search-root --output --limit --jobs --skip-html/--no-skip-html --skip-xlsx/--no-skip-xlsx --skip-host-trace/--no-skip-host-trace --reuse-existing` (`:410-475`) | Root discovery + per-root `analyze_profile` in a thread pool + cross-root rollup | `profile_sweep.py` |
| 17 | `ascend_profile/html_report_v2/__init__.py` (238, `+__main__.py`) | `analysis_root` `output_html` `--single-file --single-file-max-mb` (`:217-224`) | Thin-shell renderer + gzipped lazy assets | `report.py`; standalone re-render |
| 18 | `ascend_profile/html_report.py` (3,168) | positional `<root> <output.html>`, **no argparse** (`:3156-3167`) | Legacy single-file HTML SPA | `report.py --html-renderer legacy`; standalone re-render |
| 19 | `scripts/dev/golden_db_vs_csv.py` (320) | `--db --csv --tolerance --out` | Row-by-row, field-by-field equivalence of the db adapter against `kernel_details.csv` from the same capture | Developer, on the container |

### 1.3 `ascend-memory-profiling` (3 entry points, 2,467 non-test lines)

| # | File | Verbs / real parameters | What it does | Called by |
|---|---|---|---|---|
| 20 | `scripts/mem_collect.py` (925) | 28 flags at `:96-128`: `--attach`, `--resume-run`, `--baseline-from`, `--session-*`, `--model --tp --dp --devices --port --gpu-memory-utilization --max-model-len --enable-expert-parallel --enforce-eager --max-tokens --prompt --image-url --tag --health-timeout --msprof-mem-freq --speculative-config --compilation-config --additional-config --quantization --extra-serve-args` | Two modes. Attach: read `serving.json`, npu-smi snapshots, vLLM log scrape, weight manifest, one inference, msprof CSV export, manifest. Standalone: additionally builds its own `vllm serve` command (`:163-189`), wraps it in `msprof --application` (`:200-231`), health-waits (`:234`), and kills processes with `pkill -f 'vllm.entrypoints'` (`:303-312`) | Agent (SKILL.md:113) |
| 21 | `scripts/mem_analyze.py` (973) | positional `run_dir`, `--format{json,text}` (`:847`) | Parses npu-smi / vLLM log / msprof CSV / weight manifest, computes the component breakdown and cross-validation, writes `report.json` + `report.txt` | Agent (SKILL.md:157) |
| 22 | `scripts/weight_inspector.py` (274) | positional `model_dir` (`:219-221`) | Reads safetensors headers, classifies every tensor into a component and a shard strategy | **(hidden)** `mem_collect.py:423-427` copies this file's *source text* to `/tmp/_vaws_weight_inspector.py` on the container and runs it there; nothing invokes it by name |

Plus the two non-CLI helpers the SKILL.md asks the agent to call **inline**:
`_common.check_msprof_available` and `_common.upload_msprof_wrapper`, via
hand-written `python3 -c` snippets (`ascend-memory-profiling/SKILL.md:70-107`).
They are entry points in practice; they just do not have a CLI.

---

## 2. Classification per capability

Legend: **M** closed-world mechanics · **J** open-world judgment ·
**X** mixed (seam named) · **R** redundant.

| # | Capability | Class | Evidence and reasoning |
|---|---|---|---|
| 1 | Collection orchestration | **X** | Mechanics: the whole call chain is deterministic and correctly pinned — window bracketing, flush window (`collect_torch_profile_case.py:970-973`), hard gates (`:1005-1010`), stop-on-failure (`:1069-1088`). Judgment scripted into it: the workload *shape* defaults (`:703-728`) and the 0.8 success bar (`:722`) decide what the trace will contain. Seam in §3.1. |
| 2 | Profiler window control | **M** | Two verbs, one port lookup, one POST. Nothing to decide. Correctly owned here rather than in serving (`references/behavior.md:9-11`). |
| 3 | `analyse()` + per-rank verification | **M** | The single most mature thing in the family: one shared bash driver (`run_remote_analyse.py:204-280`), a per-rank rc table, export-mode-specific verification (`:401-436`), a stable four-value `analysis_status` enum (`:472-494`), worst-of ordering that prefers the more actionable signal (`:585-604`). |
| 4–5 | Two selftests | **M** | These are the maturation asset for #3: they execute the *real* generated bash locally. Keep; they belong under a `selftest` verb, not as top-level scripts. |
| 6 | Single-root analysis wrapper | **X** | Mechanics: transport, stage-scoped artifact validation (`profile_analyze.py:667-709`), bounded ssh retry with a recorded cause (`:635-664`), mode-scoped pull lists (`_common.py:137-207`). Judgment: nothing — except that it *hides* one, see #10. |
| 7 | Multi-root sweep wrapper | **X**/**R** | The transport and rollup are mechanics. The flag set is a near-duplicate of #6 (`--report-mode`, `--render-html`, `--keep-remote-output`, `--jobs`) and the "which roots are comparable" question — the actual sweep judgment — is left entirely to `--search-root` globbing. |
| 8 | Pipeline driver | **M** | Stage registry + resume markers + window validation. Boring in the good sense. |
| 9 | Normalize (db/CSV → 46-column stream) | **M** | Closed-world by construction, and *proved* closed-world by #19. `--source auto` with a probe and a recorded fallback note (`normalize.py:81-138`) is the right shape. |
| 10 | Segment | **X** | The family's central seam. Mechanics: exact-cover validation, row-lossless coverage, the layer-count *invariant* as a trust marker (`segment.py:1947-2007`), the anchor-degradation diagnostic (`:3650-3666`). Judgment scripted: the model-guided path *decides* the layer count and then reshapes the data to match it (`:2009-2044`, `:2105-2153`). Seam in §3.1. |
| 11 | Classify (layer → blocks) | **X** | Mechanics: role-driven anchoring, shape-strict class signatures. Judgment: the split point is the arithmetic midpoint between the last attention row and the first MoE row (`classify.py:219`), and an "AICPU layer" is one where ≥ half the events are AICPU (`:187-189`). Those are readable defaults, not derived facts. |
| 12 | Summarize | **X** | Mechanics: interval union, per-step anatomy, HCCL/operator rollups, pipeline-field sums. Judgment: nine thresholds hardcoded as module constants (`summarize.py:100-126`) with their provenance in a comment rather than in `knowledge/`. |
| 13 | Cross-rank alignment | **M** | Aligning ranks by role/time window is deterministic correlation. This is exactly the mechanic an agent must never be asked to do by hand. |
| 14 | Diagnostics | **X**, well-cut | The seam is already in the right place: thresholds and wording in `knowledge/diagnosis_rules.yaml:27-38`, trigger conditions in Python, and the file says so (`diagnosis_rules.yaml:5-9`). This is the model the rest of the family should copy. |
| 15 | Report (md/xlsx/summary) | **M** | A report format that is stable across models is a feature. `analysis_summary.json` (`analysis_summary.py:1-26`) is the correct agent-facing contract: re-derived from artifacts, never re-concluded, `null` + `limitations` when inputs are missing. |
| 16 | Sweep engine | **M** | Discovery + thread pool + rollup. |
| 17 | HTML v2 renderer | **M** | Presentation. |
| 18 | Legacy HTML renderer | **R** | 3,168 lines kept behind `--html-renderer legacy` (`report.py:1485-1493`), with its own argv main and its own UI-only heuristics (`references/deferred-work.md:162-173`). Superseded by #17 by the project's own statement (`deferred-work.md:11-16`). |
| 19 | db-vs-CSV golden check | **M** | The single best maturation artifact in the family: it converts "trust the adapter" into a measured, per-field verdict with documented exceptions (`golden_db_vs_csv.py:9-30`). |
| 20 | Memory collection | **X**, badly cut | Mechanics: npu-smi snapshots, msprof wrapping, CSV export, two-phase resume. Judgment: none. But the *seam* is wrong twice — the agent hand-assembles the msprof preflight and wrapper upload (`SKILL.md:70-107`) and hand-sequences five steps (`SKILL.md:113-155`), while the script simultaneously owns a full duplicate service lifecycle (`mem_collect.py:163-231`, `:303-312`) that duplicates `vllm-ascend-serving`. |
| 21 | Memory analysis | **X** | Mechanics: log/CSV parsing, per-device roll-up, residual reporting with an explicit "no estimation" policy (`mem_analyze.py:702-714`). Judgment scripted: a full per-architecture parameter-count formula (`:396-500`) and a component display/order table (`:178-220`). |
| 22 | Weight inspector | **X** | Mechanics: safetensors header parsing is byte-exact. Judgment: `classify_tensor` (`:63-141`) and `classify_shard_strategy` (`:150-214`) are ~130 lines of per-architecture name patterns that restate what a vLLM model implementation does. SKILL.md is honest about it (`SKILL.md:243`), which is why this is a seam and not a lie. |

### 2.1 Named seams (the main product)

1. **Collection: window mechanics vs workload design.** Everything from
   `/start_profile` to per-rank verification is closed-world and should be
   one command with no room for parameter drift. What the workload *is*
   (`--prompt-tokens`, `--benchmark-output-tokens`, concurrency, wave size,
   the success bar) is a decision about what question the trace answers.
   Today they are the same argv, and the decision half carries silent
   defaults (`collect_torch_profile_case.py:703-728`).

2. **Segment: evidence extraction vs structural conclusion.** Extracting
   anchor candidates, building layer observations, validating an exact cover
   and *reporting* a layer-count mismatch are mechanics. Choosing
   "this capture is 43 layers" and coalescing frames until they are is the
   conclusion (`segment.py:2105-2153`). The seam is exactly the boundary
   between `layer_anchor_candidates` + `layer_count_check` (keep) and
   `model_guided_target_layer_count` + `coalesce_frame_to_model_layers`
   (should be an agent-visible hypothesis, not an in-pipeline rewrite).

3. **Model identity: catalogue lookup vs family inference.** Fetching a
   `config.json` for an exact repo id is mechanics and is already gated
   correctly (`model_context.py:491-524`, `:570-595`). Scoring observed
   operator categories against weighted per-family rules to *pick* a model
   (`:375-427`) is inference, and it is where per-model churn lands.

4. **Diagnosis: threshold data vs trigger logic.** Already cut correctly in
   `diagnostics.py` + `diagnosis_rules.yaml`. `summarize.py:100-126` is the
   same kind of content on the wrong side of the seam.

5. **Memory: measurement vs attribution model.** npu-smi/msprof/log numbers
   are measurements. "Which component does this byte belong to, and how is
   this tensor sharded" is a model of the runtime
   (`weight_inspector.py:150-214`, `mem_analyze.py:396-500`). The seam is
   the manifest: keep it as measured bytes + names, and let attribution be
   a report-time overlay the agent can override.

---

## 3. The two opposite errors

### 3.1 Scripted decisions (the agent should be deciding)

1. **A per-model layer-count table that the pipeline then enforces.**
   `knowledge/model_fingerprints.json` carries 21 entries; 13 of them state
   an `expected_layers` (DSV4-Pro 61 `:9`, DSV4-Flash 43 `:28`, DSV3.1 61
   `:55`, Qwen3-8B 36 `:73`, Qwen3-VL 48 `:90`, Qwen3.5-35B 40 `:109`,
   Qwen3.5-397B 60 `:149`, 122B 48 `:177`, 9B/4B 32 `:190`/`:203`, 0.8B 24
   `:216`, DSV2-Lite 27 `:257`, GLM-5 78 `:359`, Kimi-K3 93 `:380`).
   `segment.py:2009-2044` picks the nearest target inside a tolerance and
   `segment.py:2105-2153` then *slices the observed frame by index
   arithmetic* until it has exactly that many "logical layers". When the
   catalogue is right this looks like a fix; when it is stale the pipeline
   silently produces a structure the data does not show.

2. **Tolerances and multipliers standing in for reading the data.**
   `complete_layer_tolerance` defaults to 0.15 (`segment.py:1884-1890`) and
   `max_anchor_multiplier` is clamped to 1..8 (`:1893-1899`). A ±15%
   window on layer count is a large licence, and per-model overrides of it
   already exist in the catalogue (0.15 for DSV4-Flash
   `model_fingerprints.json:39`, 0.08 for Qwen3.5-397B `:161`).

3. **Weighted operator-fingerprint scoring as model identification.**
   `model_context.py:375-407` sums per-category weights and a `base_score`
   per family; the catalogue then tunes those numbers to steer outcomes —
   `base_score: 0` for GLM-5 "keeps the generic DeepSeek DSA family as the
   operator-match winner" (`model_fingerprints.json:371-373`) and the same
   for Kimi-K3 (`:393`). Steering a scorer with a constant is a decision
   procedure wearing data's clothes.

4. **A negative per-model rule: "family X must not absorb model Y".**
   The Qwen3.5 family entry forbids `attention.mla`
   (`model_fingerprints.json:233`) purely so that Kimi-K3 captures stop
   resolving to Qwen3.5; the reason is recorded as a counterexample
   (`knowledge/known_counterexamples.md:43`) and the commit is
   `c2c993d "Qwen3.5 family must not absorb K3"`. The underlying fact —
   "the FIA kernel is mode-neutral; use latent-rank shape and companion
   evidence" — is written down in prose in the very same catalogue entry
   (`:397`) but is *not* what the code uses.

5. **Free-text structure words mapped to features by a lookup table.**
   `model_context.py:325-346` maps 21 substrings (`csa`, `hca`,
   `compressor`, `lightningindexer`, `gdn`, `mamba`, …) onto catalogue
   features, and `:276-291` declares which feature matches count as
   "specific". This is the agent's reading-comprehension step, scripted.

6. **Org-prefix guessing when a bare model name is supplied.**
   `model_context.py:514-522` tries `deepseek-ai/<name>`, `Qwen/<name>`,
   `zai-org/<name>`, `THUDM/<name>` based on substrings of the name. The
   exact-basename gate on HF search (`:588-593`) shows the authors knew the
   risk; the prefix table is the same guess with fewer guards.

7. **Nine anomaly thresholds as Python constants.**
   `summarize.py:100-126`: `UNDERFEED_HEAVY_RATIO 0.30`,
   `INTERNAL_BUBBLE_MIN_MS 1.0` / `WALL_RATIO 0.10`, `EDGE_GAP_*`,
   `RECURRING_BUBBLE_MIN_RATIO 0.60` / `MIN_STEPS 3`,
   `PARTIAL_CAPTURE_MIN_STEP_FRACTION 0.5`, `WAIT_ANCHOR_RATIO 0.80`,
   `FALSE_HOTSPOT_* 0.95 / 10 us / top-10`, `AICPU_MASKED_RATIO 0.90`.
   Their provenance (a retired skill's rulebook) is a comment
   (`:103-108`, `known_counterexamples.md:3-12`). "Is a 0.9 ms gap on a
   9 ms step heavy?" is a judgment about the capture, not a constant.

8. **Attention-shape sanity constants.** `rules.py:371-378`
   (`_VALID_HEAD_DIMS`, `_MAX_NUM_HEADS 1024`) and `:436`
   (`_PAGED_K_BATCH_RATIO_GUARD 8`) decide which shape parses are
   plausible. Every new head-dim in a future model is a code edit.

9. **Block split by midpoint arithmetic.** `classify.py:219` splits
   attention from MoE at `(last_attn + first_moe) // 2`. On interleaved
   ranges it falls back to `first_moe - 1` (`:221`). Reasonable defaults —
   but the report then presents `block_kind` shares as fact.

10. **A per-architecture parameter formula in the memory skill.**
    `mem_analyze.py:396-500` reimplements weight accounting: FFN as
    `3 * hidden * intermediate` (`:459`), norms as `4 * hidden` (`:448`),
    hybrid linear-attention parameters from `linear_key_head_dim` /
    `linear_num_key_heads` (`:424-433`), a bespoke MTP formula (`:380-393`),
    and a separate per-device branch for MoE (`:473-484`). Every new
    architecture that names its config fields differently is a code edit.

11. **Tensor component + shard-strategy tables.**
    `weight_inspector.py:63-141` and `:150-214` decide component and shard
    strategy from name substrings — `mtp`, `linear_attn`, `visual`,
    `q_a_proj`, `w1`/`w3` vs `w2`, and so on. The authoritative answer lives
    in the model's `load_weights` in the vLLM/vllm-ascend submodule; this is
    a parallel implementation that must be kept in sync by hand.

12. **UI-only heuristics inside the renderer.** `compute_ep_balance`,
    `assess_companion_run`, `detect_attention_subtype`,
    `derive_layer_composition`, `guess_model_structure` are rendered with a
    "UI-only" pill precisely because they are guesses
    (`references/deferred-work.md:162-173`). Labelling them was the right
    call; keeping ~400 lines of them in a 3,168-line legacy renderer is not.

### 3.2 Mechanics left to the agent (should be pinned)

1. **The memory skill asks the agent to write Python inline.**
   `ascend-memory-profiling/SKILL.md:70-107` instructs the agent to run two
   hand-written `python3 -c` snippets that `sys.path.insert` into the
   skill's `scripts/` and call `check_msprof_available` /
   `upload_msprof_wrapper(ep, mem_freq=50)`. That is a deterministic
   preflight and a deterministic file upload — exactly the shape that
   should be one flag on one command (`mem_collect ... --preflight` or
   simply "collect does this for you"). Every character of those snippets
   is a parameter an agent can get wrong, and nothing validates them.

2. **A five-step hand-sequenced memory workflow with a stateful resume.**
   `SKILL.md:110-155`: baseline npu-smi *before* start → `serve_start
   --wrap-script <the path printed by step 1>` → `mem_collect --attach` →
   `serve_stop` → `mem_collect --attach --resume-run <run-dir from step 2>`
   → `mem_analyze <run-dir>`. Ordering, the wrapper path, and the run-dir
   handoff are all mechanics; if the agent forgets the second
   `--attach --resume-run`, the run is silently missing every msprof
   component and the report happily prints "未归因 (缺少 msprof 数据)"
   (`mem_analyze.py:702-714`). This should be one collect verb.

3. **Baseline provenance is the agent's problem.** `--baseline-from`
   accepts a previous run dir *or* a raw npu-smi text file
   (`SKILL.md:164-172`); omitting it silently reports fixed overhead as 0
   (`mem_analyze.py:542-548`). "Where did the baseline come from and is it
   the same machine" is mechanics that the manifest should record and the
   command should enforce.

4. **`--expected-ranks` is optional on a rank-completeness check.**
   `run_remote_analyse.py:632-640` documents that omitting it makes a
   partial capture look clean, and the docstring tells the agent to
   "always pass" it (`:57-60`). A check whose soundness depends on the
   caller remembering a flag is not pinned. The orchestrator passes it
   (`collect_torch_profile_case.py:988`); a direct re-analyse by an agent
   very plausibly does not.

5. **Twelve framework stages are directly runnable with bare `--output`.**
   `segment.py:4019-4024`, `classify.py:582-586`, `summarize.py:1836-1863`,
   `cross_rank.py:298-302`, `diagnostics.py:443-447`, `report.py:1461-1506`
   each take `--output <dir>` and assume the previous stage's artifacts are
   there. `analyze.py:119-132` implements the prerequisite check; the
   individual stages do not. The wrapper's SKILL.md forbids hand-running
   them (`ascend-profiling-analysis/SKILL.md:39`) — which is the tell: the
   surface exists and the docs have to talk the agent out of using it.

6. **The legacy HTML renderer has no argument parser at all.**
   `html_report.py:3156-3162` reads `sys.argv[1]`/`[2]` and prints a usage
   line to stderr. No `--help`, no validation, no JSON contract.

7. **Sweep comparability is unpinned.** `profile_sweep.py:69-73` takes raw
   `--search-root` paths; nothing checks that the roots are comparable
   (same hardware, same export mode, same TP shape). The rollup then puts
   them in one table (`sweep_class_rollup.csv`), which is exactly the kind
   of correlation an agent should not be eyeballing by hand — and exactly
   the kind of precondition a command should assert.

8. **Two parallel implementations of output verification.**
   `run_remote_analyse.verify_outputs` (shell, `:401-436`) and
   `verify_outputs_local` (pathlib, `:439-469`) are kept in sync by a
   selftest and by a comment (`references/deferred-work.md:249-251`). One
   of them should be the mechanic.

---

## 4. Model-specific hardening: the real maintenance cost

### 4.1 The history is the evidence

The 52 commits behind `161fed1` contain, in ~5 days:

| Commit | What it changed |
|---|---|
| `8c7f5cc` | register verified fingerprints for 4 models (36/48/40/61 layers) |
| `0ea03e0`, `87eea9d` | register GLM-5 as a DSA-structure fingerprint, then restore file formatting around it |
| `cb1cffc` | register GLM-5 (78L DSA) **and** Kimi-K3 (93L period-4 hybrid) structures |
| `dcea14a` | classify `SparseFlashMla` / `SparseFlashAttention` as `sparse_sharedkv` |
| `b90c994` | emit plain `attention.mla` from MLA KV-pipe kernels |
| `c2c993d` | cap the layer-count retry storm; **"Qwen3.5 family must not absorb K3"** |
| `a9f0c6d` | align K3 fingerprint features with the profile vocabulary |
| `4342b1b` | add KDA/GDN sub-families, attention residual, MHC categories |
| `c32846a` | hybrid layer-start **union** anchor for linear+MLA models |
| `c2f2439` | pre-register the SWA kernel `sas` as `attention.swa` |
| `3e67334` | **revert**: "DSV4 SWA is the `sparse_sharedkv` kernel family, not a separate `sas`" |
| `7415d86` | KMP minimal period + split budget breaker in segment |
| `59ed1c3` | record the K3 counterexample: boundary rotation fixed, tail merge still open |

That is a register → mis-register → revert cycle on a *single kernel name*
(`c2f2439` → `3e67334`), a negative family rule invented to keep two models
apart (`c2c993d`), and a performance breaker added because a per-model
mismatch caused an 8× resegmentation storm (`known_counterexamples.md:43`:
2380 s → ~300 s). Both are textbook symptoms of judgment that was scripted.

### 4.2 Every place a new model requires a code or data change

| # | Location | What must change | Inherent? | My judgement |
|---|---|---|---|---|
| H1 | `knowledge/model_fingerprints.json` — a new entry with `expected_layers`, `features`, `field_hints` | one catalogue entry per model | **Partly inherent** as *provenance* | Recording "this checkpoint's config says 93 layers, verified on date D" is legitimate evidence. Making the pipeline *enforce* it (§3.1.1) is not. Keep the entry, drop the enforcement. |
| H2 | `model_fingerprints.json:operator_match` weights + `base_score` per family (`:130-139`, `:230-241`, `:361-373`, `:382-394`) | tune scores so the new model wins its own profiles and loses others' | **Not inherent** | Derivable. The discriminators are stated in prose in the same file (compressor present → CSA; indexer+sparse, no compressor → DSA; linear+MLA coexisting → K3). An agent given the observed category set and those written rules resolves this without weights. The weights exist to make a scorer produce a pre-decided ranking. |
| H3 | `model_fingerprints.json` `forbidden:` lists (`:233`, `:304`, `:324`, `:344`, `:363`, `:384`) | add the new model's markers to sibling families' `forbidden` | **Not inherent** | This is O(families²) maintenance: every new family may need edits in every existing one. The K3-vs-Qwen3.5 case (`c2c993d`) is exactly this. Replace with agent-side elimination over the observed category set. |
| H4 | `knowledge/segmentation_rules.yaml:layer_start_categories` (`:31-42`) | register the new architecture's layer-start marker | **Inherent** | This is a real, stable, hardware-level fact ("the RecurrentKda kernel opens a layer"). It is data, it is reviewable, and the hybrid-union rule (`:44-51`, `segment.py:340-347`) generalises it. Keep. |
| H5 | `knowledge/segmentation_rules.yaml:companion_only_categories` / `companion_prefixes` / `fused_norm_exclusions` (`:52-93`) | add companions and fused-norm name tokens | **Inherent** | Same as H4. `fused_norm_exclusions` matching on folded name substrings (`:90-93`) is slightly worse than a category, but the YAML explains why no category separates fused from pure norms (`:83-89`). Acceptable. |
| H6 | `knowledge/kernel_signatures.yaml:match_rules` | register the new kernel spelling / alias | **Inherent, and correctly placed** | Kernel name → neutral category is a closed-world naming fact, YAML-driven since `5a129cd`, with `evidence: path:line` per entry and an 11,909-case differential corpus. This is what "matured until boring" looks like. The `sas` flip-flop (`c2f2439`/`3e67334`) was a *semantic* error (a new family invented for an existing kernel), not a mechanical one — and the fix landed as an alias plus a note (`kernel_signatures.yaml:548-572`). |
| H7 | `knowledge/attention_families.yaml` resolver + `semantic_conventions.yaml` enum | add a family value and a resolver branch together | **Not inherent** | The family label is a *conclusion* from the category set. Note the project already learned this: `f408ef1` removed the `fa` family because the resolver stopped emitting it, and left a note that a future generic-FA backend would need "both an enum value and a resolver branch added together". That coupling is the cost of pinning a conclusion. |
| H8 | `knowledge/model_architectures.yaml` candidate family lists | map a new architecture class name to candidate families | **Not inherent** | `3903cce` had to change `GlmMoeDsaForCausalLM` from `[mla, sfa]` to `[mla, dsa]` — i.e. a rename of a label with no observable consequence. Pure maintenance. |
| H9 | `segment_hints.profile_visible_layer_counts` + `complete_layer_tolerance` per model (`:37-46`, `:160-168`, `:264-272`) | measure and register how many layers a profile of this model actually shows, per mode | **Not inherent — and unbounded** | `knowledge/model_knowledge_todo.md:6-19` states the real cost: these must be collected *per model × per mode (prefill/decode/graph/eager) × per parallel layout*. That is a combinatorial table that will never be complete. The observed layer count is measurable from the capture itself; what the agent needs is the mismatch reported (which `layer_count_check` already does), not a table to match against. |
| H10 | `segment.py` model-guided helpers (`:1839-1899`, `:2009-2044`, `:2105-2153`, `:2156-2247`) | occasionally extend when a model's structure breaks the assumptions | **Not inherent** | ~400 lines whose only job is to apply H1/H9. Removing the enforcement removes the maintenance. |
| H11 | `segment.py` per-model comments and special cases (`:177`, `:340-347`, `:880`, `:1224`, `:2254-2255`, `:2697`, `:2715`, `:2767`, `:2905`, `:3012`, `:3364`, `:3587`) | 12 sites reason explicitly about K3 / DSV4 / GLM / dsv2-lite / Qwen3.5 behaviour | **Not inherent** | Even where the code is generic, the *reason* it is shaped that way is a specific capture. These are the fossils of scripted judgment; the counterexample file is the right home for them and `known_counterexamples.md` already holds two in full. |
| H12 | `segment.py:3583-3598` retry cap + `:1758-1763` split budget | tuned because specific models blew up the search | **Inherent as a breaker, not as a rule** | A time budget with a loud degraded marker is good engineering (it is announced on stderr `:1781-1788` and tagged on the frames `:1797`). The `<= max(2, 5%)` small-delta skip (`:3595`) is a judgment: "an off-by-one is a boundary artefact, not a wrong anchor". True for K3; asserted for everything. |
| H13 | `rules.py:371-378` `_VALID_HEAD_DIMS`, `_MAX_NUM_HEADS` | add a head-dim when a model uses a new one | **Not inherent** | Derivable from the shape distribution in the capture. |
| H14 | `weight_inspector.py:63-141`, `:150-214` | add the new architecture's tensor-name patterns and shard strategies | **Not inherent** | The truth is in the model's `load_weights`. A new hybrid model needs edits in both functions (as `linear_attn` did, `:91-99` and `:163-172`). |
| H15 | `mem_analyze.py:396-500` `estimate_weight_size` + `:378-393` MTP formula + `:178-220` display table | add config-field names and a parameter formula per architecture | **Not inherent** | Two mitigations already exist and are better: safetensors byte-exact headers, and vLLM's own `DeviceMemoryProfiler` number, which the report already prefers (`:566-577`). The theoretical formula is a third, worse source kept as fallback. |
| H16 | `collect_torch_profile_case.py:694-699` `--speculative-method` default | changed from a model-specific alias to canonical `mtp` after a crash | **Resolved correctly** | The fix was to stop being model-specific. Worth naming as the good outcome: the default is now vLLM's canonical name and the comment records why. |
| H17 | `collect_torch_profile_case.py:98-100` VL default image path into the `vllm-ascend` submodule | breaks whenever the submodule layout moves | **Not inherent** | A workspace rule explicitly says submodule layout is volatile (`.cursor/rules/submodule-context.mdc`). |
| H18 | `knowledge/db_source_mapping.yaml` `total_cores=20` constant | update per chip generation (`deferred-work.md:246-248`) | **Inherent** | A platform constant; and the golden check will expose it. |
| H19 | `knowledge/hardware_peak_measurements.json` sustained factors (0.95 / 0.65) | measure per chip / per operator path | **Inherent** | Measured facts with provenance, used only for ranking, with MFU kept on theoretical peak (`ascend-profiling-analysis/SKILL.md:288-291`). Correctly pinned. |

### 4.3 The clearest single case

**H2 + H3 together**, and specifically the Kimi-K3 vs Qwen3.5 rule.

The catalogue already contains, in prose, the exact discriminator an agent
needs (`model_fingerprints.json:397`): K3's full attention runs the FIA
kernel *in MLA mode* (latent KV with `kv_lora_rank`/`q_lora_rank` and MLA
companions `mla_preprocess` / `kv_norm_rope_cache`), while Qwen3.5 runs the
*same* FIA kernel in GQA mode with no lora ranks and no MLA companions —
"the FIA kernel name is mode-neutral, so hybrid-family resolution must use
the latent-rank shape evidence and/or the preceding-operator (companion)
evidence, never the score kernel alone."

Every fact in that sentence is in the capture: category sets, shape fields,
and event order are all in `normalized_event_index.csv`. Instead of letting
the agent apply it, the fix was `forbidden: [attention.mla]` on the Qwen3.5
family (`:233`) — a rule that says "not K3" without saying why, that has to
be re-checked for every future hybrid, and that will mis-fire the first time
a real Qwen3.5 variant grows an MLA layer. Meanwhile the honest signal that
this is inference — `base_score: 0` on both K3 and GLM-5 so the generic
families win by default (`:371-373`, `:393`) — is a hand-tuned constant.

Replacement: `summarize` already emits `model_feature_summary.csv`,
`model_inferred_config.csv` and `model_layer_type_summary.csv` from the
capture. Emit the candidate set with its per-candidate *evidence and
contradictions* (which categories matched, which shape fields were visible,
which companions preceded the score kernel) and let the agent resolve it,
with `expected_layers` staying `unknown` until it does. That is what the
skill's own critical rule already demands
(`ascend-profiling-analysis/SKILL.md:43`: "不在算法里硬编码层数 / 模型语义").

### 4.4 Where pinning was genuinely right

- Kernel-name → neutral category (H6). Names are closed-world; the YAML has
  per-rule `evidence: path:line`, aliases for CANN spelling drift, and a
  differential corpus.
- Layer-start / companion markers (H4, H5). Structural facts about a
  kernel's role inside a layer, expressed as data.
- The report format. Section layout, `analysis_summary.json` schema, the
  enum catalogue in `semantic_conventions.yaml`, and the evidence-chain
  validator (`report.py:validate_evidence_chain`, which caught two real bugs
  when introduced — `314ee82`; the validator is `report.py:1228`). A format
  that does not change per model is
  precisely why cross-model comparison works.
- The db-direct adapter plus its golden check (#9, #19) and the documented
  semantic differences in `db_source_mapping.yaml`.
- Hardware peaks with provenance separation (H19).

### 4.5 The `expected_layers` backfill deserves a special note

`profile_analyze.py:497-573` backfills `layer_validation.expected_layers`
from the *workspace* knowledge store when the pipeline found none, marks
`expected_source = "knowledge:<entry_id>"`, recomputes `layers_match`, flips
`status` to `degraded` on mismatch, and writes a `layers_note` explaining the
source. This is the right pattern: an external claim, labelled as external,
that changes a trust marker rather than the data. If the model-guided
segmentation path were reshaped this way — hypothesis in, trust marker out,
frames untouched — most of H1/H9/H10 would stop being maintenance.

### 4.6 How I sized "scripted judgment"

Not a guess; a per-region count of the non-test Python that exists only to
apply a per-model or per-threshold decision:

| Region | Lines | Basis |
|---|---|---|
| `segment.py` model-guided + regime/template machinery | ~2,600 | `:1839-2290` model-guided block (~450) plus the regime-split / repeated-body / exact-template passes (`:856-1760`) that exist to recover a per-model body shape |
| `model_context.py` | 978 | whole file is identity inference; only ~150 lines (URL fetch guards) are mechanics |
| `model_insights.py` candidate matching | ~350 | `:744-853` feature/field-hint scoring and candidate rows |
| `rules.py` family resolver + shape refinement | ~350 | `:371-683` |
| `summarize.py` thresholds + anomaly tagging | ~350 | `:100-148` plus the tag application sites |
| `diagnostics.py` trigger conditions | ~300 | `:194-380` (deliberately in Python; still judgment) |
| `classify.py` block-split heuristics | ~250 | `:145-227` |
| `html_report.py` UI-only heuristics | ~400 | the five functions named in `deferred-work.md:162-173` |
| memory: `weight_inspector` tables + `mem_analyze` estimator | ~750 | `weight_inspector.py:63-214`, `mem_analyze.py:378-500` |
| **Non-test total** | **~6,300–6,700** | ≈22% of 30,732 |
| Tests pinning those decisions | ~4,300 | `test_kernel_signatures` 746, `test_attention_families` 946, `test_segment_validator` 1,158, `test_segment_anchor_stability` 593, `test_moe_families` 146, `test_model_insights` 237, `test_model_context` 258, `test_classify_signatures` 157 |
| **Total judgment-shaped** | **~11,000** | ≈27% of 40,522 |

Genuine mechanics, by the same method: transport and orchestration ~5,300
(collection 3,019 + analysis wrappers 2,288), input/normalize/metrics layer
~3,900, report and renderers ~7,300, sweep/cross-rank/hardware ~1,250,
remaining framework plumbing ~2,000, plus ~5,500 test lines pinning them —
about 25,000 lines, ≈62%. Note that the largest single mechanic (report and
HTML rendering, 7,300 lines) is also the most legitimately pinned thing in
the family.

---

## 5. Diagnosability gaps

Ordered worst first. For each: the script, the failure mode, and what the
agent is left guessing.

### G1. `analysis_status: missing_kernel_details` names a file, not a layer

`run_remote_analyse.py:472-494` returns one enum for every way device data
can fail to land, and the enum set is deliberately frozen because downstream
gates on it (`:479-483`). The manifest adds `expected_output_kind` (db/csv)
but nothing about **why**. The documented causes span four different layers
(`references/acceptance.md:100-108`): capture window too short, `FRAMEWORK/
torch.op_range` never written, `device_*/data` suspiciously small, first-call
lazy compilation. The per-rank `analyse_parallel.log` exists on the container
and its tail is captured *only when `analyse()` itself exits non-zero*
(`:373-377`) — in the `missing_kernel_details` case `analyse()` exited 0, so
the log is never read and never referenced from the manifest. The agent gets
"re-collect required" and has to SSH in and guess which of the four it was.
Cheapest fix: on a non-ok per-rank status, record the rank dir's
`ASCEND_PROFILER_OUTPUT` listing, `PROF_*/device_*/data` size, presence of
`FRAMEWORK/torch.op_range`, and the log path into the manifest.

### G2. Nothing distinguishes "empty profile window" from "empty trace"

The collection skill gates on workload success (`collect_torch_profile_case.py:425-457`)
and on artifact presence, but there is no check that the *window contained
device work*. `/start_profile` and `/stop_profile` returning 200 with a
successful benchmark wave is compatible with a trace whose steps are all
warmup. The analysis skill then discovers it as a segmentation problem
(`no_structural_layers`, `segment.py:3571`) three layers away from the cause.
An event count and a device-busy fraction per rank, read at verify time,
would attribute this at the collection layer.

### G3. Segmentation degradation is reported in three different vocabularies

- `segment_manifest.rank_summaries[].segmentation_strategy.mode` can be
  `model_guided`, `knowledge_uniform_period`, `exact_cover_knowledge_miss`
  (`segment.py:3502`), any of those with an `_anchor_degraded` suffix
  (`:3654`), `no_events`, or `no_structural_layers`.
- `layer_count_validation.status` is independently `ok` / `mismatch` /
  `unknown` with reasons `no_expected_layers` / `anchor_kind_not_validated`,
  plus optional `retry_skipped: small_delta` and `unvalidated`
  (`:1990-2006`, `:3597`, `:3639-3642`).
- The wrapper surfaces only *one* of these at top level: `degraded_ranks`
  for `exact_cover_knowledge_miss` (`profile_analyze.py:759-765`,
  `:1118-1125`). A rank that ran `model_guided` with a layer-count mismatch
  and an anchor degradation reports `segmentation_degraded: false`.

An agent reading the stdout JSON cannot tell "the structure is trustworthy"
from "the structure was forced to match a catalogue entry". `analysis_summary`
does carry `layer_validation` + `segmentation_mode`
(`analysis_summary.py:215-370`), which is the right place — the gap is that
the top-level status fields disagree with it.

### G4. The budget breaker degrades silently in the JSON

`segment.py:1802-1836` gives up after 300 s and returns coarser frames tagged
`composite_split_budget_exceeded`, with a warning on **stderr** only
(`:1820-1826`). Frames carry the tag, but no manifest field aggregates it and
no wrapper field surfaces it. A run that hit the breaker looks identical in
`status`, `segment_count` and `layer_count` to one that converged. The agent
is left guessing whether a coarse layer count is the model or the timer.

### G5. Archive failures leave a half-populated evidence path

Both skills treat archiving as best-effort and record `archive_error`
(`collect_torch_profile_case.py:1029-1035`, `profile_analyze.py:376-405`) —
correct policy. But the collection manifest's per-rank `archived_path` is
then `null` for the failed ranks while the top-level `archive_dir` points at
a directory that exists and is *partially* filled
(`archive_rank_outputs:597-650` is serial and does not roll back). Feeding
that archive root to `--remote-profile-root` later gives a silent
`rank_count_mismatch` at best. Nothing marks the archive root as incomplete
on the archive side; the only record is in a local manifest the next agent
may never see.

### G6. Database schema mismatch surfaces as a source fallback, not a fault

`normalize.py:81-138` probes the profiler db and falls back to
`kernel_details.csv` when the probe fails, recording `source_kinds` and
`source_notes` in `normalize_manifest.json`. In the default collection mode
(`--analyse-export db`) there *is no CSV*, so a schema mismatch on a newer
CANN becomes "no usable source for rank N" rather than "the db schema
changed". Neither the fallback nor its reason is promoted to the wrapper's
stdout JSON; `01b0f7e` ("tolerate absent `COMMUNICATION_*` tables") shows the
schema does drift. An agent sees a thin analysis and no reason to suspect
CANN.

### G7. A missing rank is caught at collection but not at analysis

`--expected-ranks` gives `rank_count_mismatch` at collection time
(`run_remote_analyse.py:600-601`). The analysis wrapper never learns the
expected topology: `--remote-profile-root` runs (historical roots, archive
roots) have no manifest, and `normalize` simply reports the `rank_count` it
found. Cross-rank findings are then computed over whatever ranks exist. The
manifest path does carry `expected_ranks`; the wrapper reads
`analysis_status` and `remote_profile_root` from it (`_common.py:548-576`)
and drops the rest.

### G8. The memory report cannot tell "no msprof" from "wrong phase"

`mem_analyze.py:702-714` reports the residual as "未归因 (缺少 msprof 数据)"
when no `APP` component is present. That is true both when msprof was never
enabled and when the agent skipped step 4 of the manual sequence
(`SKILL.md:143-152`) so the CSVs were never exported. The manifest records a
`serving_state_ref` but nothing that says "this run was expected to have
msprof and the export step has not run yet".

### G9. The memory skill's own limitations contradict the collection skill

`ascend-memory-profiling/SKILL.md:282` states that
"`torch_npu.profiler` via vLLM's `/start_profile`/`/stop_profile` endpoints
currently does not produce device-side data (device_0/data is empty). Use
msprof wrapping instead." The entire collection skill exists because that
path *does* produce device data, and its verification proves it per rank.
One of the two is stale. An agent that reads the memory skill first has a
documented reason not to trust the collection skill. This is the family's
clearest case of the theme in `.agents/knowledge/known-failure-signatures.yaml`
— a measurement problem preserved as a fact about the system.

### G10. The knowledge hooks are wired to an empty room

`collect_torch_profile_case.py:125-215` queries `model-capabilities`,
`parallelism-compatibility` and `known-failure-signatures` before collecting
and again on every hard-fail; `profile_analyze.py:455-494` attaches
`knowledge_refs` to every findings group. Both degrade to `[]` gracefully.
But `.agents/knowledge/known-failure-signatures.yaml` holds exactly two
entries, both transport-level (container hostname missing from `/etc/hosts`;
ssh streams dying on the shared ControlMaster mux) and neither in this
family's domain. Every genuine profiling failure signature this family has
learned lives instead in
`ascend_profile/knowledge/known_counterexamples.md` — prose, inside the skill
package, not queryable by the hooks. The retrieval mechanism is built and the
corpus it retrieves from is empty; the corpus that exists is unreachable.

---

## 6. Proposed collapse

22 runnable entry points → **6 commands, 15 verbs**. No behaviour change is
proposed in this audit; this is the target shape.

```
profile collect      # collect | window | analyse | verify | archive | selftest
profile analyze      # run | sweep | stage | render
profile memory       # collect | analyze
profile golden       # db-vs-csv (developer verb, stays separate on purpose)
```

| New surface | Absorbs | Notes |
|---|---|---|
| `profile collect` | #1, #2, #3, #4, #5 | `window` = `profile_control`; `analyse` = `run_remote_analyse`; `verify` and `archive` become verbs over an existing root; `selftest` folds both selftests. `--expected-ranks` becomes derived-and-required (§3.2.4). |
| `profile analyze run` / `sweep` | #6, #7 | One flag vocabulary; `sweep` gains a comparability assertion over its roots (§3.2.7). |
| `profile analyze stage` | #8–#16 | The 9 stage CLIs become `stage --name segment` on the *pipeline driver*, which already knows the prerequisite rules (`analyze.py:119-132`). |
| `profile analyze render` | #17, #18 | One renderer verb; the legacy renderer keeps its `--renderer legacy` flag but loses its argv-only main. |
| `profile memory collect` | #20 + the two inline snippets | msprof preflight, wrapper upload, baseline capture, both attach phases and the msprof export become one verb with recorded phases (§3.2.1–3). |
| `profile memory analyze` | #21, #22 | `weight_inspector` stays a remote payload, not a user-facing entry point. |

Order I would do it in: `profile collect` first (§7), then
`profile memory collect`, then the stage CLIs, then the renderers.

### What real-hardware validation would make each mature

- **`profile collect`**: the matrix that already exists in the git history —
  7 models × {eager, full_decode_only, piecewise} × {TP2, TP4, TP8, TP16} ×
  {text, vl} × `--analyse-export {db, text}` — plus four *negative* cases
  that are currently only theory: one rank killed mid-window (must give
  `rank_count_mismatch`), a window opened with no traffic (must give a
  workload gate failure), an old CANN without db export (must give a rank
  failure naming `is_support_export_db`), and a full disk during `analyse()`.
  Maturity criterion: each negative case names its layer in the manifest
  without an SSH session.
- **`profile analyze run`**: re-run the existing 61-root sweep baseline
  (`references/behavior.md:139`) with `--source db` and `--source csv` on the
  same captures and require byte-identical `analysis_summary.json` except
  the documented `db_source_mapping.yaml` differences — i.e. extend
  `golden_db_vs_csv.py` from the event stream to the conclusions.
- **`profile analyze stage`**: the golden segmentation fixtures the project
  already specified and deferred (`references/deferred-work.md:135-160`).
  This is the prerequisite for touching `segment.py` at all.
- **`profile memory collect`**: one model at TP4 and TP8, msprof on and off,
  attach and standalone, with the residual required to stay under 200 MB in
  the msprof cases (the number the skill already claims,
  `ascend-memory-profiling/SKILL.md:57`), and a run that deliberately skips
  the export phase to prove G8 is diagnosable.

---

## 7. First capability I would consolidate

`profile collect` — merging `profile_control.py` and `run_remote_analyse.py`
into `collect_torch_profile_case.py` as verbs.

Why first: it is the family's most closed-world region, it is already the
most mature (the parallel driver, the frozen status enum, the two selftests
that execute the real bash), and it is the *upstream* of everything else — a
mis-parameterised collection cannot be fixed downstream
(`references/acceptance.md:100-110`). It is also where the two errors are
cheapest to fix together: pin `--expected-ranks` and the per-rank failure
context (G1, §3.2.4) while moving the workload-shape flags into an explicit
"what am I trying to see" block instead of silent defaults (§3.1, seam 1).
No analysis semantics change, so no report contract moves.

What I would *not* touch first: `segment.py`. The project's own conclusion is
the right one (`references/deferred-work.md:274-278`): its algorithms are
correctness-critical and need golden fixtures across real captures before any
restructuring. The right first move there is not code but contract — stop
letting a catalogue layer count reshape frames, and let the layer-count
invariant report instead of enforce.
