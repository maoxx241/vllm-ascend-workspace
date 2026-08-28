#!/usr/bin/env python3
"""Validate the repository-local skill catalog and package basics."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
README_SKILL_RE = re.compile(r"^\|\s*\*\*([a-z0-9-]+)\*\*\s*\|", re.MULTILINE)
AGENTS_SKILL_RE = re.compile(r"^\|\s*`([a-z0-9-]+)`\s*\|", re.MULTILINE)
AGENTS_README_SKILL_RE = re.compile(
    r"^-\s+`\.agents/skills/([a-z0-9-]+)/`", re.MULTILINE
)


@dataclass(frozen=True)
class SkillRecord:
    name: str
    description: str
    path: str
    body_lines: int
    resources: tuple[str, ...]


@dataclass(frozen=True)
class Finding:
    code: str
    path: str
    message: str


class CatalogError(RuntimeError):
    """Raised when a SKILL.md cannot be parsed."""


def _parse_scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def parse_skill(skill_file: Path, repo_root: Path) -> SkillRecord:
    text = skill_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise CatalogError("missing opening YAML frontmatter delimiter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise CatalogError("missing closing YAML frontmatter delimiter") from exc

    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise CatalogError(f"invalid frontmatter line: {line!r}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = _parse_scalar(value)

    unsupported = sorted(set(metadata) - {"name", "description"})
    if unsupported:
        raise CatalogError(f"unsupported frontmatter fields: {', '.join(unsupported)}")
    for required in ("name", "description"):
        if not metadata.get(required):
            raise CatalogError(f"missing required frontmatter field: {required}")

    resources = tuple(
        name
        for name in ("agents", "scripts", "references", "assets", "tests")
        if (skill_file.parent / name).exists()
    )
    return SkillRecord(
        name=metadata["name"],
        description=metadata["description"],
        path=skill_file.parent.relative_to(repo_root).as_posix(),
        body_lines=max(0, len(lines) - end - 1),
        resources=resources,
    )


def discover_skills(repo_root: Path) -> tuple[list[SkillRecord], list[Finding]]:
    skills_root = repo_root / ".agents" / "skills"
    findings: list[Finding] = []
    records: list[SkillRecord] = []
    if not skills_root.is_dir():
        return [], [
            Finding(
                "skills-root-missing",
                ".agents/skills",
                "repository-local skills directory does not exist",
            )
        ]

    for skill_dir in sorted(path for path in skills_root.iterdir() if path.is_dir()):
        skill_file = skill_dir / "SKILL.md"
        relative = skill_file.relative_to(repo_root).as_posix()
        if not skill_file.is_file():
            findings.append(Finding("skill-file-missing", relative, "SKILL.md does not exist"))
            continue
        try:
            record = parse_skill(skill_file, repo_root)
        except CatalogError as exc:
            findings.append(Finding("frontmatter-invalid", relative, str(exc)))
            continue
        records.append(record)
        if record.name != skill_dir.name:
            findings.append(
                Finding(
                    "name-mismatch",
                    relative,
                    f"frontmatter name {record.name!r} does not match directory {skill_dir.name!r}",
                )
            )
        if not SKILL_NAME_RE.fullmatch(record.name):
            findings.append(
                Finding(
                    "name-invalid",
                    relative,
                    "skill name must contain only lowercase letters, digits, and hyphens",
                )
            )
        if "<" in record.description or ">" in record.description:
            findings.append(
                Finding(
                    "description-invalid",
                    relative,
                    "frontmatter description must not contain angle brackets",
                )
            )
        if record.body_lines > 500:
            findings.append(
                Finding(
                    "body-too-long",
                    relative,
                    f"SKILL.md body has {record.body_lines} lines; keep it at or below 500",
                )
            )
        findings.extend(validate_markdown_links(skill_file, repo_root))

    duplicate_names: dict[str, list[str]] = {}
    for record in records:
        duplicate_names.setdefault(record.name, []).append(record.path)
    for name, paths in sorted(duplicate_names.items()):
        if len(paths) > 1:
            findings.append(
                Finding(
                    "duplicate-name",
                    ".agents/skills",
                    f"{name!r} is declared by: {', '.join(paths)}",
                )
            )
    return records, findings


def validate_markdown_links(skill_file: Path, repo_root: Path) -> list[Finding]:
    findings: list[Finding] = []
    text = skill_file.read_text(encoding="utf-8")
    for target in MARKDOWN_LINK_RE.findall(text):
        clean_target = target.split("#", 1)[0].strip()
        if not clean_target or "://" in clean_target or clean_target.startswith("mailto:"):
            continue
        candidate = (skill_file.parent / clean_target).resolve()
        try:
            candidate.relative_to(repo_root.resolve())
        except ValueError:
            findings.append(
                Finding(
                    "link-outside-repo",
                    skill_file.relative_to(repo_root).as_posix(),
                    f"local link escapes the repository: {target}",
                )
            )
            continue
        if not candidate.exists():
            findings.append(
                Finding(
                    "link-missing",
                    skill_file.relative_to(repo_root).as_posix(),
                    f"local link target does not exist: {target}",
                )
            )
    return findings


def _extract_catalog_names(path: Path, pattern: re.Pattern[str]) -> set[str]:
    if not path.is_file():
        return set()
    return set(pattern.findall(path.read_text(encoding="utf-8")))


def validate_document_catalogs(
    repo_root: Path, records: Iterable[SkillRecord]
) -> list[Finding]:
    expected = {record.name for record in records}
    documents = (
        ("README.md", README_SKILL_RE),
        ("README.en.md", README_SKILL_RE),
        ("AGENTS.md", AGENTS_SKILL_RE),
        (".agents/README.md", AGENTS_README_SKILL_RE),
    )
    findings: list[Finding] = []
    for relative, pattern in documents:
        path = repo_root / relative
        if not path.is_file():
            findings.append(Finding("catalog-document-missing", relative, "file does not exist"))
            continue
        actual = _extract_catalog_names(path, pattern)
        missing = sorted(expected - actual)
        unknown = sorted(actual - expected)
        if missing:
            findings.append(
                Finding(
                    "catalog-entry-missing",
                    relative,
                    f"missing skills: {', '.join(missing)}",
                )
            )
        if unknown:
            findings.append(
                Finding(
                    "catalog-entry-unknown",
                    relative,
                    f"unknown skills: {', '.join(unknown)}",
                )
            )
    return findings


def validate_repo(repo_root: Path) -> tuple[list[SkillRecord], list[Finding]]:
    records, findings = discover_skills(repo_root)
    findings.extend(validate_document_catalogs(repo_root, records))
    return records, sorted(findings, key=lambda item: (item.path, item.code, item.message))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="workspace root; defaults to the repository containing this script",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="output format",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    repo_root = args.repo_root.resolve()
    records, findings = validate_repo(repo_root)
    payload = {
        "status": "passed" if not findings else "failed",
        "repo_root": str(repo_root),
        "skill_count": len(records),
        "skills": [asdict(record) for record in records],
        "finding_count": len(findings),
        "findings": [asdict(finding) for finding in findings],
    }
    if args.format == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        if findings:
            for finding in findings:
                print(
                    f"{finding.path}: {finding.code}: {finding.message}",
                    file=sys.stderr,
                )
        print(json.dumps(payload, ensure_ascii=False))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
