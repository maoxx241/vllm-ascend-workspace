#!/usr/bin/env python3
"""Generate Claude Code skill shims and the ModelScope Trae projection.

`.claude/skills/<name>/SKILL.md` shims come from `.agents/skills`. The six
`.trae/skills/modelscope` files are copied byte-for-byte from
`.agents/skills/modelscope`. Edit ModelScope only in that canonical package;
this command regenerates the Trae projection.

    python3 .agents/scripts/sync_claude_skills.py          # regenerate
    python3 .agents/scripts/sync_claude_skills.py --check  # verify, exit 1 on drift
"""
from __future__ import annotations

import sys

import argparse
import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / ".agents" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)

import yaml  # noqa: E402

AGENTS_SKILLS = ROOT / ".agents" / "skills"
CLAUDE_SKILLS = ROOT / ".claude" / "skills"
TRAE_SKILLS = ROOT / ".trae" / "skills"
MAX_SHIM_LINES = 60
MODELSCOPE_SKILL = "modelscope"
MODELSCOPE_TRAE_PATHS = (
    "SKILL.md",
    "agents/openai.yaml",
    "scripts/_modelscope_common.py",
    "scripts/download_from_modelscope.py",
    "scripts/modelscope_auto.py",
    "scripts/modelscope_download_status.py",
    "scripts/verify_modelscope_sha256.py",
)


def parse_frontmatter(source: Path) -> dict[str, str]:
    lines = source.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        return {}
    end = lines.index("---", 1)
    return yaml.safe_load("\n".join(lines[1:end]))


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
    return f"""---
name: {json.dumps(name, ensure_ascii=False)}
description: {json.dumps(description, ensure_ascii=False)}
---

<!-- Generated from .agents/skills/{skill_dir.name}/SKILL.md. Do not edit. -->

# {title}

Read `.agents/skills/{skill_dir.name}/SKILL.md` and only the references needed
for the current task. The canonical skill owns the workflow.
"""


def source_skill_dirs() -> list[Path]:
    return sorted(path for path in AGENTS_SKILLS.iterdir() if path.is_dir() and (path / "SKILL.md").exists())


def modelscope_source_dir() -> Path:
    return AGENTS_SKILLS / MODELSCOPE_SKILL


def modelscope_trae_dir() -> Path:
    return TRAE_SKILLS / MODELSCOPE_SKILL


def _exec_bits(path: Path) -> int:
    return path.stat().st_mode & 0o111


def _copy_projected_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    updated_mode = (target.stat().st_mode & ~0o111) | _exec_bits(source)
    if updated_mode != target.stat().st_mode:
        target.chmod(updated_mode)


def _projected_file_relpaths(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    relative: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative.append(path.relative_to(root).as_posix())
    return relative


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


def check_modelscope_trae() -> list[str]:
    errors: list[str] = []
    source_root = modelscope_source_dir()
    target_root = modelscope_trae_dir()
    owned = set(MODELSCOPE_TRAE_PATHS)
    for relative in MODELSCOPE_TRAE_PATHS:
        source = source_root / relative
        target = target_root / relative
        if not source.is_file():
            errors.append(f"missing canonical modelscope source: {relative}")
            continue
        if not target.is_file():
            errors.append(f"missing Trae modelscope projection: {relative}")
            continue
        if source.read_bytes() != target.read_bytes() or _exec_bits(source) != _exec_bits(target):
            errors.append(f"stale Trae modelscope projection: {relative}")
    for relative in _projected_file_relpaths(target_root):
        if relative not in owned:
            errors.append(f"unexpected file in Trae modelscope projection: {relative}")
    return errors


def check_generated() -> list[str]:
    return check_shims() + check_modelscope_trae()


def sync_shims() -> None:
    CLAUDE_SKILLS.mkdir(parents=True, exist_ok=True)
    for skill_dir in source_skill_dirs():
        target_dir = CLAUDE_SKILLS / skill_dir.name
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "SKILL.md"
        target.write_text(expected_skill_body(skill_dir), encoding="utf-8", newline="\n")
    for existing in CLAUDE_SKILLS.iterdir():
        if existing.is_dir() and not (AGENTS_SKILLS / existing.name / "SKILL.md").exists():
            shutil.rmtree(existing)


def sync_modelscope_trae() -> None:
    source_root = modelscope_source_dir()
    target_root = modelscope_trae_dir()
    for relative in MODELSCOPE_TRAE_PATHS:
        source = source_root / relative
        if not source.is_file():
            raise FileNotFoundError(f"missing canonical modelscope source: {relative}")
        _copy_projected_file(source, target_root / relative)


def sync_generated() -> None:
    sync_shims()
    sync_modelscope_trae()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Sync generated Claude Code skill shims and the ModelScope Trae "
            "projection from .agents/skills."
        )
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Only verify that .claude/skills shims and the "
            ".trae/skills/modelscope projection are synchronized."
        ),
    )
    args = parser.parse_args(argv)
    if args.check:
        errors = check_generated()
        for error in errors:
            print(error)
        return 1 if errors else 0
    sync_generated()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
