#!/usr/bin/env python3
"""Resolve the vLLM ref that the local vllm-ascend checkout tests against."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


HEX_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def read_clean(path: Path) -> str | None:
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def classify_ref(value: str) -> str:
    if HEX_RE.match(value):
        return "commit"
    if value.startswith("v") and SAFE_REF_RE.match(value):
        return "tag"
    if SAFE_REF_RE.match(value):
        return "ref"
    return "unknown"


def parse_conf_value(conf_path: Path, key: str) -> str | None:
    if not conf_path.exists():
        return None
    text = conf_path.read_text(encoding="utf-8")
    patterns = (
        rf"{re.escape(key)}\s*=\s*['\"]([^'\"]+)['\"]",
        rf"['\"]{re.escape(key)}['\"]\s*:\s*['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return None


def parse_workflow_vllm_version(workflows_dir: Path) -> tuple[str, str] | None:
    if not workflows_dir.exists():
        return None

    def clean_value(raw: str) -> str | None:
        value = raw.strip().strip("[]").strip().strip("'\"").strip()
        if "," in value:
            value = value.split(",", 1)[0].strip().strip("'\"").strip()
        if not value or value == "-" or "$" in value or not SAFE_REF_RE.match(value):
            return None
        return value

    for path in sorted(workflows_dir.glob("*.y*ml")):
        text = path.read_text(encoding="utf-8", errors="replace")
        if "vllm_version" not in text:
            continue
        lines = text.splitlines()
        for idx, line in enumerate(lines):
            if "vllm_version" not in line or ":" not in line:
                continue
            inline = clean_value(line.split(":", 1)[1])
            if inline:
                return inline, str(path)
            for follow in lines[idx + 1 : idx + 8]:
                stripped = follow.strip()
                if not stripped:
                    continue
                if not stripped.startswith("-"):
                    break
                listed = clean_value(stripped[1:])
                if listed:
                    return listed, str(path)
    return None


def resolve(vllm_ascend_dir: Path) -> dict[str, Any]:
    repo = vllm_ascend_dir.resolve()
    github_dir = repo / ".github"
    main_verified = github_dir / "vllm-main-verified.commit"
    release_tag_file = github_dir / "vllm-release-tag.commit"
    docs_conf = repo / "docs" / "source" / "conf.py"
    workflows_dir = github_dir / "workflows"

    cross_checks: dict[str, Any] = {}
    release_tag = read_clean(release_tag_file)
    if release_tag:
        cross_checks["vllm_release_tag"] = {
            "value": release_tag,
            "source": str(release_tag_file),
        }

    docs_main = parse_conf_value(docs_conf, "main_vllm_commit")
    if docs_main:
        cross_checks["docs_main_vllm_commit"] = {
            "value": docs_main,
            "source": str(docs_conf),
        }

    docs_version = parse_conf_value(docs_conf, "vllm_version")
    if docs_version:
        cross_checks["docs_vllm_version"] = {
            "value": docs_version,
            "source": str(docs_conf),
        }

    workflow_value = parse_workflow_vllm_version(workflows_dir)
    if workflow_value:
        value, source = workflow_value
        cross_checks["workflow_vllm_version"] = {
            "value": value,
            "source": source,
        }

    verified = read_clean(main_verified)
    if verified:
        return {
            "status": "ok",
            "vllm_ref": verified,
            "ref_type": classify_ref(verified),
            "source": str(main_verified),
            "precedence": "vllm-main-verified.commit",
            "cross_checks": cross_checks,
        }

    if workflow_value:
        value, source = workflow_value
        return {
            "status": "ok",
            "vllm_ref": value,
            "ref_type": classify_ref(value),
            "source": source,
            "precedence": "workflow vllm_version fallback",
            "cross_checks": cross_checks,
        }

    fallback = docs_main or docs_version
    if fallback:
        return {
            "status": "ok",
            "vllm_ref": fallback,
            "ref_type": classify_ref(fallback),
            "source": str(docs_conf),
            "precedence": "docs conf fallback",
            "cross_checks": cross_checks,
        }

    return {
        "status": "failed",
        "reason": "could not resolve a vLLM CI pin from .github/vllm-main-verified.commit, workflows, or docs/source/conf.py",
        "vllm_ascend_dir": str(repo),
        "cross_checks": cross_checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Resolve vLLM CI-pinned ref for a vllm-ascend checkout.",
        allow_abbrev=False,
    )
    parser.add_argument("--vllm-ascend-dir", default="vllm-ascend")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = resolve(Path(args.vllm_ascend_dir))
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if payload.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
