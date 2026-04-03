from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

import yaml

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.lib.agent_contract import validate_tool_result
from tools.lib.config import RepoPaths


def _validate_yaml_mapping(path: Path) -> str | None:
    if not path.is_file():
        return f"missing overlay file: {path.relative_to(path.parents[1])}"
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return f"invalid overlay file: {path.relative_to(path.parents[1])}"
    if payload is not None and not isinstance(payload, dict):
        return f"invalid overlay file: {path.relative_to(path.parents[1])}"
    return None


def probe_config_validity(paths: RepoPaths) -> dict[str, object]:
    if not paths.local_overlay.is_dir():
        return validate_tool_result(
            {
                "status": "needs_repair",
                "observations": ["missing local overlay directory: .workspace.local/"],
                "reason": "local overlay directory is missing",
                "next_probes": ["workspace.probe_config_validity"],
                "idempotent": True,
            },
            action_kind="probe",
        )

    problems = [
        problem
        for problem in (
            _validate_yaml_mapping(paths.local_servers_file),
            _validate_yaml_mapping(paths.local_auth_file),
            _validate_yaml_mapping(paths.local_repos_file),
        )
        if problem is not None
    ]
    if problems:
        return validate_tool_result(
            {
                "status": "needs_repair",
                "observations": problems,
                "reason": "; ".join(problems),
                "next_probes": ["workspace.probe_config_validity"],
                "idempotent": True,
            },
            action_kind="probe",
        )

    return validate_tool_result(
        {
            "status": "ready",
            "observations": ["workspace overlay files are present and parseable"],
            "payload": {
                "checked_files": [
                    ".workspace.local/servers.yaml",
                    ".workspace.local/auth.yaml",
                    ".workspace.local/repos.yaml",
                ]
            },
            "idempotent": True,
        },
        action_kind="probe",
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="workspace.probe_config_validity")
    parser.parse_args(argv)
    result = probe_config_validity(RepoPaths(root=Path.cwd()))
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
