#!/usr/bin/env python3
"""Model-context helpers shared by profiling analysis stages.

The segmenter needs model knowledge before summarize/report runs.  Keep this
module lightweight and independent from ``model_insights`` so early stages can
resolve user-supplied model ids, config files, and the local fingerprint
catalog without importing the heavier shape-analysis code.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Mapping, Sequence

try:
    from .common import NormalizedEvent
    from .store import KNOWLEDGE_DIR, first_present, norm_text, text_config, to_int
except ImportError:  # pragma: no cover
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from common import NormalizedEvent  # type: ignore[no-redef]
    from store import KNOWLEDGE_DIR, first_present, norm_text, text_config, to_int  # type: ignore[no-redef]

MODEL_FINGERPRINTS_PATH = KNOWLEDGE_DIR / "model_fingerprints.json"
CONFIG_FILENAMES = ("config.json", "configuration.json")
EXTERNAL_CONFIG_TIMEOUT_S = 8
MAX_EXTERNAL_CONFIG_BYTES = 2 * 1024 * 1024


def load_model_fingerprints(path: Path = MODEL_FINGERPRINTS_PATH) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    models = payload.get("models") if isinstance(payload, Mapping) else []
    return [dict(item) for item in models or [] if isinstance(item, Mapping)]


def _catalog_names(model: Mapping[str, Any]) -> set[str]:
    names = {norm_text(model.get("model_name"))}
    names.update(norm_text(item) for item in model.get("aliases") or [])
    names.update(norm_text(item) for item in model.get("hub_ids") or [])
    return {item for item in names if item}


def _config_features(root: Mapping[str, Any], cfg: Mapping[str, Any]) -> list[str]:
    features: list[str] = []
    if to_int(first_present(cfg, "num_experts", "n_routed_experts", default=0)) > 0:
        features.append("moe")
    if to_int(first_present(cfg, "q_lora_rank", "kv_lora_rank", default=0)) > 0:
        features.append("mla")
    if isinstance(cfg.get("compress_ratios"), list):
        features.append("kv_compressor")
    layer_types = cfg.get("layer_types") if isinstance(cfg.get("layer_types"), list) else []
    if any("linear" in str(item).lower() for item in layer_types):
        features.append("linear_attention_or_mamba")
    if to_int(first_present(cfg, "index_topk", "index_n_heads", default=0)) > 0:
        features.append("dsa_or_csa_indexer")
    if isinstance(root.get("vision_config"), Mapping):
        features.append("vision")
    return list(dict.fromkeys(features))


def _config_layers(root: Mapping[str, Any]) -> int | None:
    cfg = text_config(root)
    layers = to_int(first_present(cfg, "num_hidden_layers", "n_layers", "num_layers", default=0))
    return layers if layers > 0 else None


def _config_context_fields(
    *,
    root: Mapping[str, Any],
    model_name: str,
    source: str,
    observed_features: Sequence[str],
    matched_reasons: Sequence[str],
) -> dict[str, Any]:
    cfg = text_config(root)
    layers = _config_layers(root)
    cfg_features = _config_features(root, cfg)
    return {
        "available": True,
        "model_name": model_name,
        "source": source,
        "confidence": "high",
        "expected_layers": layers,
        "segment_hints": root.get("segment_hints") if isinstance(root.get("segment_hints"), Mapping) else {},
        "features": list(dict.fromkeys([*cfg_features, *observed_features])),
        "matched_reasons": list(matched_reasons) if matched_reasons else ["config"],
    }


def _operator_profile(events: Sequence[NormalizedEvent] | None) -> dict[str, Any]:
    if not events:
        return {"categories": set(), "roles": set(), "names": set()}
    cats = {cat for event in events for cat in event.op_categories}
    roles = {role for event in events for role in event.op_roles}
    names = {normalized_name_key(event.name_raw) for event in events}
    return {
        "categories": cats,
        "roles": roles,
        "names": names,
    }


def normalized_name_key(name: str) -> str:
    text = re.sub(r"0x[0-9a-f]+", "", str(name or "").lower())
    text = re.sub(r"[0-9a-f]{16,}", "", text)
    text = re.sub(r"[^a-z0-9_]+", "", text)
    return text[:96] or "unknown"


def _features_from_operator_profile(profile: Mapping[str, Any]) -> list[str]:
    cats = set(profile.get("categories") or [])
    roles = set(profile.get("roles") or [])
    features: list[str] = []
    if "moe" in roles or any(cat.startswith("moe.") for cat in cats):
        features.append("moe")
    if any(cat.startswith("attention.mla") for cat in cats):
        features.append("mla")
    if "attention.kv_compressor" in cats:
        features.append("kv_compressor")
    if "attention.lightning_indexer" in cats:
        features.append("dsa_or_csa_indexer")
    if "attention.sparse_sharedkv" in cats:
        features.append("sparse_sharedkv")
    if "attention.csa.compressor" in cats:
        features.append("csa")
    if "attention.flash_score" in cats:
        features.append("dense_flash_attention")
    if "attention.linear_or_mamba" in cats:
        features.append("linear_attention_or_mamba")
    if "attention.rope" in cats or "attention.rope.partial" in cats:
        features.append("rope")
    has_compressor = "attention.kv_compressor" in cats
    has_indexer = "attention.lightning_indexer" in cats
    has_sparse_sharedkv = "attention.sparse_sharedkv" in cats
    has_flash = "attention.flash_score" in cats
    if has_compressor and has_indexer and has_sparse_sharedkv:
        features.append("csa")
    elif has_compressor and has_flash and not has_indexer and not has_sparse_sharedkv:
        features.append("hca")
    elif has_indexer and has_sparse_sharedkv and not has_compressor:
        features.append("dsa")
    return list(dict.fromkeys(features))


def _event_features(events: Sequence[NormalizedEvent] | None) -> list[str]:
    return _features_from_operator_profile(_operator_profile(events))


def _alias_is_substring_hit(name: str, target: str, raw_target: str) -> bool:
    """Guard the ``name in target`` fuzzy direction.

    Short normalized aliases such as ``dsa`` or ``gdn`` would otherwise
    attach to any id that happens to contain those letters.  Require a
    minimum alias length for plain substring hits; shorter aliases only
    count when they match a whole token of the raw (un-normalized) id.
    """
    if name not in target:
        return False
    if len(name) >= 6:
        return True
    return name in re.findall(r"[a-z0-9]+", raw_target.lower())


def _catalog_matches_by_model_id(
    model_id: str | None,
    models: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], str]:
    """Return ``(matches, kind)`` where kind is ``exact`` or ``fuzzy``.

    Callers must treat fuzzy hits as weaker evidence: a single fuzzy hit is
    not a high-confidence identification.
    """
    if not model_id:
        return [], "none"
    raw_target = str(model_id)
    target = norm_text(model_id)
    if not target:
        return [], "none"
    exact: list[Mapping[str, Any]] = []
    fuzzy: list[Mapping[str, Any]] = []
    for model in models:
        names = _catalog_names(model)
        if target in names:
            exact.append(model)
            continue
        if any(target in name for name in names) or any(
            _alias_is_substring_hit(name, target, raw_target) for name in names
        ):
            fuzzy.append(model)
    if exact:
        return exact, "exact"
    return fuzzy, "fuzzy"


def _catalog_lookup(models: Sequence[Mapping[str, Any]], value: Any) -> Mapping[str, Any] | None:
    target = norm_text(value)
    if not target:
        return None
    for model in models:
        if target in _catalog_names(model):
            return model
    return None


def _dedupe_models(models: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for model in models:
        key = norm_text(model.get("model_name")) or json.dumps(model, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(dict(model))
    return out


def _expand_catalog_variants(
    matches: Sequence[Mapping[str, Any]],
    all_models: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    expanded: list[Mapping[str, Any]] = []
    for model in matches:
        variants = model.get("variants") if isinstance(model.get("variants"), list) else []
        if not variants:
            expanded.append(model)
            continue
        for item in variants:
            if isinstance(item, Mapping):
                expanded.append(item)
                continue
            found = _catalog_lookup(all_models, item)
            if found is not None:
                expanded.append(found)
                continue
            text = str(item or "").strip()
            if text:
                expanded.append(
                    {
                        "model_name": text.rsplit("/", 1)[-1],
                        "hub_ids": [text] if "/" in text else [],
                        "expected_layers": None,
                        "features": model.get("features") or [],
                        "field_hints": {},
                    }
                )
    return _dedupe_models(expanded)


def _match_catalog_by_features(
    observed_features: Sequence[str],
    models: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any] | None, float, list[str]]:
    observed = set(observed_features)
    best: tuple[Mapping[str, Any] | None, float, list[str]] = (None, 0.0, [])
    if not observed:
        return best
    for model in models:
        expected = set(str(item) for item in model.get("features") or [])
        if not expected:
            continue
        matched = sorted(observed & expected)
        score = len(matched) / max(len(expected), 1)
        if score > best[1]:
            best = (model, score, matched)
    return best


SPECIFIC_STRUCTURE_FEATURES = {
    "kv_compressor",
    "dsa_or_csa_indexer",
    "sparse_sharedkv",
    "csa",
    "hca",
    "dsa",
    "mla",
    "linear_attention_or_mamba",
}


def _feature_match_is_specific(matched_features: Sequence[str]) -> bool:
    matched = set(matched_features)
    return len(matched) >= 2 or bool(matched & SPECIFIC_STRUCTURE_FEATURES)


def _catalog_matches_by_structure_features(
    observed_features: Sequence[str],
    models: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], float, list[str]]:
    observed = set(observed_features)
    if not observed or not _feature_match_is_specific(observed):
        return [], 0.0, []
    scored: list[tuple[float, int, Mapping[str, Any], list[str]]] = []
    for model in models:
        expected = set(str(item) for item in model.get("features") or [])
        if not expected:
            continue
        matched = sorted(observed & expected)
        if not matched or not _feature_match_is_specific(matched):
            continue
        score = len(matched) / max(len(expected), 1)
        # Prefer fuzzy family entries over concrete variants when the user
        # supplied an architecture description rather than a model name.
        family_bonus = 1 if isinstance(model.get("variants"), list) and model.get("variants") else 0
        scored.append((score, family_bonus, model, matched))
    if not scored:
        return [], 0.0, []
    scored.sort(key=lambda item: (item[0], item[1], str(item[2].get("model_name") or "")), reverse=True)
    best_score = scored[0][0]
    best_family_bonus = scored[0][1]
    best = [item for item in scored if item[0] == best_score and item[1] == best_family_bonus]
    matched_features: list[str] = []
    for _score, _bonus, _model, features in best:
        matched_features.extend(features)
    return [item[2] for item in best], best_score, sorted(set(matched_features))


STRUCTURE_HINT_FEATURE_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("moe", ("moe",)),
    ("gatingtopk", ("moe",)),
    ("topkgating", ("moe",)),
    ("mla", ("mla",)),
    ("dsa", ("dsa", "dsa_or_csa_indexer", "sparse_sharedkv")),
    ("csa", ("csa", "kv_compressor", "dsa_or_csa_indexer", "sparse_sharedkv")),
    ("hca", ("hca", "kv_compressor", "dense_flash_attention")),
    ("compressor", ("kv_compressor",)),
    ("kvcompressor", ("kv_compressor",)),
    ("lightningindexer", ("dsa_or_csa_indexer",)),
    ("indexer", ("dsa_or_csa_indexer",)),
    ("sparsesharedkv", ("sparse_sharedkv",)),
    ("sparseattention", ("sparse_sharedkv",)),
    ("flashattention", ("dense_flash_attention",)),
    ("fullattention", ("dense_flash_attention",)),
    ("linearattention", ("linear_attention_or_mamba",)),
    ("mamba", ("linear_attention_or_mamba",)),
    ("gdn", ("linear_attention_or_mamba",)),
    ("rope", ("rope",)),
)


def _user_structure_features(text: str | None) -> list[str]:
    normalized = norm_text(text)
    if not normalized:
        return []
    features: list[str] = []
    for token, values in STRUCTURE_HINT_FEATURE_ALIASES:
        if token in normalized:
            features.extend(values)
    return list(dict.fromkeys(features))


def _category_values(rule: Mapping[str, Any], *keys: str) -> set[str]:
    values: set[str] = set()
    for key in keys:
        raw = rule.get(key)
        if isinstance(raw, str):
            values.add(raw)
        elif isinstance(raw, (list, tuple, set)):
            values.update(str(item) for item in raw if item)
    return values


def _operator_match_rule(model: Mapping[str, Any]) -> Mapping[str, Any]:
    rule = model.get("operator_match")
    return rule if isinstance(rule, Mapping) else {}


def _score_operator_match(
    model: Mapping[str, Any],
    profile: Mapping[str, Any],
) -> tuple[float, list[str]]:
    rule = _operator_match_rule(model)
    if not rule:
        return 0.0, []
    categories = set(profile.get("categories") or [])
    required_all = _category_values(rule, "required_all", "required_categories")
    required_any = _category_values(rule, "required_any", "any_categories")
    forbidden = _category_values(rule, "forbidden", "forbidden_categories")
    optional = _category_values(rule, "optional", "optional_categories")
    if forbidden & categories:
        return 0.0, []
    if required_all and not required_all.issubset(categories):
        return 0.0, []
    if required_any and not (required_any & categories):
        return 0.0, []
    matched_required = sorted((required_all | required_any) & categories)
    matched_optional = sorted(optional & categories)
    if not matched_required and not matched_optional:
        return 0.0, []
    weights = rule.get("weights") if isinstance(rule.get("weights"), Mapping) else {}
    score = 0.0
    for category in matched_required:
        score += float(weights.get(category, 5.0))
    for category in matched_optional:
        score += float(weights.get(category, 1.0))
    score += float(rule.get("base_score", 0.0) or 0.0)
    reasons = [f"operator:{category}" for category in matched_required]
    reasons.extend(f"operator_optional:{category}" for category in matched_optional)
    return score, reasons


def _match_catalog_by_operator_profile(
    profile: Mapping[str, Any],
    models: Sequence[Mapping[str, Any]],
) -> tuple[list[Mapping[str, Any]], float, list[str]]:
    scored: list[tuple[float, Mapping[str, Any], list[str]]] = []
    for model in models:
        score, reasons = _score_operator_match(model, profile)
        if score > 0:
            scored.append((score, model, reasons))
    if not scored:
        return [], 0.0, []
    scored.sort(key=lambda item: (item[0], str(item[1].get("model_name") or "")), reverse=True)
    best_score = scored[0][0]
    best = [item for item in scored if item[0] == best_score]
    reasons: list[str] = []
    for _score, _model, item_reasons in best:
        reasons.extend(item_reasons)
    return [item[1] for item in best], best_score, list(dict.fromkeys(reasons))


def _generic_operator_context(
    *,
    observed_features: Sequence[str],
    profile: Mapping[str, Any],
    source: str = "profile_operator_fingerprint:generic",
    reason: str = "operator:moe.gating",
) -> dict[str, Any] | None:
    categories = set(profile.get("categories") or [])
    if "moe" in observed_features and "moe.gating" in categories:
        return {
            "available": True,
            "model_name": "MoE architecture",
            "source": source,
            "confidence": "low",
            "expected_layers": None,
            "segment_hints": {},
            "features": list(dict.fromkeys(observed_features)),
            "matched_reasons": [reason],
            "limitations": [
                "MoE gating proves an MoE architecture family, but not a concrete model or layer count by itself"
            ],
        }
    return None


def _generic_structure_context(
    observed_features: Sequence[str],
    *,
    source: str,
    reason: str,
) -> dict[str, Any] | None:
    if set(observed_features) == {"moe"}:
        return {
            "available": True,
            "model_name": "MoE architecture",
            "source": source,
            "confidence": "low",
            "expected_layers": None,
            "segment_hints": {},
            "features": ["moe"],
            "matched_reasons": [reason],
            "limitations": [
                "MoE structure alone narrows the architecture family but does not prove a concrete model or layer count"
            ],
        }
    return None


def _append_unique(values: list[str], value: Any) -> None:
    text = str(value or "").strip()
    if not text or text in values:
        return
    values.append(text)


# HF/ModelScope repo ids are exactly ``org/name`` with no whitespace; catalog
# family display names ("DeepSeek DSA sparse-attention family") must never be
# turned into real external config requests.
_HF_REPO_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
_HF_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _is_probable_repo_id(value: Any) -> bool:
    return bool(_HF_REPO_ID_RE.match(str(value or "").strip()))


def _is_probable_repo_name(value: Any) -> bool:
    return bool(_HF_REPO_NAME_RE.match(str(value or "").strip()))


def _repo_id_candidates(model_id: str | None, matched: Mapping[str, Any] | None = None) -> list[str]:
    candidates: list[str] = []

    def add(candidate: Any) -> None:
        text = str(candidate or "").strip()
        if _is_probable_repo_id(text):
            _append_unique(candidates, text)

    if matched is not None:
        for item in matched.get("hub_ids") or []:
            add(item)
        for item in matched.get("aliases") or []:
            add(item)
    if model_id:
        add(model_id)
        if "/" not in model_id:
            normalized = norm_text(model_id)
            if "deepseek" in normalized or normalized.startswith("dsv"):
                add(f"deepseek-ai/{model_id}")
            if "qwen" in normalized:
                add(f"Qwen/{model_id}")
            if "glm" in normalized or "zai" in normalized:
                add(f"zai-org/{model_id}")
                add(f"THUDM/{model_id}")
    return candidates


def _read_url_text(url: str, timeout_s: int = EXTERNAL_CONFIG_TIMEOUT_S) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ascend-profiling-analysis/1.0",
            "Accept": "application/json,text/plain,*/*",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.read(MAX_EXTERNAL_CONFIG_BYTES + 1).decode("utf-8")
    except Exception as urllib_error:
        try:
            completed = subprocess.run(
                [
                    "curl",
                    "-LfsS",
                    "--max-time",
                    str(timeout_s),
                    url,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_s + 3,
            )
        except Exception as curl_error:
            raise RuntimeError(f"urllib={urllib_error}; curl={curl_error}") from curl_error
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(f"urllib={urllib_error}; curl_exit={completed.returncode}: {detail[:200]}")
        return completed.stdout[: MAX_EXTERNAL_CONFIG_BYTES + 1]


def _json_from_url(url: str) -> Mapping[str, Any]:
    text = _read_url_text(url)
    if len(text.encode("utf-8")) > MAX_EXTERNAL_CONFIG_BYTES:
        raise RuntimeError("config payload exceeded size limit")
    payload = json.loads(text)
    if not isinstance(payload, Mapping):
        raise RuntimeError("config payload is not a JSON object")
    return payload


def _hf_search_model_ids(query: str, limit: int = 5) -> list[str]:
    url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
        {"search": query, "limit": str(limit), "full": "false"}
    )
    try:
        payload = json.loads(_read_url_text(url))
    except Exception:
        return []
    if not isinstance(payload, list):
        return []
    target = norm_text(query)
    results: list[str] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        repo_id = str(item.get("modelId") or item.get("id") or "").strip()
        if not repo_id:
            continue
        # Search results are allowed only when the repository basename is an
        # exact normalized match.  Suffix variants such as "-0324" must be
        # supplied by the user or the catalog; otherwise layer count would be a
        # repository-choice guess rather than config evidence.
        if norm_text(repo_id.rsplit("/", 1)[-1]) == target:
            _append_unique(results, repo_id)
    return results


def _config_urls_for_repo(repo_id: str) -> list[tuple[str, str, str]]:
    quoted_repo = urllib.parse.quote(repo_id.strip("/"), safe="/")
    urls: list[tuple[str, str, str]] = []
    for filename in CONFIG_FILENAMES:
        quoted_file = urllib.parse.quote(filename, safe="/")
        urls.append(
            (
                "huggingface",
                filename,
                f"https://huggingface.co/{quoted_repo}/resolve/main/{quoted_file}",
            )
        )
        urls.append(
            (
                "modelscope",
                filename,
                f"https://modelscope.cn/models/{quoted_repo}/resolve/master/{quoted_file}",
            )
        )
    return urls


def fetch_external_model_config(
    model_id: str | None,
    matched: Mapping[str, Any] | None = None,
) -> tuple[Mapping[str, Any] | None, dict[str, Any]]:
    """Fetch config.json for an exact known model id from HF/ModelScope.

    This is evidence collection, not model inference.  If no accessible config
    reports a layer count, callers must keep ``expected_layers`` unknown.
    """

    repo_ids = _repo_id_candidates(model_id, matched)
    if model_id and "/" not in model_id and _is_probable_repo_name(model_id):
        for repo_id in _hf_search_model_ids(model_id):
            _append_unique(repo_ids, repo_id)
    attempts: list[dict[str, Any]] = []
    for repo_id in repo_ids:
        for provider, filename, url in _config_urls_for_repo(repo_id):
            attempt = {"provider": provider, "repo_id": repo_id, "filename": filename, "url": url}
            try:
                payload = _json_from_url(url)
            except Exception as exc:
                attempt.update({"status": "failed", "error": str(exc)[:240]})
                attempts.append(attempt)
                continue
            layers = _config_layers(payload)
            attempt.update({"status": "ok", "expected_layers": layers})
            attempts.append(attempt)
            if layers is not None:
                return payload, {
                    "status": "ok",
                    "provider": provider,
                    "repo_id": repo_id,
                    "filename": filename,
                    "url": url,
                    "expected_layers": layers,
                    "attempts": attempts,
                }
    return None, {"status": "not_found", "attempts": attempts}


def _catalog_context_fields(
    *,
    model: Mapping[str, Any],
    observed_features: Sequence[str],
    source: str,
    matched_reasons: Sequence[str],
) -> dict[str, Any]:
    segment_hints = model.get("segment_hints") if isinstance(model.get("segment_hints"), Mapping) else {}
    return {
        "available": True,
        "model_name": model.get("model_name"),
        "source": source,
        "confidence": "high",
        "expected_layers": model.get("expected_layers"),
        "segment_hints": dict(segment_hints),
        "features": list(dict.fromkeys([*(model.get("features") or []), *observed_features])),
        "field_hints": dict(model.get("field_hints") or {}) if isinstance(model.get("field_hints"), Mapping) else {},
        "hub_ids": list(model.get("hub_ids") or []),
        "matched_reasons": list(matched_reasons),
    }


def _context_with_external_config(
    *,
    context: dict[str, Any],
    model_id: str | None,
    model: Mapping[str, Any],
    observed_features: Sequence[str],
    force_fetch: bool = False,
) -> dict[str, Any]:
    if context.get("expected_layers") is not None and not force_fetch:
        return context
    external_config, config_resolution = fetch_external_model_config(model_id, model)
    context["config_resolution"] = config_resolution
    if external_config is None:
        if context.get("expected_layers") is None:
            context.setdefault("limitations", []).append(
                "known model candidate has no catalog layer count and no accessible config.json on Hugging Face or ModelScope"
            )
        return context

    cfg_fields = _config_context_fields(
        root=external_config,
        model_name=str(model.get("model_name") or model_id or "external_config_model"),
        source=f"external_model_config:{config_resolution.get('provider')}:{config_resolution.get('url')}",
        observed_features=observed_features,
        matched_reasons=[*(context.get("matched_reasons") or []), "external_config.num_hidden_layers"],
    )
    # Preserve catalog hints that are not present in a vanilla config.json.
    cfg_fields["features"] = list(dict.fromkeys([*(context.get("features") or []), *cfg_fields.get("features", [])]))
    cfg_fields["segment_hints"] = context.get("segment_hints") or cfg_fields.get("segment_hints") or {}
    cfg_fields["field_hints"] = context.get("field_hints") or {}
    cfg_fields["hub_ids"] = context.get("hub_ids") or []
    context.update(cfg_fields)
    return context


def _resolve_catalog_model_contexts(
    *,
    model_id: str,
    matches: Sequence[Mapping[str, Any]],
    all_models: Sequence[Mapping[str, Any]],
    observed_features: Sequence[str],
    source: str = "model_fingerprint_catalog:model_id",
    matched_reasons: Sequence[str] | None = None,
) -> list[dict[str, Any]]:
    expanded = _expand_catalog_variants(matches, all_models)
    contexts: list[dict[str, Any]] = []
    force_fetch = len(expanded) > 1
    reasons = list(matched_reasons or [f"model_id={model_id}"])
    for model in expanded:
        context = _catalog_context_fields(
            model=model,
            observed_features=observed_features,
            source=source,
            matched_reasons=reasons,
        )
        contexts.append(
            _context_with_external_config(
                context=context,
                model_id=str((model.get("hub_ids") or [model.get("model_name") or model_id])[0]),
                model=model,
                observed_features=observed_features,
                force_fetch=force_fetch and context.get("expected_layers") is None,
            )
        )
    return contexts


def _single_or_family_context(
    *,
    model_id: str,
    matches: Sequence[Mapping[str, Any]],
    candidate_contexts: Sequence[dict[str, Any]],
    observed_features: Sequence[str],
    source: str = "model_fingerprint_catalog:model_family_variants",
    matched_reasons: Sequence[str] | None = None,
    confidence: str = "medium",
) -> dict[str, Any]:
    if len(candidate_contexts) == 1:
        return dict(candidate_contexts[0])
    model_names = [str(item.get("model_name") or "") for item in candidate_contexts if item.get("model_name")]
    expected_layers = sorted(
        {
            layers
            for layers in (to_int(item.get("expected_layers"), default=0) for item in candidate_contexts)
            if layers > 0
        }
    )
    visible_counts = sorted(
        {
            int(value)
            for item in candidate_contexts
            if isinstance(item.get("segment_hints"), Mapping)
            for value in item.get("segment_hints", {}).get("profile_visible_layer_counts", [])
            if isinstance(value, int)
        }
    )
    family_name = str(matches[0].get("model_name") or model_id) if matches else model_id
    limitations = [
        "model id resolved to a model family; layer count must be selected from enumerated candidates using profile evidence"
    ]
    family_features = list(matches[0].get("features") or []) if matches else []
    reasons = list(matched_reasons or [f"model_id={model_id}", "family_variant_enumeration"])
    if "family_variant_enumeration" not in reasons:
        reasons.append("family_variant_enumeration")
    return {
        "available": True,
        "model_id": model_id,
        "model_name": family_name,
        "source": source,
        "confidence": confidence,
        "expected_layers": None,
        "segment_hints": {"profile_visible_layer_counts": visible_counts} if visible_counts else {},
        "features": list(dict.fromkeys([*family_features, *observed_features])),
        "matched_reasons": reasons,
        "candidate_model_contexts": list(candidate_contexts),
        "candidate_model_names": model_names,
        "candidate_expected_layers": expected_layers,
        "limitations": limitations,
    }


def resolve_model_context(
    *,
    model_id: str | None = None,
    model_config: Path | None = None,
    events: Sequence[NormalizedEvent] | None = None,
    fingerprint_path: Path = MODEL_FINGERPRINTS_PATH,
) -> dict[str, Any]:
    """Resolve model context for early pipeline stages.

    Priority is explicit config > explicit model/catalog id > user-supplied
    structure hint > profiling operator fingerprint > conservative feature
    fallback.  The feature-only paths are intentionally conservative because
    they can identify a family but may not prove an exact SKU.
    """

    models = load_model_fingerprints(fingerprint_path)
    operator_profile = _operator_profile(events)
    observed_features = _features_from_operator_profile(operator_profile)
    context: dict[str, Any] = {
        "available": False,
        "model_id": model_id,
        "model_name": None,
        "source": "none",
        "confidence": "unknown",
        "expected_layers": None,
        "segment_hints": {},
        "features": observed_features,
        "matched_reasons": [],
    }

    if model_config is not None and model_config.is_file():
        try:
            root = json.loads(model_config.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            context["matched_reasons"] = [f"config_parse_error:{model_config}"]
            context["limitations"] = [f"--model-config is not valid JSON ({model_config}): {exc}"]
            return context
        if not isinstance(root, Mapping):
            context["matched_reasons"] = [f"config_parse_error:{model_config}"]
            context["limitations"] = [f"--model-config is not a JSON object ({model_config})"]
            return context
        context.update(
            _config_context_fields(
                root=root,
                model_name=model_id or model_config.parent.name or str(first_present(text_config(root), "model_type", default="config_model")),
                source=str(model_config),
                observed_features=observed_features,
                matched_reasons=["config.num_hidden_layers"] if _config_layers(root) else ["config"],
            )
        )
        return context

    if model_id:
        matches, match_kind = _catalog_matches_by_model_id(model_id, models)
        if matches:
            candidate_contexts = _resolve_catalog_model_contexts(
                model_id=model_id,
                matches=matches,
                all_models=models,
                observed_features=observed_features,
            )
            resolved = _single_or_family_context(
                model_id=model_id,
                matches=matches,
                candidate_contexts=candidate_contexts,
                observed_features=observed_features,
            )
            resolved["model_id"] = model_id
            if match_kind == "fuzzy":
                # A fuzzy substring hit is weaker evidence than an exact
                # catalog-name hit; never report it as high confidence.
                if resolved.get("confidence") == "high":
                    resolved["confidence"] = "medium"
                reasons = resolved.setdefault("matched_reasons", [])
                if isinstance(reasons, list) and "fuzzy_model_id_match" not in reasons:
                    reasons.append("fuzzy_model_id_match")
            return resolved

        structure_features = _user_structure_features(model_id)
        structure_matches, structure_score, matched_features = _catalog_matches_by_structure_features(structure_features, models)
        if structure_matches:
            combined_features = list(dict.fromkeys([*structure_features, *observed_features]))
            reasons = [f"user_structure_features={','.join(matched_features)}"]
            candidate_contexts = _resolve_catalog_model_contexts(
                model_id=model_id,
                matches=structure_matches,
                all_models=models,
                observed_features=combined_features,
                source="model_fingerprint_catalog:user_structure",
                matched_reasons=reasons,
            )
            resolved = _single_or_family_context(
                model_id=model_id,
                matches=structure_matches,
                candidate_contexts=candidate_contexts,
                observed_features=combined_features,
                source="model_fingerprint_catalog:user_structure_family_variants",
                matched_reasons=reasons,
                confidence="high" if structure_score >= 0.75 else "medium",
            )
            if len(candidate_contexts) == 1:
                resolved["source"] = "model_fingerprint_catalog:user_structure"
                resolved["confidence"] = "high" if structure_score >= 0.75 else "medium"
                resolved["matched_reasons"] = reasons
            resolved["model_id"] = model_id
            resolved["structure_match_score"] = structure_score
            return resolved
        generic_structure = _generic_structure_context(
            structure_features,
            source="model_fingerprint_catalog:user_structure_generic",
            reason=f"user_structure_features={','.join(structure_features)}",
        )
        if generic_structure is not None:
            generic_structure["model_id"] = model_id
            return generic_structure

    if model_id:
        external_config, config_resolution = fetch_external_model_config(model_id, None)
        if external_config is not None:
            context.update(
                _config_context_fields(
                    root=external_config,
                    model_name=model_id,
                    source=f"external_model_config:{config_resolution.get('provider')}:{config_resolution.get('url')}",
                    observed_features=observed_features,
                    matched_reasons=[f"model_id={model_id}", "external_config.num_hidden_layers"],
                )
            )
            context["config_resolution"] = config_resolution
            return context
        context["config_resolution"] = config_resolution

    operator_matches, operator_score, operator_reasons = _match_catalog_by_operator_profile(operator_profile, models)
    if operator_matches:
        candidate_contexts = _resolve_catalog_model_contexts(
            model_id="profile_operator_fingerprint",
            matches=operator_matches,
            all_models=models,
            observed_features=observed_features,
            source="model_fingerprint_catalog:profile_operator",
            matched_reasons=operator_reasons,
        )
        resolved = _single_or_family_context(
            model_id="profile_operator_fingerprint",
            matches=operator_matches,
            candidate_contexts=candidate_contexts,
            observed_features=observed_features,
            source="profile_operator_fingerprint:operator_match",
            matched_reasons=operator_reasons,
            confidence="high" if operator_score >= 8.0 else "medium",
        )
        if len(candidate_contexts) == 1:
            resolved["source"] = "profile_operator_fingerprint:operator_match"
            resolved["confidence"] = "high" if operator_score >= 8.0 else "medium"
            resolved["matched_reasons"] = operator_reasons
        resolved["operator_match_score"] = operator_score
        return resolved

    generic_operator = _generic_operator_context(observed_features=observed_features, profile=operator_profile)
    if generic_operator is not None:
        return generic_operator

    feature_match, score, matched_features = _match_catalog_by_features(observed_features, models)
    if feature_match is not None and score >= 0.5 and _feature_match_is_specific(matched_features):
        context.update(
            {
                "available": True,
                "model_name": feature_match.get("model_name"),
                "source": "model_fingerprint_catalog:profile_features",
                "confidence": "medium" if score >= 0.75 else "low",
                "expected_layers": feature_match.get("expected_layers"),
                "segment_hints": feature_match.get("segment_hints") if isinstance(feature_match.get("segment_hints"), Mapping) else {},
                "features": list(dict.fromkeys([*(feature_match.get("features") or []), *observed_features])),
                "matched_reasons": ["features=" + ",".join(matched_features)],
            }
        )
    return context
