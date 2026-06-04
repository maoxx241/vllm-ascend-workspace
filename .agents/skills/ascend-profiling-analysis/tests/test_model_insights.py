from __future__ import annotations

from ascend_profile.common import NormalizedEvent
from ascend_profile.model_insights import (
    candidate_model_rows,
    operator_efficiency_rows,
    profile_inferred_model_insights,
)


def _event(
    event_id: str,
    name: str,
    *,
    task_type: str = "MatMul",
    categories: tuple[str, ...] = ("compute.matmul",),
    roles: tuple[str, ...] = (),
    shape_features: dict | None = None,
    duration_us: float = 100.0,
) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=event_id,
        profile_id="p0",
        rank_id="rank0",
        source_id="src0",
        row_idx=int(event_id.removeprefix("e") or 0),
        name_raw=name,
        task_type=task_type,
        accelerator_core="AI_CORE",
        stream_id="0",
        start_us=0.0,
        end_us=duration_us,
        duration_us=duration_us,
        wait_us=0.0,
        op_categories=categories,
        op_roles=roles,
        shape_features=shape_features or {},
        op_type="aic",
    )


def test_profile_inferred_model_fingerprint_uses_vocab_shard_and_rank_visible_params() -> None:
    events = [
        _event(
            "e1",
            "lm_head_MatMul",
            shape_features={
                "estimated_work_class": "matmul",
                "estimated_flops": 2 * 4096 * 31040,
                "estimated_bytes": 4096 * 31040 * 2,
                "input_shape_sample": [[1, 4096], [4096, 31040]],
                "output_shape_sample": [[1, 31040]],
            },
        ),
        _event(
            "e2",
            "GroupedMatmulExpert",
            categories=("compute.matmul", "moe.expert_matmul"),
            roles=("moe",),
            shape_features={
                "estimated_work_class": "matmul",
                "estimated_flops": 2 * 4096 * 1024,
                "estimated_bytes": 512 * 4096 * 1024 * 2,
                "input_shape_sample": [[8, 4096], [512, 4096, 1024]],
                "output_shape_sample": [[8, 1024]],
            },
        ),
        _event(
            "e3",
            "FusedInferAttentionScore",
            task_type="FusedInferAttentionScore",
            categories=("attention.flash_score", "attention.linear_or_mamba", "attention.rope"),
            shape_features={
                "estimated_work_class": "attention",
                "input_shape_sample": [[1, 32, 128, 256], [1, 2, 128, 256]],
                "output_shape_sample": [[1, 32, 128, 256]],
            },
        ),
    ]
    step_rows = [{"segment_type": "step", "main_layer_count": 60}]
    layer_rows = [{"block_kinds": ["attention", "moe"]}]

    insights = profile_inferred_model_insights(events, step_rows, layer_rows)
    inferred = {row["field"]: row for row in insights["inferred_config_rows"]}

    assert inferred["vocab_size_or_lm_head_shard"]["inferred_value"] == 31040
    assert inferred["rank_visible_matmul_weight_params_lower_bound"]["inferred_value"] > 0
    assert "single-rank parameter estimates" in " ".join(insights["limitations"])

    top = insights["candidate_model_rows"][0]
    assert top["model_name"] == "Qwen3.5-397B-A17B"
    assert any("vocab_shard=31040x8->248320" == reason for reason in top["matched_reasons"])


def test_candidate_model_rows_tolerates_unknown_non_numeric_fields() -> None:
    rows = [{"field": "hidden_size", "inferred_value": "unknown"}]
    candidates = candidate_model_rows(rows, ["moe"])
    assert candidates


def test_operator_efficiency_rows_rank_shape_derived_work() -> None:
    events = [
        _event(
            "e1",
            "MatMulHot",
            duration_us=200.0,
            shape_features={
                "estimated_work_class": "matmul",
                "estimated_flops": 2_000_000_000.0,
                "estimated_bytes": 20_000_000.0,
                "estimated_dtype": "BF16",
            },
        )
    ]

    rows = operator_efficiency_rows(events)

    assert rows[0]["work_class"] == "matmul"
    assert rows[0]["estimated_flops"] == 2_000_000_000.0
    assert rows[0]["achieved_tflops"] > 0
    assert rows[0]["confidence"] == "shape_estimate"
