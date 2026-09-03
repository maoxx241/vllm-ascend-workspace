#!/usr/bin/env python3
"""Kernel taxonomy rules and attention-family resolution.

``categories_and_roles`` classifies one kernel into op_categories +
op_roles; ``resolve_attention_family`` maps a block's category set to the
paper-aligned family label; ``refine_dense_attention_from_shapes`` is the
best-effort shape-driven refinement of the dense umbrella label. The
knowledge files under ``knowledge/`` are the runtime rule source for the
first two (``kernel_signatures.yaml:match_rules`` and
``attention_families.yaml:cheat_sheet.resolver``).
"""

from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

try:
    from .models import NormalizedEvent
    from .store import KNOWLEDGE_DIR, fold_text
except ImportError:  # pragma: no cover - script-mode fallback
    from models import NormalizedEvent  # type: ignore[no-redef]
    from store import KNOWLEDGE_DIR, fold_text  # type: ignore[no-redef]


# ----------------------------------------------------------------------------
# Kernel signature matcher (data-driven)
# ----------------------------------------------------------------------------
# The ordered rule list lives in ``knowledge/kernel_signatures.yaml`` under
# ``match_rules:`` and is LOADED AT RUNTIME by ``categories_and_roles`` —
# adding a kernel rule is a YAML edit, not a code edit. The loader validates
# the schema strictly and raises RuntimeError naming the offending rule, so
# a malformed knowledge file fails fast at normalize time instead of
# silently misclassifying kernels.
#
# Evaluation model (deliberately tiny — clause dicts, no DSL):
#   text       = fold_text(f"{name} {task_type} {accelerator_core}")
#   state      = category set accumulated so far + named text predicates
#   rule.when  = clause dict; all keys in a clause are ANDed
#   branches   = optional ordered variants; first match wins, a branch
#                without ``when`` is the fall-through (else)
#
# When adding a new kernel:
#   1. Add the rule under ``match_rules:`` in kernel_signatures.yaml.
#   2. Add the kernel to the ``kernels:`` inventory with
#      ``evidence: path:line``.
#   3. If it changes a family's must-have set, update
#      ``attention_families.yaml`` or ``moe_families.yaml``.
#   4. Add any new category to ``semantic_conventions.yaml`` so the schema
#      test passes.

_KERNEL_SIGNATURES_PATH = KNOWLEDGE_DIR / "kernel_signatures.yaml"

_CLAUSE_KEYS = frozenset({
    "any_of",
    "all_of",
    "none_of",
    "starts_with_any",
    "unless_category",
    "unless_category_prefix",
    "predicate",
    "not_predicate",
    "match_any",
})
_TOKEN_LIST_KEYS = ("any_of", "all_of", "none_of", "starts_with_any")
_RULE_KEYS = frozenset({"id", "when", "add_categories", "add_roles", "branches"})
_BRANCH_KEYS = frozenset({"when", "add_categories", "add_roles"})


def _schema_error(path: Path, context: str, problem: str) -> RuntimeError:
    return RuntimeError(f"{path.name}: {context}: {problem}")


def _validate_token_list(path: Path, context: str, key: str, value: Any) -> None:
    if value is None:
        return
    if not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value):
        raise _schema_error(path, context, f"{key} must be a non-empty list of tokens")


def _validate_clause(
    path: Path,
    context: str,
    clause: Any,
    predicates: set[str],
    *,
    allow_match_any: bool,
) -> None:
    if not isinstance(clause, dict):
        raise _schema_error(path, context, f"condition clause must be a mapping, got {type(clause).__name__}")
    unknown = set(clause) - _CLAUSE_KEYS
    if unknown:
        raise _schema_error(path, context, f"unknown condition keys: {sorted(unknown)}")
    for key in _TOKEN_LIST_KEYS:
        _validate_token_list(path, context, key, clause.get(key))
    for key in ("unless_category", "unless_category_prefix"):
        value = clause.get(key)
        if value is not None and not isinstance(value, str):
            raise _schema_error(path, context, f"{key} must be a string")
    for key in ("predicate", "not_predicate"):
        value = clause.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise _schema_error(path, context, f"{key} must be a string")
        if value not in predicates:
            raise _schema_error(path, context, f"{key} references undefined predicate {value!r}")
    match_any = clause.get("match_any")
    if match_any is not None:
        if not allow_match_any:
            raise _schema_error(path, context, "match_any may not be nested inside match_any")
        if not isinstance(match_any, list) or not match_any:
            raise _schema_error(path, context, "match_any must be a non-empty list of sub-clauses")
        for sub in match_any:
            _validate_clause(path, f"{context} match_any", sub, predicates, allow_match_any=False)


