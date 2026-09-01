from __future__ import annotations

from pathlib import Path

from ascend_profile.hardware_insights import (
    load_static_theoretical_peaks,
    parse_cann_platform_config,
    peak_flops_per_second,
)
from ascend_profile.model_insights import operator_efficiency_rows

from test_model_insights import _event


def test_parse_cann_platform_config_derives_910b4_cube_peaks(tmp_path: Path) -> None:
    cfg = tmp_path / "Ascend910B4.ini"
    cfg.write_text(
        """
[version]
SoC_version=Ascend910B4
Short_SoC_version=Ascend910B
AIC_version=AIC-C-220
NpuArch=2201

[SoCInfo]
ai_core_cnt=20
cube_core_cnt=20
vector_core_cnt=40
memory_size=34359738368
support_bf16=1

[AICoreSpec]
cube_freq=1500
cube_m_size=16
cube_n_size=16
cube_k_size=16

[AICoreMemoryRates]
ddr_rate=32

[DtypeMKN]
Default=16,16,16
DT_INT8=16,32,16
DT_INT4=16,64,16
""",
        encoding="utf-8",
    )

    row = parse_cann_platform_config(cfg)

    assert row["soc_version"] == "Ascend910B4"
    assert row["fp16_tflops"] == 245.76
    assert row["bf16_tflops"] == 245.76
    assert row["int8_tops"] == 491.52
    assert row["memory_size_gib"] == 32.0
    assert row["cann_ddr_derived_gbps"] == 960.0


def test_static_cann_theoretical_peak_snapshot_contains_910b4() -> None:
    rows = load_static_theoretical_peaks()
    by_soc = {row["soc_version"]: row for row in rows}

    assert len(rows) >= 1
    assert by_soc["Ascend910B4"]["fp16_tflops"] == 245.76
    assert by_soc["Ascend910B4"]["int8_tops"] == 491.52


def test_peak_flops_selects_theoretical_and_sustained_dtype_paths() -> None:
    hardware = {
        "summary": {
            "hardware_model": "Ascend910B4",
            "fp16_tflops_theoretical": 245.76,
            "bf16_tflops_theoretical": 245.76,
            "int8_tops_theoretical": 491.52,
            "fp16_tflops_sustained": 233.472,
            "bf16_tflops_sustained": 233.472,
            "int8_tops_sustained": 319.488,
        }
    }

    bf16_theory, bf16_source = peak_flops_per_second(
        hardware, work_class="matmul", dtype="BF16", sustained=False
    )
    bf16_sustained, bf16_sustained_source = peak_flops_per_second(
        hardware, work_class="matmul", dtype="BF16", sustained=True
    )
    int8_sustained, _source = peak_flops_per_second(
        hardware, work_class="matmul", dtype="INT8", sustained=True
    )

    assert bf16_theory == 245.76e12
    assert bf16_source == "cann_theoretical"
    assert bf16_sustained == 233.472e12
    assert bf16_sustained_source == "sustained_measurement_factor"
    assert int8_sustained == 319.488e12


def test_dtype_peak_key_maps_int4_fp8_hif8_to_int8_peak() -> None:
    hardware = {"summary": {"int8_tops_theoretical": 491.52}}

    for dtype in ("INT4", "FP8", "float8_e4m3fn", "HIF8"):
        peak, source = peak_flops_per_second(hardware, work_class="matmul", dtype=dtype)
        assert peak == 491.52e12, dtype
        assert source == "cann_theoretical", dtype


def test_unknown_dtype_reports_no_peak_instead_of_silent_fp16() -> None:
    hardware = {"summary": {"fp16_tflops_theoretical": 245.76}}

    peak, source = peak_flops_per_second(hardware, work_class="matmul", dtype="FP32")
    assert (peak, source) == (0.0, "no_peak_for_dtype")

    peak, source = peak_flops_per_second(hardware, work_class="attention", dtype="")
    assert (peak, source) == (0.0, "no_peak_for_dtype")


def test_operator_efficiency_uses_hardware_theory_and_sustained_peaks() -> None:
    hardware = {
        "summary": {
            "hardware_model": "Ascend910B4",
            "bf16_tflops_theoretical": 245.76,
            "bf16_tflops_sustained": 233.472,
        }
    }
    events = [
        _event(
            "e1",
            "MatMulHot",
            duration_us=1_000_000.0,
            shape_features={
                "estimated_work_class": "matmul",
                "estimated_flops": 245.76e12,
                "estimated_bytes": 0.0,
                "estimated_dtype": "BF16",
            },
        )
    ]

    rows = operator_efficiency_rows(events, hardware=hardware)

    assert rows[0]["hardware_model"] == "Ascend910B4"
    assert rows[0]["theoretical_peak_tflops_or_tops"] == 245.76
    assert rows[0]["sustained_peak_tflops_or_tops"] == 233.472
    assert rows[0]["mfu_theoretical"] == 1.0
    assert rows[0]["sustained_efficiency"] > 1.0
