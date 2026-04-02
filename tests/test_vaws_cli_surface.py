import argparse
import sys
from pathlib import Path

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

    assert {"init", "machine", "serving", "benchmark", "reset", "doctor", "internal"} == set(choices)
    assert "foundation" not in choices
    assert "git-profile" not in choices
    assert "fleet" not in choices
    assert "target" not in choices
    assert "sync" not in choices


def test_reset_uses_subcommands_not_boolean_flags():
    parser = vaws.build_parser()
    args = parser.parse_args(["reset", "prepare"])
    assert args.command == "reset"
    assert args.reset_command == "prepare"


def test_internal_remotes_normalize_parses():
    parser = vaws.build_parser()
    args = parser.parse_args(["internal", "remotes", "normalize"])
    assert args.command == "internal"
    assert args.internal_command == "remotes"
    assert args.remotes_command == "normalize"


def test_canonical_code_paths_do_not_import_retired_modules():
    for relative_path in (
        "tools/vaws.py",
        "tools/lib/init_flow.py",
        "tools/lib/machine.py",
    ):
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "tools.lib.foundation" not in text
        assert "tools.lib.git_profile" not in text
        assert "tools.lib.bootstrap" not in text
        assert "tools.lib.fleet" not in text
