#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AGENTS_SKILLS = ROOT / ".agents" / "skills"
CLAUDE_SKILLS = ROOT / ".claude" / "skills"
MAX_SHIM_LINES = 60


def parse_frontmatter(source: Path) -> dict[str, str]:
    lines = source.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        return {}
    data: dict[str, str] = {}
    for line in lines[1:]:
        if line == "---":
            break
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip()
    return data


def first_markdown_heading(source: Path, default: str) -> str:
    for line in source.read_text(encoding="utf-8").splitlines():
        if line.startswith("# "):
            return line.removeprefix("# ").strip() or default
    return default


def expected_skill_body(skill_dir: Path) -> str:
    source = skill_dir / "SKILL.md"
    frontmatter = parse_frontmatter(source)
    name = frontmatter.get("name") or skill_dir.name
    description = frontmatter.get("description") or first_markdown_heading(source, skill_dir.name)
    title = first_markdown_heading(source, name)
    return f"""<!-- Generated Claude Code shim from .agents/skills/{skill_dir.name}/SKILL.md. Do not edit. -->
---
name: {name}
description: {description}
---

# {title}

Canonical skill source:

`.agents/skills/{skill_dir.name}/SKILL.md`

Before using this skill:

1. Read the canonical skill file above.
2. Follow its routing rules, entrypoints, guardrails, and acceptance criteria.
3. Use `.remote-dev` companion tools for ordinary remote endpoint read/edit/bash/search/patch work.
4. Use this Claude project skill only for the domain workflow described by the canonical source.
"""


def source_skill_dirs() -> list[Path]:
    return sorted(path for path in AGENTS_SKILLS.iterdir() if path.is_dir() and (path / "SKILL.md").exists())


def check_shims() -> list[str]:
    errors: list[str] = []
    expected_names = {path.name for path in source_skill_dirs()}
    observed_names = {path.name for path in CLAUDE_SKILLS.iterdir() if path.is_dir()} if CLAUDE_SKILLS.exists() else set()
    for missing in sorted(expected_names - observed_names):
        errors.append(f"missing Claude skill shim: {missing}")
    for extra in sorted(observed_names - expected_names):
        errors.append(f"extra Claude skill shim: {extra}")
    for name in sorted(observed_names & expected_names):
        for path in sorted((CLAUDE_SKILLS / name).iterdir()):
            if path.name != "SKILL.md":
                errors.append(f"unexpected file in Claude skill shim {name}: {path.name}")
    for skill_dir in source_skill_dirs():
        target = CLAUDE_SKILLS / skill_dir.name / "SKILL.md"
        if not target.exists():
            continue
        expected = expected_skill_body(skill_dir)
        observed = target.read_text(encoding="utf-8")
        if observed != expected:
            errors.append(f"stale Claude skill shim: {skill_dir.name}")
        if len(observed.splitlines()) > MAX_SHIM_LINES:
            errors.append(f"Claude skill shim is too large: {skill_dir.name}")
    return errors


def sync_shims() -> None:
    CLAUDE_SKILLS.mkdir(parents=True, exist_ok=True)
    for skill_dir in source_skill_dirs():
        target_dir = CLAUDE_SKILLS / skill_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "SKILL.md"
        target.write_text(expected_skill_body(skill_dir), encoding="utf-8")
    for existing in CLAUDE_SKILLS.iterdir():
        if existing.is_dir() and not (AGENTS_SKILLS / existing.name / "SKILL.md").exists():
            shutil.rmtree(existing)


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync generated Claude Code skill shims from .agents/skills.")
    parser.add_argument("--check", action="store_true", help="Only verify that .claude/skills is synchronized.")
    args = parser.parse_args()
    if args.check:
        errors = check_shims()
        for error in errors:
            print(error)
        return 1 if errors else 0
    sync_shims()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
