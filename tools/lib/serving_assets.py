from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class ServingPreset:
    name: str
    topology: str
    model_profile: str
    served_model_name: str
    device_binding: str
    port: int
    additional_config: dict[str, Any]
    serve_args: dict[str, Any]


@dataclass(frozen=True)
class MaterializedServingConfig:
    preset_name: str
    topology: str
    model_profile: str
    runner_kind: str
    served_model_name: str
    weights_input: str
    device_binding: str
    port: int
    additional_config: dict[str, Any]
    serve_args: dict[str, Any]


def _assets_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _load_yaml(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"invalid asset payload: {path}")
    return payload


def load_serving_preset(name: str) -> ServingPreset:
    payload = _load_yaml(_assets_root() / "serving" / "presets" / f"{name}.yaml")
    return ServingPreset(
        name=name,
        topology=str(payload["topology"]),
        model_profile=str(payload["model_profile"]),
        served_model_name=str(payload["served_model_name"]),
        device_binding=str(payload["device_binding"]),
        port=int(payload["port"]),
        additional_config=dict(payload.get("additional_config", {})),
        serve_args=dict(payload.get("serve_args", {})),
    )


def materialize_serving_config(
    preset: ServingPreset,
    *,
    weights_input: str,
) -> MaterializedServingConfig:
    profile = _load_yaml(_assets_root() / "serving" / "model-profiles" / f"{preset.model_profile}.yaml")
    runner_kind = str(profile["runner_kind"])
    if runner_kind != "generate":
        raise RuntimeError(f"phase-1 serving only supports generate runner, got {runner_kind}")
    additional_config = dict(profile.get("additional_config", {}))
    additional_config.update(preset.additional_config)
    serve_args = dict(profile.get("serve_args", {}))
    serve_args.update(preset.serve_args)
    return MaterializedServingConfig(
        preset_name=preset.name,
        topology=preset.topology,
        model_profile=preset.model_profile,
        runner_kind=runner_kind,
        served_model_name=preset.served_model_name,
        weights_input=weights_input,
        device_binding=preset.device_binding,
        port=preset.port,
        additional_config=additional_config,
        serve_args=serve_args,
    )
