"""Execute generated hooks under native Windows shells with literal input."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("windows_client_setup", ROOT / ".agents/scripts/vaws_client_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)


@pytest.mark.skipif(os.name != "nt", reason="native Windows shell execution")
@pytest.mark.parametrize("shell", ["cmd", "powershell", "pwsh"])
def test_hook_roundtrip_literal_arguments_stdin_and_exit(shell):
    executable = shutil.which(shell)
    if executable is None:
        pytest.skip(f"{shell} is not installed")
    with tempfile.TemporaryDirectory(prefix="hook 中文 '") as temporary:
        script = Path(temporary) / "echo args.py"
        script.write_text(
            "import json, sys\nprint(json.dumps([sys.argv[1:], sys.stdin.read()], ensure_ascii=False))\nsys.exit(7)\n",
            encoding="utf-8",
        )
        arguments = [sys.executable, str(script), "中文 空格", "x&y", "$value", "single'quote", "%PATH%"]
        command = setup.local_hook_command(arguments)
        assert setup.hook_argv(command) == arguments
        invocation = ([executable, "/d", "/s", "/c", command] if shell == "cmd" else
                      [executable, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", command + "; exit $LASTEXITCODE"])
        result = subprocess.run(invocation, input='{"message":"输入文本"}', text=True,
                                encoding="utf-8", capture_output=True, timeout=20)
        assert result.returncode == 7, result.stderr
        assert json.loads(result.stdout) == [arguments[2:], '{"message":"输入文本"}']


def test_foreign_encoded_command_is_not_owned():
    assert not setup.owned_hook_command("powershell.exe -EncodedCommand Zg==", "codex", ROOT)
