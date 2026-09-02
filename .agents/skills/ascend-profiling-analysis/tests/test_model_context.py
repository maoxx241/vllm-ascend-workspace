from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

from ascend_profile import model_context
from ascend_profile.common import NormalizedEvent
from ascend_profile.model_context import resolve_model_context


def _no_network():
    """Guard: any external config fetch during these tests is a bug."""
    return mock.patch.object(
        model_context,
        "_read_url_text",
        side_effect=AssertionError("unexpected external network access in test"),
    )


def _event(
    event_id: str,
    name: str,
    *,
    categories: tuple[str, ...],
    roles: tuple[str, ...] = (),
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        profile_id="profile_test",
        rank_id="rank0",
        source_id="source_test",
        row_idx=int(event_id.removeprefix("e") or 0),
        name_raw=name,
        task_type=name,
        accelerator_core="AI_CORE",
        stream_id="0",
        start_us=0.0,
        end_us=1.0,
        duration_us=1.0,
        wait_us=0.0,
        op_categories=categories,
        op_roles=roles,
    )


def _candidate_names(context: dict) -> set[str]:
    return set(context.get("candidate_model_names") or [])


def test_operator_fingerprint_matches_dsv4_family_from_compressor() -> None:
    context = resolve_model_context(
        events=[
            _event("e1", "Compressor", categories=("attention.kv_compressor",), roles=("attention_aux",)),
            _event("e2", "LightningIndexer", categories=("attention.lightning_indexer",), roles=("attention_aux",)),
            _event("e3", "SparseAttnSharedKV", categories=("attention.sparse_sharedkv",), roles=("attention",)),
            _event("e4", "MoeGatingTopK", categories=("moe.gating",), roles=("moe",)),
        ]
    )

    assert context["model_name"] == "DeepSeek-V4 family"
    assert context["source"] == "profile_operator_fingerprint:operator_match"
    assert context["candidate_expected_layers"] == [43, 61]
    assert _candidate_names(context) == {"DeepSeek-V4-Flash", "DeepSeek-V4-Pro"}
    assert "operator:attention.kv_compressor" in context["matched_reasons"]


def test_operator_fingerprint_matches_qwen35_family_from_linear_moe() -> None:
    context = resolve_model_context(
        events=[
            _event("e1", "MoeGatingTopK", categories=("moe.gating",), roles=("moe",)),
            _event("e2", "RecurrentGatedDeltaRule", categories=("attention.linear_or_mamba",), roles=("attention",)),
            _event("e3", "FusedInferAttentionScore", categories=("attention.flash_score",), roles=("attention",)),
            _event("e4", "RotaryMul", categories=("attention.rope",), roles=("attention_aux",)),
        ]
    )

    assert context["model_name"] == "Qwen3.5 family"
    assert context["source"] == "profile_operator_fingerprint:operator_match"
    assert "Qwen3.5-397B-A17B" in _candidate_names(context)
    assert "operator:attention.linear_or_mamba" in context["matched_reasons"]
    assert "operator:moe.gating" in context["matched_reasons"]


def test_operator_fingerprint_matches_dsa_without_confusing_dsv4() -> None:
    with _no_network():
        context = resolve_model_context(
            events=[
                _event("e1", "LightningIndexer", categories=("attention.lightning_indexer",), roles=("attention_aux",)),
                _event("e2", "SparseAttnSharedKV", categories=("attention.sparse_sharedkv",), roles=("attention",)),
                _event("e3", "MoeGatingTopK", categories=("moe.gating",), roles=("moe",)),
            ]
        )

    assert context["model_name"] == "DeepSeek DSA sparse-attention family"
    assert context["source"] == "profile_operator_fingerprint:operator_match"
    assert context["expected_layers"] is None
    assert "operator:attention.lightning_indexer" in context["matched_reasons"]
    assert "operator:attention.sparse_sharedkv" in context["matched_reasons"]


def test_moe_gating_alone_is_generic_not_specific_model_guess() -> None:
    context = resolve_model_context(
        events=[_event("e1", "MoeGatingTopK", categories=("moe.gating",), roles=("moe",))]
    )

    assert context["model_name"] == "MoE architecture"
    assert context["expected_layers"] is None
    assert context["source"] == "profile_operator_fingerprint:generic"
    assert "candidate_model_names" not in context


