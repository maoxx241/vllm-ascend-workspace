import argparse
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import vaws


def _subparser_choices(parser: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    return action.choices


def test_public_top_level_commands_match_canonical_surface():
    parser = vaws.build_parser()
    choices = _subparser_choices(parser)

    assert {"machine", "serving", "benchmark", "reset", "doctor"} == set(choices)
    assert "init" not in choices
    assert "foundation" not in choices
    assert "git-profile" not in choices
    assert "fleet" not in choices
    assert "target" not in choices
    assert "sync" not in choices
    assert "internal" not in choices


def test_vaws_has_no_init_command():
    parser = vaws.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["init"])


def test_reset_has_prepare_but_no_execute():
    parser = vaws.build_parser()
    args = parser.parse_args(["reset", "prepare"])
    assert args.command == "reset"
    assert args.reset_command == "prepare"
    with pytest.raises(SystemExit):
        parser.parse_args(["reset", "execute"])


def test_benchmark_run_requires_explicit_service_id():
    parser = vaws.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "benchmark",
                "run",
                "--server-name",
                "lab-a",
                "--preset",
                "qwen3_5_35b_tp4_perf",
            ]
        )


def test_canonical_code_paths_do_not_import_retired_modules():
    for relative_path in (
        "tools/vaws.py",
        "tools/lib/machine.py",
    ):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "tools.lib.foundation" not in text
        assert "tools.lib.git_profile" not in text
        assert "tools.lib.bootstrap" not in text
        assert "tools.lib.fleet" not in text
        assert "tools.lib.init_flow" not in text
        assert "tools.lib.reset" not in text
