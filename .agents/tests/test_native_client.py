"""The first child operation observes its real directory and literal inputs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_native_child_cwd_literal_arguments_and_fresh_identity(tmp_path):
    workspace = tmp_path / "工作目录 with spaces"
    workspace.mkdir()
    client = tmp_path / "client.py"
    client.write_text(
        "import json,os,sys; from pathlib import Path; "
        "print(json.dumps({'cwd':str(Path.cwd()),'args':sys.argv[1:],"
        "'context':os.environ.get('VAWS_CONTEXT_FILE'),'native':os.environ.get('CODEX_THREAD_ID'),"
        "'literal':os.environ['TEST_LITERAL']},ensure_ascii=False))", encoding="utf-8")
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    entry = "import sys;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import vaws_client;raise SystemExit(vaws_client.run_client([sys.executable,sys.argv[2],*sys.argv[4:]],Path(sys.argv[3])))"
    literals = ["中文 空格", "$(echo wrong); & |", "single' double\"", "line\nnext"]
    result = subprocess.run([sys.executable, "-X", "utf8", "-c", entry, str(scripts), str(client), str(workspace), *literals],
                            capture_output=True, encoding="utf-8", timeout=20,
                            env={**os.environ, "PYTHONUTF8": "1", "VAWS_CONTEXT_FILE": "parent-context",
                                 "CODEX_THREAD_ID": "parent-id", "TEST_LITERAL": "$(unchanged); 中文"})
    assert result.returncode == 0, result.stderr
    observed = json.loads(result.stdout)
    assert Path(observed["cwd"]).resolve() == workspace.resolve()
    assert observed["args"] == literals
    assert observed["context"] is None and observed["native"] is None
    assert observed["literal"] == "$(unchanged); 中文"
