from __future__ import annotations

from typing import Any, Dict, Mapping


ALLOWED_ACTION_KINDS = ("probe", "repair", "bootstrap", "cleanup", "execute")
REQUIRED_RESULT_KEYS = {"status", "observations"}
OPTIONAL_RESULT_KEYS = {
    "reason",
    "side_effects",
    "next_probes",
    "retryable",
    "idempotent",
    "payload",
}
MUTATING_ACTION_KINDS = {"repair", "bootstrap", "cleanup", "execute"}


def _is_json_compatible(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_is_json_compatible(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_compatible(item) for key, item in value.items())
    return False


def validate_action_kind(action_kind: str) -> str:
    if action_kind not in ALLOWED_ACTION_KINDS:
        raise RuntimeError(f"unsupported action_kind: {action_kind}")
    return action_kind


def validate_tool_result(result: Mapping[str, Any], *, action_kind: str) -> Dict[str, Any]:
    kind = validate_action_kind(action_kind)
    if not isinstance(result, Mapping):
        raise RuntimeError("tool result must be a mapping")

    keys = set(result)
    missing = REQUIRED_RESULT_KEYS - keys
    if missing:
        raise RuntimeError(f"missing required result keys: {sorted(missing)}")

    allowed = REQUIRED_RESULT_KEYS | OPTIONAL_RESULT_KEYS
    unexpected = keys - allowed
    if unexpected:
        raise RuntimeError(f"unexpected result keys: {sorted(unexpected)}")

    observations = result["observations"]
    if not isinstance(observations, list) or not all(isinstance(item, str) for item in observations):
        raise RuntimeError("observations must be a list of strings")

    normalized: Dict[str, Any] = {
        "status": str(result["status"]),
        "observations": list(observations),
    }

    for key in ("reason", "retryable", "idempotent"):
        if key in result:
            normalized[key] = result[key]

    for key in ("side_effects", "next_probes"):
        if key in result:
            value = result[key]
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise RuntimeError(f"{key} must be a list of strings")
            normalized[key] = list(value)

    if "payload" in result:
        payload = result["payload"]
        if not isinstance(payload, Mapping):
            raise RuntimeError("payload must be a mapping")
        payload_mapping = dict(payload)
        if not _is_json_compatible(payload_mapping):
            raise RuntimeError("payload must be JSON-compatible")
        normalized["payload"] = payload_mapping

    if kind in MUTATING_ACTION_KINDS and "side_effects" not in normalized:
        raise RuntimeError(f"{kind} results must declare side_effects")

    return normalized
