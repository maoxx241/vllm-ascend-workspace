# Step / Layer / Block Class Grouping

This document defines the **shape-strict equality** rule used to merge
similar steps, layers, and blocks into classes.  The intent is that
"two members are in the same class" should be a hard, falsifiable
statement -- not a fuzzy similarity score.

## 1. Class signature

For any scope (step, layer, block) we build the class signature from
two ingredients:

1. The scope's `structure_signature` (or, for blocks, the parent
   layer's structure_signature).  This is the human-readable layer
   period anchor string already produced by `segment.py`
   (e.g. `anchors:attention.gqa_or_mha:fusedinferattentionscore|...`).
2. The **ordered list of `(normalized_op_name, shape_signature)`
   pairs** for events inside the scope that *carry* a `shape_signature`.

The class id is then the BLAKE2b digest over the JSON of `[structure,
scope_label, pairs]`:

```python
def class_id(prefix, structure, scope_label, pairs, fallback_id):
    if not pairs:
        digest = blake2b(fallback_id.encode(), digest_size=6).hexdigest()
        return f"{prefix}_unknown_shape_{digest}"
    payload = json.dumps([structure or "", scope_label or "", list(pairs)],
                         separators=(",", ":"), ensure_ascii=False)
    digest = blake2b(payload.encode(), digest_size=8).hexdigest()
    return f"{prefix}_{digest}"
```

`scope_label` is per-scope context that prevents an attention-only
class from accidentally merging into a moe-only class:

* steps: `"layers=N|main=M"`
* layers: `"k1->k2|companion=0/1"` where `k1, k2` are the contained
  block kinds.
* blocks: `"<kind>|companion=0/1"`.

## 2. Strict equality, order matters

The user's spec is unambiguous:

> Shape 必须完全一样才可以；跨 dp 的话可能存在两个 step 一个 shape 为
> (3, 4)，另一个 shape 为 (4, 3)，这种情况也视为两种不同的 step.

Two consequences for the implementation:

1. We hash the **ordered tuple** of `(name, shape_sig)` pairs.  A step
   that runs a 3×4 matmul before a 4×3 matmul has a different signature
   from one that runs them in reverse, even though the multiset is
   identical.
2. We compare `shape_signature` strings byte-for-byte.  These come from
   `kernel_details.csv:Input Shapes` / `Output Shapes` so any axis
   reordering or dimension difference produces a different signature.

There is intentionally no notion of "similar" or "approximately equal"
shape.  Off-by-one / off-by-axis issues should always show up as a new
class, never as a noisy member of an existing class.

## 3. Missing-data policy

The user explicitly forbade fabricating data:

> 如果缺失 shape 数据的话就不需要合并.

We honour this in two ways:

1. Events without a `shape_signature` (typical: `RmsNorm`, `Argmax`,
   `Cast`, AICPU launchers) are **silently dropped** from the class
   signature.  They are still counted in the per-block / per-layer wall
   metrics, just not used for merging.
2. If a scope has **zero** shape-bearing events the class id becomes
   `*_unknown_shape_<digest>` where `digest` is derived from the member
   id.  Every such scope therefore lands in its own singleton class
   and is never merged with anything.  The aggregated CSVs surface this
   via the `has_unknown_shape` boolean.

This is conservative on purpose: we would rather under-cluster (and
make the report a touch noisier) than report a false equivalence.

## 4. Aggregate computation

Per-class aggregates use **member-level** values (not event-level):

* `wall_ms_mean`, `wall_ms_p50`, `wall_ms_p90` -- arithmetic mean and
  quantiles over the per-member wall_ms.
* `wall_ms_sum` -- sum of per-member wall_ms.  Sorting classes by
  `member_count * wall_ms_mean` (or just `wall_ms_sum`) yields a
  "total-time contribution" ranking that the report uses for top-N
  tables.
* `bound_family` / `dominant_core` -- recomputed on the **summed**
  pipeline aggregate using the rules in `bound_classification.md`.
  This is intentional: a class's bound family describes its overall
  behaviour, not a histogram of bound labels of individual members.
  The histogram still lives next to it
  (`bound_family_member_histogram`) for transparency.
* `comm_share_mean` -- arithmetic mean of the per-member comm-share
  fractions.  We chose mean over weighted sum because the user-facing
  question is "on average, what fraction of an X-block is comms?".
* `top_ops` -- union of per-member `top_ops` lists, summed on
  `duration_sum_us` and `call_count`, then truncated to top 10.  This
  is an approximation: any operator that did not make the top-5 of any
  member never reaches the class top-10.  In practice this is fine
  because the class only contains structurally identical members, so
  the top contributors converge across members.  Consumers wanting the
  exact aggregate should join `block_summary.csv` against
  `class_signatures.json:block_class_by_id`.

## 5. Output files

| File | Scope | Cardinality |
|---|---|---|
| `block_segments.json` | per block | many per layer |
| `class_signatures.json:step_class_by_id` | step → class | one per step |
| `class_signatures.json:layer_class_by_id` | layer → class | one per layer |
| `class_signatures.json:block_class_by_id` | block → class | one per block |
| `class_signatures.json:step_classes` | class → metadata + members | one per class |
| `class_signatures.json:layer_classes` | class → metadata + members | one per class |
| `class_signatures.json:block_classes` | class → metadata + members | one per class |
| `step_class_summary.csv` | one row per step class | aggregated metrics |
| `layer_class_summary.csv` | one row per layer class | aggregated metrics |
| `block_class_summary.csv` | one row per block class | aggregated metrics |

`step_summary.csv` and `layer_summary.csv` carry the class id columns
(`step_class_id`, `layer_class_id`) so a SQL-style join is trivial.
