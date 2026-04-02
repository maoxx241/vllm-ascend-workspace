from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class BenchmarkPreset:
    name: str
    serving_preset: str
    probe_runner: str
    request_family: str
    dataset_name: str
    num_prompts: int
    random_input_len: int
    random_output_len: int


def load_benchmark_preset(name: str) -> BenchmarkPreset:
    root = Path(__file__).resolve().parents[2]
    payload = yaml.safe_load(
        (root / "benchmarking" / "presets" / f"{name}.yaml").read_text(encoding="utf-8")
    )
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid benchmark preset: {name}")
    return BenchmarkPreset(
        name=name,
        serving_preset=str(payload["serving_preset"]),
        probe_runner=str(payload["probe_runner"]),
        request_family=str(payload["request_family"]),
        dataset_name=str(payload["dataset_name"]),
        num_prompts=int(payload["num_prompts"]),
        random_input_len=int(payload["random_input_len"]),
        random_output_len=int(payload["random_output_len"]),
    )