def test_user_structure_hint_matches_catalog_before_external_lookup() -> None:
    context = resolve_model_context(model_id="CSA MoE")

    assert context["model_name"] == "DeepSeek-V4 family"
    assert context["source"] == "model_fingerprint_catalog:user_structure_family_variants"
    assert context["candidate_expected_layers"] == [43, 61]
    assert _candidate_names(context) == {"DeepSeek-V4-Flash", "DeepSeek-V4-Pro"}
    assert "config_resolution" not in context


def test_local_config_context_does_not_require_catalog_match(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"model_type": "toy_moe", "num_hidden_layers": 7, "num_experts": 4}),
        encoding="utf-8",
    )

    context = resolve_model_context(model_id="toy", model_config=config)

    assert context["model_name"] == "toy"
    assert context["expected_layers"] == 7
    assert "moe" in context["features"]


def test_invalid_model_config_degrades_gracefully(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text("{ not valid json", encoding="utf-8")

    context = resolve_model_context(model_id="toy", model_config=config)

    assert context["available"] is False
    assert context["expected_layers"] is None
    assert context["matched_reasons"] == [f"config_parse_error:{config}"]
    assert any("not valid JSON" in item for item in context["limitations"])


def test_non_object_model_config_degrades_gracefully(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(json.dumps(["not", "a", "model"]), encoding="utf-8")

    context = resolve_model_context(model_config=config)

    assert context["available"] is False
    assert any("not a JSON object" in item for item in context["limitations"])


def test_fuzzy_model_id_substring_hit_is_capped_at_medium() -> None:
    # "dsv2lite" is long enough to substring-match, but a fuzzy hit must
    # never be reported as high confidence.
    with _no_network():
        context = resolve_model_context(model_id="xxdsv2liteyy")

    assert context["model_name"] == "DeepSeek-V2-Lite"
    assert context["confidence"] == "medium"
    assert "fuzzy_model_id_match" in context["matched_reasons"]
    assert context["expected_layers"] == 27


def test_exact_model_id_hit_keeps_high_confidence() -> None:
    with _no_network():
        context = resolve_model_context(model_id="DeepSeek-V2-Lite")

    assert context["model_name"] == "DeepSeek-V2-Lite"
    assert context["confidence"] == "high"
    assert "fuzzy_model_id_match" not in context["matched_reasons"]


def test_short_alias_does_not_substring_match_arbitrary_ids() -> None:
    models = model_context.load_model_fingerprints()

    matches, _kind = model_context._catalog_matches_by_model_id("xxdsaxx", models)
    assert matches == []

    # A whole-token hit on a short alias is still allowed.
    matches, kind = model_context._catalog_matches_by_model_id("my dsa build", models)
    assert kind == "fuzzy"
    assert [item["model_name"] for item in matches] == ["DeepSeek DSA sparse-attention family"]


def test_family_display_name_is_not_turned_into_repo_candidates() -> None:
    family = {
        "model_name": "DeepSeek DSA sparse-attention family",
        "aliases": ["dsa", "deepseek-dsa"],
    }

    assert model_context._repo_id_candidates("DeepSeek DSA sparse-attention family", family) == []


def test_external_fetch_skips_invalid_repo_ids_without_network() -> None:
    family = {
        "model_name": "DeepSeek DSA sparse-attention family",
        "aliases": ["dsa"],
    }
    with _no_network():
        config, resolution = model_context.fetch_external_model_config(
            "DeepSeek DSA sparse-attention family", family
        )

    assert config is None
    assert resolution["status"] == "not_found"
    assert resolution["attempts"] == []


def test_external_fetch_still_uses_valid_repo_ids() -> None:
    payload = json.dumps({"num_hidden_layers": 9})
    with mock.patch.object(model_context, "_read_url_text", return_value=payload) as reader:
        config, resolution = model_context.fetch_external_model_config("deepseek-ai/DeepSeek-V4-Pro", None)

    assert config is not None
    assert resolution["status"] == "ok"
    assert resolution["repo_id"] == "deepseek-ai/DeepSeek-V4-Pro"
    assert resolution["expected_layers"] == 9
    assert reader.call_count == 1
    assert "huggingface.co/deepseek-ai/DeepSeek-V4-Pro" in reader.call_args_list[0].args[0]


def test_family_context_tolerates_non_numeric_expected_layers() -> None:
    resolved = model_context._single_or_family_context(
        model_id="toy",
        matches=[{"model_name": "toy family"}],
        candidate_contexts=[
            {"available": True, "model_name": "toy-a", "expected_layers": "unknown"},
            {"available": True, "model_name": "toy-b", "expected_layers": 61},
        ],
        observed_features=[],
    )

    assert resolved["candidate_expected_layers"] == [61]
