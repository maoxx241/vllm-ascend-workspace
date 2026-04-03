import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.config import RepoPaths
from tools import vaws
def test_reset_cli_dispatches_to_compat_adapter(monkeypatch, vaws_repo):
    called = {}
    monkeypatch.chdir(vaws_repo)

    def fake_run(paths, args):
        called["root"] = str(paths.root)
        called["reset_command"] = args.reset_command
        return 0

    monkeypatch.setattr(vaws, "vaws_reset", SimpleNamespace(run=fake_run), raising=False)

    assert vaws.main(["reset", "prepare"]) == 0
    assert called == {
        "root": str(vaws_repo),
        "reset_command": "prepare",
    }


def test_vaws_reset_has_no_execute_subcommand():
    parser = vaws.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["reset", "execute"])