def _validate_effect_list(path: Path, context: str, owner: dict[str, Any], key: str) -> None:
    value = owner.get(key)
    if value is not None and (not isinstance(value, list) or not value or not all(isinstance(item, str) for item in value)):
        raise _schema_error(path, context, f"{key} must be a non-empty list of labels")


def _load_match_rules(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Load and strictly validate ``match_rules:`` from kernel_signatures.yaml."""

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - yaml is a hard dependency
        raise RuntimeError("PyYAML is required to load knowledge/kernel_signatures.yaml") from exc
    if not path.exists():
        raise RuntimeError(f"kernel signature knowledge base missing: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    match_rules = doc.get("match_rules")
    if not isinstance(match_rules, dict):
        raise _schema_error(path, "match_rules", "section missing or not a mapping")

    raw_predicates = match_rules.get("predicates") or {}
    if not isinstance(raw_predicates, dict):
        raise _schema_error(path, "match_rules.predicates", "must be a mapping")
    predicates: dict[str, Any] = {}
    for name, clause in raw_predicates.items():
        # Predicates are pure text matchers; they may not reference other
        # predicates (keeps evaluation order-free).
        _validate_clause(path, f"predicate {name!r}", clause, set(), allow_match_any=True)
        predicates[str(name)] = clause

    raw_rules = match_rules.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise _schema_error(path, "match_rules.rules", "must be a non-empty list")
    rules: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for index, rule in enumerate(raw_rules):
        context = f"rule #{index + 1}"
        if not isinstance(rule, dict):
            raise _schema_error(path, context, f"rule must be a mapping, got {type(rule).__name__}")
        rule_id = rule.get("id")
        if not isinstance(rule_id, str) or not rule_id:
            raise _schema_error(path, context, "rule needs a non-empty string id")
        context = f"rule {rule_id!r}"
        if rule_id in seen_ids:
            raise _schema_error(path, context, "duplicate rule id")
        seen_ids.add(rule_id)
        unknown = set(rule) - _RULE_KEYS
        if unknown:
            raise _schema_error(path, context, f"unknown rule keys: {sorted(unknown)}")
        if rule.get("when") is not None:
            _validate_clause(path, f"{context} when", rule["when"], set(predicates), allow_match_any=True)
        branches = rule.get("branches")
        if branches is not None:
            if rule.get("add_categories") is not None or rule.get("add_roles") is not None:
                raise _schema_error(path, context, "rule may not mix branches with top-level add_categories/add_roles")
            if not isinstance(branches, list) or not branches:
                raise _schema_error(path, context, "branches must be a non-empty list")
            for branch_idx, branch in enumerate(branches):
                branch_context = f"{context} branch #{branch_idx + 1}"
                if not isinstance(branch, dict):
                    raise _schema_error(path, branch_context, "branch must be a mapping")
                unknown_branch = set(branch) - _BRANCH_KEYS
                if unknown_branch:
                    raise _schema_error(path, branch_context, f"unknown branch keys: {sorted(unknown_branch)}")
                if branch.get("when") is not None:
                    _validate_clause(path, f"{branch_context} when", branch["when"], set(predicates), allow_match_any=True)
                elif branch_idx < len(branches) - 1:
                    raise _schema_error(path, branch_context, "branch without `when` (else) must be the last branch")
                if branch.get("add_categories") is None and branch.get("add_roles") is None:
                    raise _schema_error(path, branch_context, "branch has no effect (needs add_categories and/or add_roles)")
                _validate_effect_list(path, branch_context, branch, "add_categories")
                _validate_effect_list(path, branch_context, branch, "add_roles")
        else:
            if rule.get("add_categories") is None and rule.get("add_roles") is None:
                raise _schema_error(path, context, "rule has no effect (needs add_categories and/or add_roles)")
        _validate_effect_list(path, context, rule, "add_categories")
        _validate_effect_list(path, context, rule, "add_roles")
        rules.append(rule)
    return predicates, rules


@functools.lru_cache(maxsize=1)
def _kernel_match_rules() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return _load_match_rules(_KERNEL_SIGNATURES_PATH)


def _clause_matches(
    clause: Mapping[str, Any],
    text: str,
    categories: set[str],
    predicates: Mapping[str, Any],
) -> bool:
    tokens = clause.get("any_of")
    if tokens and not any(token in text for token in tokens):
        return False
    tokens = clause.get("all_of")
    if tokens and not all(token in text for token in tokens):
        return False
    tokens = clause.get("none_of")
    if tokens and any(token in text for token in tokens):
        return False
    tokens = clause.get("starts_with_any")
    if tokens and not any(text.startswith(token) for token in tokens):
        return False
    category = clause.get("unless_category")
    if category is not None and category in categories:
        return False
    prefix = clause.get("unless_category_prefix")
    if prefix is not None and any(cat.startswith(prefix) for cat in categories):
        return False
    predicate = clause.get("predicate")
    if predicate is not None and not _clause_matches(predicates[predicate], text, categories, predicates):
        return False
    not_predicate = clause.get("not_predicate")
    if not_predicate is not None and _clause_matches(predicates[not_predicate], text, categories, predicates):
        return False
    match_any = clause.get("match_any")
    if match_any and not any(_clause_matches(sub, text, categories, predicates) for sub in match_any):
        return False
    return True


_CATEGORY_ROLE_CACHE: dict[tuple[str, str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {}


def categories_and_roles(name: str, task_type: str, accelerator_core: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Classify one kernel into op_categories + op_roles.

    Data-driven: the ordered rule list is ``match_rules:`` in
    ``knowledge/kernel_signatures.yaml`` (loaded once per process and
    schema-validated). Rules evaluate in order against the folded text
    ``fold_text(f"{name} {task_type} {accelerator_core}")``; effects are
    set-union into the category/role sets, so order only matters for
    conditions that read the accumulated categories (``unless_category`` /
    ``unless_category_prefix``) — e.g. the generic RoPE fallback runs after
    the specific RoPE rules.

    **Naming policy — paper vs CANN backend:**

    * Architecture-family labels (``mla`` / ``dsa`` / ``csa`` / ``hca``)
      are the names used in the DeepSeek papers and are what we surface
      in the report.
    * CANN / vllm-ascend route them through *backend* classes:
      ``AscendMLAImpl`` for MLA, ``AscendSFAImpl`` for both DSA (V3.2)
      and CSA (V4). The runtime backend is annotated separately and is
      NOT used as a category name to avoid hiding the paper-level
      distinction.
    * Kernel-level categories are **neutral** so the same Compressor
      kernel can serve both CSA (V4) and HCA (V4); the architecture
      family is then resolved from the *combination* of kernels present
      in a block (see ``resolve_attention_family``).
    """
    cache_key = (name, task_type, accelerator_core)
    cached = _CATEGORY_ROLE_CACHE.get(cache_key)
    if cached is not None:
        return cached
    text = fold_text(f"{name} {task_type} {accelerator_core}")
    predicates, rules = _kernel_match_rules()
    categories: set[str] = set()
    roles: set[str] = set()
    for rule in rules:
        when = rule.get("when")
        if when is not None and not _clause_matches(when, text, categories, predicates):
            continue
        effects: dict[str, Any] | None = rule
        branches = rule.get("branches")
        if branches is not None:
            effects = next(
                (
                    branch
                    for branch in branches
                    if branch.get("when") is None
                    or _clause_matches(branch["when"], text, categories, predicates)
                ),
                None,
            )
        if effects is None:
            continue
        categories.update(effects.get("add_categories") or ())
        roles.update(effects.get("add_roles") or ())
    result = (tuple(sorted(categories)), tuple(sorted(roles)))
    _CATEGORY_ROLE_CACHE[cache_key] = result
    return result
    return result


# ----------------------------------------------------------------------------
# Attention family resolver (category-driven)
# ----------------------------------------------------------------------------
# Single source of truth for the paper-aligned attention family label used
# by both ``html_report.detect_attention_subtype`` and the unit tests in
# ``tests/test_attention_families.py``. Keeping the decision logic here
# (instead of duplicating it as a private helper in either consumer)
# guarantees the test contract and the HTML report agree.
#
# Inputs are category labels emitted by ``categories_and_roles``, NOT raw
# kernel names — that way:
#   * metadata-only categories like ``attention.sparse_sharedkv.metadata``
#     can't masquerade as the main ``attention.sparse_sharedkv``
#     signature (regression-tested);
#   * future kernel renames touch one place (``categories_and_roles`` /
#     ``kernel_signatures.yaml``) and the resolver stays unchanged.
#
# Returns one of the paper-aligned family names:
#
#   * ``csa``         — DeepSeek-V4 main layers (Compressed Sparse Attention)
#   * ``hca``         — DeepSeek-V4 alternating layers (Heavily Compressed
#                       Attention; heuristic)
#   * ``dsa``         — DeepSeek-V3.2 (DeepSeek Sparse Attention, arxiv
#                       2512.02556)
#   * ``mla``         — DeepSeek-V2 / V3 (Multi-head Latent Attention)
#   * ``linear``      — Mamba / GDN / linear attention
#   * ``gqa_or_mha``  — dense flash-style attention via FIA /
#                       UnpadFlashAttention. Both kernels support
#                       MHA *and* GQA via the ``num_key_value_heads``
#                       parameter. This resolver function looks at
#                       *categories only* and therefore can't pick
#                       between MHA and GQA; it returns the umbrella
#                       ``gqa_or_mha``. A best-effort downstream step
#                       (``refine_dense_attention_from_shapes``) reads
#                       the Q/K Input Shapes recorded in
#                       ``kernel_details.csv`` and refines this to
#                       ``mha`` / ``gqa`` / ``mqa`` when shapes are
#                       available and pass sanity checks. The
#                       refinement is a heuristic — when shapes are
#                       missing or ambiguous, the report keeps the
#                       umbrella ``gqa_or_mha``.
#   * ``attn``        — unknown / unclassified
#
# An ``+kvc`` suffix is appended if the Hamming-distance KV-compression
# overlay is active (decode-only opt-in).
#
# Why ``attention.flash_score`` is neutral (not ``attention.gqa_or_mha``):
# the underlying CANN op
# (``aclnnFusedInferAttentionScore*`` / ``npu_fused_infer_attention_score``)
# is documented to handle MHA, GQA, AND MLA via parameter configuration.
# Naming the *kernel* category after one specific architecture would
# leak architecture inference into the kernel layer; we keep the kernel
# category neutral and resolve the architecture from the *combination*
# of categories present in a block.



# Sanity-check guard for shape parsing: the last axis of Q/K tensors fed
# to FIA / UnpadFlashAttention is the per-head dim. If we accidentally
# pick up a mask, position table, or scale tensor, the "last axis" will
# rarely fall in this set, so we drop the candidate. Values cover the
# range seen across vLLM-Ascend supported models (dense path); MLA
# layers carry their own NoPE+RoPE concat head_dim values (192 / 576)
# but are resolved earlier in the decision order, so they don't reach
# the dense refinement path.
_VALID_HEAD_DIMS = frozenset({
    16, 32, 48, 64, 80, 96, 112, 128, 144, 160, 192, 224, 256,
    320, 384, 448, 512, 576, 640, 768, 1024,
})


# vLLM model attention heads are within this range (sanity check).
_MAX_NUM_HEADS = 1024


# Names recognised as dense flash-attention score kernels for shape-based
# refinement. Keep in sync with the rule in ``categories_and_roles`` that
# emits ``attention.flash_score``.
#
# NOTE: The ATB ``pagedattention`` kernels also feed the dense flash-score
# path on Ascend (qwen25vl, glm45_0919, qwen25vl7b uses *both* this kernel
# AND ``UnpadFlashAttentionBF16NdKernel``). They were previously omitted
# from the token list which caused the shape-refinement pass to skip every
# qwen25vl/glm-0919 event silently. The refinement still returns the
# umbrella ``gqa_or_mha`` for paged-K layouts because num_kv_heads is not
# directly recoverable from the cache shape, but at least the events are
# now considered and the cases that DO carry non-paged shapes (UnpadFA
# prefill) can refine to mha / gqa / mqa.
_FLASH_SCORE_NAME_TOKENS = (
    "fusedinferattentionscore",
    "unpadflashattention",
    "flashattentionscore",
    "flashattention",
    "pagedattentionmask",
    "pagedattention",
)


def _parse_shape_token(token: str) -> list[int] | None:
    """Parse one CANN ``Input Shapes`` token into a list of positive
    integers. Returns ``None`` when the token is empty, malformed, or
    contains non-positive dims (which would mean a placeholder /
    optional input).
    """
    s = token.strip().strip('"').strip()
    if not s or s in ("()", "[]"):
        return None
    s = s.strip("()[]")
    parts = re.split(r"[,;\s]+", s)
    dims: list[int] = []
    for part in parts:
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            return None
        if value <= 0:
            return None
        dims.append(value)
    return dims or None


# When K is a 3D paged-KV-cache tensor ``[num_blocks, block_size, head_dim]``
# the original num_kv_heads has been folded into ``num_blocks * block_size``
# and we cannot recover it from shape alone. Heuristic: if K[0] is much
# larger than Q[0] we likely picked up a paged-cache K (e.g. Q=[4, 4, 256]
# vs K=[10000, 128, 256] on decode). The threshold below is conservative
# enough that batched-tokens Q vs concatenated-tokens K don't trip it
# (e.g. Q=[1620, 4, 128] vs K=[1620, 4, 128] passes).
_PAGED_K_BATCH_RATIO_GUARD = 8


def _qk_head_counts_from_input_shapes(
    input_shapes: Sequence[str],
) -> tuple[int, int] | None:
    """Pull ``(num_q_heads, num_kv_heads)`` from the first two valid
    Q/K tensors in a CANN ``Input Shapes`` list.

    The CANN ABI for both ``aclnnFusedInferAttentionScore[V2-V5]`` and
    ``UnpadFlashAttention`` puts ``query`` at input[0] and ``key`` at
    input[1]; we scan forward until we find two tensors whose last axis
    is a plausible per-head dim, then read the second-to-last axis as
    the head count. Returns ``None`` if the shapes don't satisfy the
    dense flash-attention invariant.

    Supported Q/K layouts:
      * 3D batch-major: ``[total_tokens, num_heads, head_dim]``
        (FIA prefill, UnpadFA non-paged).
      * 4D batched:     ``[B, S, num_heads, head_dim]``.

    Refuses to read:
      * 3D paged K-cache ``[num_blocks, block_size, head_dim]`` — here
        the second-to-last axis is ``block_size``, NOT ``num_kv_heads``;
        ``num_kv_heads`` has been folded into ``num_blocks * block_size``
        and is not directly recoverable. Detected via K[0] >> Q[0].
      * 5D+ tensors (unknown layout).
    """
    candidates: list[tuple[int, int, list[int]]] = []  # (num_heads, head_dim, dims)
    for token in input_shapes:
        dims = _parse_shape_token(token)
        if dims is None or len(dims) < 3 or len(dims) > 4:
            continue
        head_dim = dims[-1]
        num_heads = dims[-2]
        if head_dim not in _VALID_HEAD_DIMS:
            continue
        if num_heads < 1 or num_heads > _MAX_NUM_HEADS:
            continue
        candidates.append((num_heads, head_dim, dims))
        if len(candidates) >= 2:
            break
    if len(candidates) < 2:
        return None
    (num_q, head_dim_q, dims_q), (num_kv, head_dim_k, dims_k) = (
        candidates[0],
        candidates[1],
    )
    if head_dim_q != head_dim_k:
        # Q and K must share head_dim on FIA / UnpadFA — mismatch means
        # we latched onto the wrong tensor (mask / pse / etc).
        return None
    # Paged-K guard (decode direction): if both Q and K are 3D and K[0]
    # is much larger than Q[0], K is almost certainly a paged-cache
    # layout where the second-to-last axis is ``block_size``, not
    # ``num_kv_heads``. Bail out so we don't emit a wrong refinement.
    if (
        len(dims_q) == 3
        and len(dims_k) == 3
        and dims_q[0] > 0
        and dims_k[0] >= dims_q[0] * _PAGED_K_BATCH_RATIO_GUARD
    ):
        return None
    # Paged-K guard (prefill direction): in prefill mode Q carries the
    # total query token count and K is the paged cache. Q[0] can easily
    # exceed K[0] (e.g. Q=[32768,8,256], K=[1950,128,256] from nextprof),
    # so the decode-direction guard above doesn't fire. However, the
    # cache's second-to-last axis (which the loop reads as
    # ``num_kv_heads``) is really the block_size and is therefore much
    # larger than the true number of KV heads — and in real
    # MHA / GQA / MQA the invariant ``num_kv_heads <= num_q_heads``
    # always holds. So whenever the candidate Q/K pair violates that
    # invariant we are looking at a paged layout (or worse, the wrong
    # tensor) and must bail out instead of emitting a bogus refinement.
    if num_kv > num_q:
        return None
    return num_q, num_kv


def _split_cann_shapes_field(value: str) -> list[str]:
    """Replicates ``html_report._split_semi`` here so ``rules.py`` stays
    independent of the report module. CANN ``Input Shapes`` is a
    ``;``-separated list; the cell may be quoted to escape inner ``;``.
    """
    if not value:
        return []
    v = value.strip()
    while len(v) >= 2 and v.startswith('"') and v.endswith('"'):
        v = v[1:-1]
        if not v:
            break
    if not v:
        return []
    return [tok.strip() for tok in v.split(';')]


def refine_dense_attention_from_shapes(events: Iterable[Any]) -> str:
    """Best-effort upgrade of the terminal ``gqa_or_mha`` family label
    to ``mha`` / ``gqa`` / ``mqa`` using shapes recorded in
    ``kernel_details.csv:Input Shapes`` for FIA / UnpadFA events.

    Returns one of ``{'mha', 'gqa', 'mqa', 'gqa_or_mha'}``. The last
    value means refinement was not possible (shapes missing, malformed,
    or events disagreed without a clear majority); in that case the
    caller should keep the ``gqa_or_mha`` terminal label.

    **Best-effort, NOT a contract.** The skill does not read HF
    ``config.json``; this heuristic relies on the CANN profiler
    serialising the Q/K Input Shapes in the kernel_details row. That
    field can be missing or quirky after aclgraph compilation, so
    treat the refined sub-kind as an annotation, not a guarantee.

    Decision rules:
      * ``num_q_heads == num_kv_heads``                            → ``mha``
      * ``num_kv_heads == 1``  AND ``num_q_heads > 1``             → ``mqa``
      * ``num_q_heads > num_kv_heads`` AND ratio divides evenly    → ``gqa``
      * non-integer ratio, or shapes failed the sanity checks      → no vote
      * disagreement without a clear majority across events        → ``gqa_or_mha``
    """
    votes: dict[str, int] = {"mha": 0, "gqa": 0, "mqa": 0}
    for event in events:
        raw_name = getattr(event, "name", "") or ""
        text = raw_name.lower()
        if not any(token in text for token in _FLASH_SCORE_NAME_TOKENS):
            continue
        raw = getattr(event, "raw_row", None) or {}
        shapes_value = raw.get("Input Shapes") or raw.get("Input Shape") or ""
        if not shapes_value:
            continue
        tokens = _split_cann_shapes_field(str(shapes_value))
        head_counts = _qk_head_counts_from_input_shapes(tokens)
        if head_counts is None:
            continue
        num_q, num_kv = head_counts
        if num_q == num_kv and num_q >= 1:
            votes["mha"] += 1
        elif num_kv == 1 and num_q > 1:
            votes["mqa"] += 1
        elif num_q > num_kv and num_q % num_kv == 0:
            votes["gqa"] += 1
        # else: silent skip — odd ratios are usually parsing slip-ups

    total = sum(votes.values())
    if total == 0:
        return "gqa_or_mha"
    winner, score = max(votes.items(), key=lambda kv: kv[1])
    # Require an outright majority (not just plurality) so that a
    # ambiguous mix doesn't get a spuriously confident label.
    if score * 2 <= total:
        return "gqa_or_mha"
    return winner


@functools.lru_cache(maxsize=1)
def _attention_family_resolver() -> tuple[tuple[dict[str, Any], ...], dict[str, Any]]:
    """Load ``cheat_sheet.resolver`` + ``cheat_sheet.overlay_rule`` from
    ``knowledge/attention_families.yaml`` (schema-validated).

    The YAML is the single source of truth for the decision order; the
    prose ``decision_order`` list next to it documents the same steps for
    humans. Missing / malformed knowledge fails fast instead of silently
    mislabelling attention blocks.
    """

    path = KNOWLEDGE_DIR / "attention_families.yaml"
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as exc:  # pragma: no cover - yaml is a hard dependency
        raise RuntimeError("PyYAML is required to load knowledge/attention_families.yaml") from exc
    if not path.exists():
        raise RuntimeError(f"attention family knowledge base missing: {path}")
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cheat_sheet = doc.get("cheat_sheet")
    if not isinstance(cheat_sheet, dict):
        raise _schema_error(path, "cheat_sheet", "section missing or not a mapping")
    families = doc.get("families") or {}

    raw_steps = cheat_sheet.get("resolver")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise _schema_error(path, "cheat_sheet.resolver", "must be a non-empty list of steps")
    steps: list[dict[str, Any]] = []
    for index, step in enumerate(raw_steps):
        context = f"cheat_sheet.resolver step #{index + 1}"
        if not isinstance(step, dict):
            raise _schema_error(path, context, f"step must be a mapping, got {type(step).__name__}")
        family = step.get("family")
        if not isinstance(family, str) or not family:
            raise _schema_error(path, context, "step needs a non-empty string family")
        if family != "attn" and family not in families:
            raise _schema_error(path, context, f"family {family!r} is not declared under families: (only the 'attn' fallback is exempt)")
        unknown = set(step) - {"family", "all", "any", "none"}
        if unknown:
            raise _schema_error(path, context, f"unknown step keys: {sorted(unknown)}")
        for key in ("all", "any", "none"):
            _validate_token_list(path, context, key, step.get(key))
        steps.append(step)
    if not any(not step.get("all") and not step.get("any") and not step.get("none") for step in steps):
        raise _schema_error(path, "cheat_sheet.resolver", "needs an unconditional fallback step (attn)")

    overlay = cheat_sheet.get("overlay_rule")
    if not isinstance(overlay, dict):
        raise _schema_error(path, "cheat_sheet.overlay_rule", "section missing or not a mapping")
    unknown = set(overlay) - {"when_any", "suffix"}
    if unknown:
        raise _schema_error(path, "cheat_sheet.overlay_rule", f"unknown keys: {sorted(unknown)}")
    _validate_token_list(path, "cheat_sheet.overlay_rule", "when_any", overlay.get("when_any"))
    if not isinstance(overlay.get("suffix"), str) or not overlay["suffix"]:
        raise _schema_error(path, "cheat_sheet.overlay_rule", "suffix must be a non-empty string")
    return tuple(steps), overlay


def resolve_attention_family(categories: Iterable[str]) -> str:
    """Decide the paper-aligned attention family label from a set of
    category names emitted by ``categories_and_roles``.

    The decision order is loaded from
    ``knowledge/attention_families.yaml:cheat_sheet.resolver`` — an ordered
    list of ``{family, all?, any?, none?}`` steps; the first matching step
    wins and the unconditional ``attn`` step is the fallback. The
    ``+kvc`` overlay suffix comes from ``cheat_sheet.overlay_rule``.

    NOTE: ``attention.sparse_sharedkv.metadata`` is deliberately not part
    of any step's ``all`` list — a metadata-only block must not classify
    as DSA / CSA. MLA-architected layers resolve before ``linear`` and
    ``gqa_or_mha`` because the MLA companions (MlaProlog /
    KvRmsNormRopeCache / MLA V-up-proj) take precedence over the bare
    flash_score signal (MLA decode reuses FIA for the score step).
    """
    cats = set(categories)
    steps, overlay = _attention_family_resolver()
    base = "attn"
    for step in steps:
        all_of = step.get("all") or ()
        any_of = step.get("any") or ()
        none_of = step.get("none") or ()
        if all_of and not all(cat in cats for cat in all_of):
            continue
        if any_of and not any(cat in cats for cat in any_of):
            continue
        if none_of and any(cat in cats for cat in none_of):
            continue
        base = str(step["family"])
        break
    if any(cat in cats for cat in overlay.get("when_any") or ()):
        base = f"{base}{overlay['suffix']}"
    return base


def is_aicpu_event(event: NormalizedEvent) -> bool:
    text = f"{event.task_type} {event.accelerator_core} {' '.join(event.op_categories)}".lower()
    return "aicpu" in text or "ai_cpu" in text


def is_comm_event(event: NormalizedEvent) -> bool:
    return "communication" in event.op_roles or "communication.collective" in event.op_categories


def is_ai_core_like(event: NormalizedEvent) -> bool:
    text = f"{event.task_type} {event.accelerator_core}".upper()
    if is_aicpu_event(event) or is_comm_event(event):
        return False
    return any(token in text for token in ("AI_CORE", "AICORE", "AI_VECTOR", "AIVECTOR", "MIX_AIC", "MIXAIC"))
