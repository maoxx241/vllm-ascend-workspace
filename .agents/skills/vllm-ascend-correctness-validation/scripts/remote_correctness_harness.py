#!/usr/bin/env python3
"""Run normalized offline or online vLLM correctness cases."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Mapping

SCHEMA_VERSION = 1
SUPPORTED_MODES = frozenset({"offline-generate", "offline-chat", "online-chat"})


class HarnessError(ValueError):
    """Raised when the harness config or response is invalid."""


def _prioritize_workspace_python_packages(
    workspace_root: Path | None = None,
) -> None:
    """Keep outer repository directories from shadowing editable packages."""
    root = workspace_root or Path(__file__).resolve().parents[4]
    source_roots = (root / "vllm", root / "vllm-ascend")
    source_values = [str(path) for path in source_roots if path.is_dir()]
    for source_root in reversed(source_roots):
        if not source_root.is_dir():
            continue
        value = str(source_root)
        while value in sys.path:
            sys.path.remove(value)
        sys.path.insert(0, value)
    inherited = [
        value
        for value in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        if value and value not in source_values
    ]
    os.environ["PYTHONPATH"] = os.pathsep.join([*source_values, *inherited])


def emit_progress(phase: str, **details: Any) -> None:
    print(json.dumps({"phase": phase, **details}, ensure_ascii=False), file=sys.stderr)


def load_config(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HarnessError(f"cannot read harness config {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise HarnessError("harness config root must be an object")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise HarnessError(f"schema_version must be {SCHEMA_VERSION}")
    if not isinstance(payload.get("cases"), list) or not payload["cases"]:
        raise HarnessError("cases must be a non-empty array")
    return payload


def _normalize_vllm_output(request_output: Any) -> dict[str, Any]:
    outputs = getattr(request_output, "outputs", None)
    if not outputs:
        raise HarnessError("vLLM request output has no candidates")
    output = outputs[0]
    token_ids = list(getattr(output, "token_ids", []) or [])
    return {
        "text": str(getattr(output, "text", "")),
        "token_ids": token_ids,
    }


def normalize_online_response(payload: Mapping[str, Any]) -> dict[str, Any]:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise HarnessError("online response has no choices")
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise HarnessError("online response choice must be an object")
    message = choice.get("message", {})
    text = message.get("content", "") if isinstance(message, Mapping) else ""
    normalized: dict[str, Any] = {"text": str(text)}
    logprobs = choice.get("logprobs")
    if isinstance(logprobs, Mapping) and isinstance(logprobs.get("content"), list):
        tokens = [
            item.get("token")
            for item in logprobs["content"]
            if isinstance(item, Mapping) and isinstance(item.get("token"), str)
        ]
        if tokens:
            normalized["tokens"] = tokens
            numeric_logprobs = [
                item.get("logprob")
                for item in logprobs["content"]
                if isinstance(item, Mapping)
                and isinstance(item.get("logprob"), (int, float))
                and not isinstance(item.get("logprob"), bool)
            ]
            if numeric_logprobs:
                normalized["numerics"] = {"logprobs": numeric_logprobs}
    return normalized


def _online_request(
    *, base_url: str, model: str, request: Mapping[str, Any], sampling: Mapping[str, Any]
) -> dict[str, Any]:
    payload = {
        "model": model,
        "messages": request.get("messages"),
        **dict(sampling),
    }
    if payload["messages"] is None:
        raise HarnessError("online-chat request.messages is required")
    payload.setdefault("logprobs", True)
    data = json.dumps(payload).encode("utf-8")
    target = base_url.rstrip("/") + "/v1/chat/completions"
    http_request = urllib.request.Request(
        target,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(http_request, timeout=120) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HarnessError(f"online request failed: {exc}") from exc
    if not isinstance(response_payload, Mapping):
        raise HarnessError("online response root must be an object")
    return normalize_online_response(response_payload)


def _build_offline_engine(config: Mapping[str, Any]):
    _prioritize_workspace_python_packages()
    try:
        from vllm import LLM, SamplingParams
    except ImportError as exc:
        raise HarnessError("vLLM is unavailable in this runtime") from exc
    model = config.get("model")
    if not isinstance(model, str) or not model:
        raise HarnessError("offline config.model is required")
    engine_args = config.get("engine_args", {})
    if not isinstance(engine_args, Mapping):
        raise HarnessError("engine_args must be an object")
    return LLM(model=model, **dict(engine_args)), SamplingParams


def execute_config(config: Mapping[str, Any]) -> dict[str, Any]:
    label = str(config.get("label", "unnamed"))
    offline_cases = [
        case
        for case in config["cases"]
        if isinstance(case, Mapping)
        and case.get("mode") in {"offline-generate", "offline-chat"}
    ]
    engine = None
    sampling_class = None
    engine_error: str | None = None
    if offline_cases:
        try:
            engine, sampling_class = _build_offline_engine(config)
        except HarnessError as exc:
            engine_error = str(exc)

    results: list[dict[str, Any]] = []
    for raw_case in config["cases"]:
        if not isinstance(raw_case, Mapping):
            results.append(
                {"id": "invalid-case", "status": "error", "error": "case must be an object"}
            )
            continue
        case_id = str(raw_case.get("id", "invalid-case"))
        mode = raw_case.get("mode")
        if mode not in SUPPORTED_MODES:
            results.append(
                {
                    "id": case_id,
                    "status": "unsupported",
                    "error": f"unsupported harness mode: {mode}",
                    "outputs": [],
                    "metrics": {},
                }
            )
            continue
        repeats = int(raw_case.get("repeats", 1))
        request = raw_case.get("request", {})
        sampling = raw_case.get("sampling", {})
        outputs: list[dict[str, Any]] = []
        try:
            if mode.startswith("offline") and engine_error is not None:
                raise HarnessError(engine_error)
            for repeat in range(repeats):
                emit_progress("execute-case", case_id=case_id, repeat=repeat + 1)
                if mode == "online-chat":
                    base_url = config.get("base_url")
                    model = config.get("served_model") or config.get("model")
                    if not isinstance(base_url, str) or not base_url:
                        raise HarnessError("online config.base_url is required")
                    if not isinstance(model, str) or not model:
                        raise HarnessError("online served_model or model is required")
                    outputs.append(
                        _online_request(
                            base_url=base_url,
                            model=model,
                            request=request,
                            sampling=sampling,
                        )
                    )
                else:
                    assert engine is not None and sampling_class is not None
                    sampling_params = sampling_class(**dict(sampling))
                    if mode == "offline-generate":
                        prompt = request.get("prompt")
                        if not isinstance(prompt, str):
                            raise HarnessError("offline-generate request.prompt is required")
                        generated = engine.generate(
                            prompt, sampling_params, use_tqdm=False
                        )
                    else:
                        messages = request.get("messages")
                        if not isinstance(messages, list):
                            raise HarnessError("offline-chat request.messages is required")
                        generated = engine.chat(
                            messages, sampling_params=sampling_params, use_tqdm=False
                        )
                    if not generated:
                        raise HarnessError("vLLM returned no request outputs")
                    outputs.append(_normalize_vllm_output(generated[0]))
        except Exception as exc:  # Runtime boundary must normalize backend failures.
            results.append(
                {
                    "id": case_id,
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "outputs": outputs,
                    "metrics": {},
                }
            )
        else:
            results.append(
                {
                    "id": case_id,
                    "status": "ok",
                    "outputs": outputs,
                    "metrics": {},
                }
            )
    return {
        "schema_version": SCHEMA_VERSION,
        "label": label,
        "cases": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        result = execute_config(config)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except HarnessError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "status": "completed",
                "output": str(args.output.resolve()),
                "case_count": len(result["cases"]),
                "error_count": sum(
                    case["status"] == "error" for case in result["cases"]
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
