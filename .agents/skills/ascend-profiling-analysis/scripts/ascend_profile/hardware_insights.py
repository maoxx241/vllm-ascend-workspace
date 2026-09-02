#!/usr/bin/env python3
"""Hardware peak knowledge for Ascend profiling analysis.

The analysis report needs two related but separate denominators:

* theoretical peak, derived from CANN ``platform_config/*.ini`` when the CANN
  package is available on the analysis host;
* sustained peak, a practical operator-path factor recorded in the local
  knowledge base for hardware we have measured.

MFU should use the theoretical denominator.  Sustained peak is for roofline
expectation / reclaim ranking, where ``theory * measured_factor`` is a better
default for real kernels.
"""

from __future__ import annotations

import configparser
import glob
import json
import os
import re
from pathlib import Path
from typing import Any, Mapping, Sequence

KNOWLEDGE_DIR = Path(__file__).resolve().parent / "knowledge"
HARDWARE_MEASUREMENTS_PATH = KNOWLEDGE_DIR / "hardware_peak_measurements.json"
HARDWARE_THEORETICAL_SNAPSHOT_PATH = KNOWLEDGE_DIR / "hardware_theoretical_peaks_cann9_0_0.json"


def _f(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _norm(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _split_mkn(value: Any) -> tuple[int, int, int]:
    parts = [part.strip() for part in str(value or "").split(",")]
    if len(parts) != 3:
        return (0, 0, 0)
    return (_i(parts[0]), _i(parts[1]), _i(parts[2]))


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_hardware_measurements(path: Path = HARDWARE_MEASUREMENTS_PATH) -> list[dict[str, Any]]:
    payload = _read_json(path)
    rows = payload.get("measurements") if isinstance(payload, Mapping) else []
    return [dict(item) for item in rows or [] if isinstance(item, Mapping)]


def load_static_theoretical_peaks(path: Path = HARDWARE_THEORETICAL_SNAPSHOT_PATH) -> list[dict[str, Any]]:
    payload = _read_json(path)
    rows = payload.get("rows") if isinstance(payload, Mapping) else []
    out = [dict(item) for item in rows or [] if isinstance(item, Mapping)]
    for row in out:
        row.setdefault("theoretical_peak_source", "knowledge_snapshot")
        row.setdefault("confidence", "cann_derived_theoretical")
    return out


def _candidate_cann_roots() -> list[Path]:
    candidates: list[Path] = []
    for key in ("ASCEND_HOME_PATH", "ASCEND_TOOLKIT_HOME"):
        value = os.environ.get(key)
        if value:
            candidates.append(Path(value))
    candidates.extend(
        [
            Path("/usr/local/Ascend/ascend-toolkit/latest"),
            Path("/usr/local/Ascend/latest"),
        ]
    )
    for pattern in ("/usr/local/Ascend/cann-*", "/usr/local/Ascend/ascend-toolkit/*"):
        for item in glob.glob(pattern):
            candidates.append(Path(item))
    out: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        try:
            resolved = item.resolve()
        except OSError:
            resolved = item
        key = str(resolved)
        if key in seen or not resolved.exists():
            continue
        seen.add(key)
        out.append(resolved)
    return out


def _platform_config_dirs(cann_roots: Sequence[Path] | None = None) -> list[Path]:
    roots = list(cann_roots) if cann_roots is not None else _candidate_cann_roots()
    dirs: list[Path] = []
    for root in roots:
        for rel in (
            "aarch64-linux/data/platform_config",
            "arm64-linux/data/platform_config",
            "x86_64-linux/data/platform_config",
            "data/platform_config",
        ):
            path = root / rel
            if path.is_dir():
                dirs.append(path)
    out: list[Path] = []
    seen: set[str] = set()
    for item in dirs:
        key = str(item.resolve())
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def _section(cfg: configparser.ConfigParser, name: str) -> Mapping[str, str]:
    return cfg[name] if cfg.has_section(name) else {}


def parse_cann_platform_config(path: Path) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str
    parser.read(path, encoding="utf-8")
    version = _section(parser, "version")
    soc = _section(parser, "SoCInfo")
    spec = _section(parser, "AICoreSpec")
    rates = _section(parser, "AICoreMemoryRates")
    dtype_mkn = _section(parser, "DtypeMKN")

    soc_version = version.get("SoC_version") or path.stem
    short_soc = version.get("Short_SoC_version") or soc_version
    ai_core_cnt = _i(soc.get("ai_core_cnt"))
    cube_core_cnt = _i(soc.get("cube_core_cnt"), ai_core_cnt)
    if cube_core_cnt <= 0:
        cube_core_cnt = ai_core_cnt
    vector_core_cnt = _i(soc.get("vector_core_cnt"))
    cube_freq_mhz = _f(spec.get("cube_freq"))
    default_mkn = _split_mkn(dtype_mkn.get("Default"))
    if not all(default_mkn):
        default_mkn = (
            _i(spec.get("cube_m_size")),
            _i(spec.get("cube_k_size")),
            _i(spec.get("cube_n_size")),
        )
    int8_mkn = _split_mkn(dtype_mkn.get("DT_INT8"))
    int4_mkn = _split_mkn(dtype_mkn.get("DT_INT4"))

    def cube_peak(mkn: tuple[int, int, int]) -> float:
        m, k, n = mkn
        if not (cube_core_cnt and cube_freq_mhz and m and k and n):
            return 0.0
        # One MMA computes m*n*k multiply-accumulate operations per cycle.
        # Count FMA as two operations, matching LLM FLOPs/MFU convention.
        return cube_core_cnt * cube_freq_mhz * 1e6 * m * n * k * 2.0 / 1e12

    fp16_tflops = cube_peak(default_mkn)
    support_bf16 = str(soc.get("support_bf16") or "").strip() in {"1", "true", "True"}
    bf16_tflops = fp16_tflops if support_bf16 else 0.0
    int8_tops = cube_peak(int8_mkn)
    int4_tops = cube_peak(int4_mkn)
    memory_size = _i(soc.get("memory_size"))
    ddr_rate = _f(rates.get("ddr_rate"))
    ddr_gbps = cube_core_cnt * cube_freq_mhz * 1e6 * ddr_rate / 1e9 if cube_core_cnt and cube_freq_mhz and ddr_rate else 0.0

    return {
        "soc_version": soc_version,
        "short_soc_version": short_soc,
        "source_path": str(path),
        "aic_version": version.get("AIC_version"),
        "ccec_aic_version": version.get("CCEC_AIC_version"),
        "npu_arch": version.get("NpuArch"),
        "ai_core_cnt": ai_core_cnt,
        "cube_core_cnt": cube_core_cnt,
        "vector_core_cnt": vector_core_cnt,
        "cube_freq_mhz": cube_freq_mhz,
        "cube_m_size": default_mkn[0],
        "cube_k_size": default_mkn[1],
        "cube_n_size": default_mkn[2],
        "fp16_tflops": round(fp16_tflops, 6) if fp16_tflops else None,
        "bf16_tflops": round(bf16_tflops, 6) if bf16_tflops else None,
        "int8_tops": round(int8_tops, 6) if int8_tops else None,
        "int4_tops": round(int4_tops, 6) if int4_tops else None,
        "memory_size_bytes": memory_size or None,
        "memory_size_gib": round(memory_size / (1024**3), 3) if memory_size else None,
        "cann_ddr_rate_bytes_per_cycle": ddr_rate or None,
        "cann_ddr_derived_gbps": round(ddr_gbps, 6) if ddr_gbps else None,
        "theoretical_peak_source": "cann_platform_config",
        "confidence": "cann_derived_theoretical",
    }


def cann_theoretical_peak_rows(cann_roots: Sequence[Path] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for directory in _platform_config_dirs(cann_roots):
        for path in sorted(directory.glob("*.ini")):
            try:
                row = parse_cann_platform_config(path)
            except (configparser.Error, OSError, UnicodeDecodeError):
                continue
            key = str(row.get("source_path"))
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)
    rows.sort(key=lambda item: (str(item.get("soc_version") or ""), str(item.get("source_path") or "")))
    return rows or load_static_theoretical_peaks()


def _load_hardware_profile_file(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    if not path.is_file():
        return {
            "available": False,
            "source": str(path),
            "reason": f"hardware profile not found: {path}",
        }
    payload = _read_json(path)
    payload.setdefault("source", str(path))
    payload.setdefault("available", True)
    return payload


def _match_measurement(model: str | None, measurements: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not model:
        return None
    want = _norm(model)
    for row in measurements:
        values = [row.get("hardware_model"), *(row.get("aliases") or [])]
        if any(_norm(value) == want for value in values):
            return row
    for row in measurements:
        values = [row.get("hardware_model"), *(row.get("aliases") or [])]
        if any(want and (want in _norm(value) or _norm(value) in want) for value in values):
            return row
    return None


def _match_theoretical(model: str | None, theoretical_rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    if not model:
        return None
    want = _norm(model)
    for row in theoretical_rows:
        values = [row.get("soc_version"), row.get("short_soc_version")]
        if any(_norm(value) == want for value in values):
            return row
    for row in theoretical_rows:
        values = [row.get("soc_version"), row.get("short_soc_version")]
        if any(want and (want in _norm(value) or _norm(value) in want) for value in values):
            return row
    return None


def _sustained_value(theoretical: Mapping[str, Any], measurement: Mapping[str, Any] | None, key: str) -> tuple[float | None, float | None]:
    theory = _f(theoretical.get(key))
    if not theory:
        return None, None
    factors = measurement.get("sustained_factors") if isinstance(measurement, Mapping) else {}
    factor_key = {
        "fp16_tflops": "fp16_matmul",
        "bf16_tflops": "bf16_matmul",
        "int8_tops": "int8_quant_matmul",
    }.get(key)
    factor = _f(factors.get(factor_key), 0.0) if isinstance(factors, Mapping) and factor_key else 0.0
    if factor <= 0:
        return None, None
    return round(theory * factor, 6), factor


def build_hardware_insights(
    *,
    hardware_model: str | None = None,
    hardware_profile_path: Path | None = None,
    scan_cann: bool = True,
) -> dict[str, Any]:
    """Build hardware summary rows and select the best peak profile."""

    measurements = load_hardware_measurements()
    theoretical_rows = cann_theoretical_peak_rows() if scan_cann else []
    profile_file = _load_hardware_profile_file(hardware_profile_path)
    selected_theoretical: Mapping[str, Any] | None = None
    selected_measurement: Mapping[str, Any] | None = None
    selected_source = "unknown"
    selected_model = hardware_model

    if profile_file and profile_file.get("available"):
        selected_model = str(profile_file.get("hardware_model") or profile_file.get("soc_version") or hardware_model or "user_supplied")
        selected_theoretical = profile_file.get("theoretical") if isinstance(profile_file.get("theoretical"), Mapping) else profile_file
        selected_measurement = profile_file.get("measurement") if isinstance(profile_file.get("measurement"), Mapping) else None
        selected_source = "hardware_profile"
    else:
        selected_theoretical = _match_theoretical(hardware_model, theoretical_rows)
        selected_measurement = _match_measurement(hardware_model, measurements)
        selected_source = "cann_platform_config" if selected_theoretical else "unknown"
        if selected_model is None and selected_theoretical:
            selected_model = str(selected_theoretical.get("soc_version") or selected_theoretical.get("short_soc_version"))

    if selected_theoretical is None and selected_measurement and hardware_model:
        selected_theoretical = _match_theoretical(str(selected_measurement.get("hardware_model") or hardware_model), theoretical_rows)
        selected_source = "cann_platform_config" if selected_theoretical else "measurement_only"

    fp16_sustained, fp16_factor = _sustained_value(selected_theoretical or {}, selected_measurement, "fp16_tflops")
    bf16_sustained, bf16_factor = _sustained_value(selected_theoretical or {}, selected_measurement, "bf16_tflops")
    int8_sustained, int8_factor = _sustained_value(selected_theoretical or {}, selected_measurement, "int8_tops")

    summary = {
        "hardware_model": selected_model or "unknown",
        "hardware_source": selected_source,
        "hardware_profile_source": profile_file.get("source") if isinstance(profile_file, Mapping) else None,
        "theoretical_peak_source": (selected_theoretical or {}).get("theoretical_peak_source"),
        "measurement_source": (selected_measurement or {}).get("source"),
        "fp16_tflops_theoretical": (selected_theoretical or {}).get("fp16_tflops"),
        "bf16_tflops_theoretical": (selected_theoretical or {}).get("bf16_tflops"),
        "int8_tops_theoretical": (selected_theoretical or {}).get("int8_tops"),
        "fp16_tflops_sustained": fp16_sustained,
        "bf16_tflops_sustained": bf16_sustained,
        "int8_tops_sustained": int8_sustained,
        "fp16_sustained_factor": fp16_factor,
        "bf16_sustained_factor": bf16_factor,
        "int8_sustained_factor": int8_factor,
        "memory_size_gib": (selected_theoretical or {}).get("memory_size_gib"),
        "cann_ddr_derived_gbps": (selected_theoretical or {}).get("cann_ddr_derived_gbps"),
        "confidence": "selected" if selected_theoretical else "missing_hardware_context",
    }
    context_rows = [{"key": key, "value": value} for key, value in summary.items()]
    limitations = [
        "hardware current-host probing is not profiling provenance; MFU is valid only when the selected hardware is known to be the capture hardware",
        "CANN platform_config provides theoretical cube peaks; sustained peaks come from local measurement knowledge when available",
        "cann_ddr_derived_gbps is derived from CANN microarchitecture rates and is not treated as an official HBM bandwidth peak",
    ]
    if not selected_theoretical:
        limitations.append("no matching CANN theoretical peak was found; operator roofline falls back to achieved-only metrics")
    if not selected_measurement:
        limitations.append("no sustained measurement factor matched the selected hardware; sustained roofline fields are omitted")

    return {
        "available": bool(selected_theoretical),
        "hardware_model": summary["hardware_model"],
        "summary": summary,
        "summary_rows": context_rows,
        "selected_theoretical": dict(selected_theoretical or {}),
        "selected_measurement": dict(selected_measurement or {}),
        "theoretical_peak_rows": theoretical_rows,
        "measurement_rows": [dict(row) for row in measurements],
        "limitations": limitations,
    }


def _dtype_peak_key(dtype: str) -> str | None:
    """Map a dtype token to the closest available peak field.

    Only fp16/bf16/int8 peaks are recorded in the hardware knowledge base.
    int4 cubes share the integer TOPS counter family (CANN DtypeMKN shows
    int4 doubling int8 K, so int8_tops is a conservative lower bound), and
    fp8/hif8 8-bit float matmuls run at the same 8-bit op rate as int8 on
    chips that expose them.  Dtypes with no modeled peak return ``None`` so
    callers can say ``no_peak_for_dtype`` instead of silently assuming fp16.
    """
    text = str(dtype or "").lower()
    if "int8" in text or "s8" in text or "u8" in text or "int4" in text or "u4" in text:
        return "int8_tops"
    if "fp8" in text or "float8" in text or "hif8" in text or "e4m3" in text or "e5m2" in text:
        return "int8_tops"
    if "bf16" in text or "bfloat16" in text:
        return "bf16_tflops"
    if "fp16" in text or "float16" in text or "half" in text:
        return "fp16_tflops"
    return None


def peak_flops_per_second(
    hardware: Mapping[str, Any] | None,
    *,
    work_class: str,
    dtype: str,
    sustained: bool = False,
) -> tuple[float, str]:
    """Return peak operations per second for one modeled operator group."""

    if not hardware:
        return 0.0, "missing_hardware"
    summary = hardware.get("summary") if isinstance(hardware.get("summary"), Mapping) else {}
    if work_class not in {"matmul", "attention"}:
        return 0.0, "no_peak_for_work_class"
    key = _dtype_peak_key(dtype)
    if key is None:
        return 0.0, "no_peak_for_dtype"
    if sustained:
        sustained_key = {
            "fp16_tflops": "fp16_tflops_sustained",
            "bf16_tflops": "bf16_tflops_sustained",
            "int8_tops": "int8_tops_sustained",
        }[key]
        value = _f(summary.get(sustained_key))
        if value:
            return value * 1e12, "sustained_measurement_factor"
        return 0.0, "missing_sustained_factor"
    theory_key = {
        "fp16_tflops": "fp16_tflops_theoretical",
        "bf16_tflops": "bf16_tflops_theoretical",
        "int8_tops": "int8_tops_theoretical",
    }[key]
    value = _f(summary.get(theory_key))
    if value:
        return value * 1e12, "cann_theoretical"
    return 0.0, "missing_theoretical_peak"


def memory_bandwidth_bytes_per_second(hardware: Mapping[str, Any] | None) -> tuple[float, str]:
    if not hardware:
        return 0.0, "missing_hardware"
    summary = hardware.get("summary") if isinstance(hardware.get("summary"), Mapping) else {}
    gbps = _f(summary.get("cann_ddr_derived_gbps"))
    if gbps:
        return gbps * 1e9, "cann_ddr_derived"
    return 0.0, "missing_memory_bandwidth"
