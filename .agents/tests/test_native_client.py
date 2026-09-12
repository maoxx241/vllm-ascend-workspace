"""The first child operation observes its real directory and literal inputs."""
from __future__ import annotations

import json
import os
import shutil
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


def test_native_shell_and_uv_use_selected_python_and_package_commits(tmp_path):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    sys.path.insert(0, str(scripts))
    import vaws_client
    receipt = {"python": sys.executable, "root": sys.prefix}
    inherited = {**os.environ, "VIRTUAL_ENV": str(tmp_path / "wrong-environment"),
                 "PYTHONHOME": str(tmp_path / "wrong-python-home"),
                 "VAWS_MANAGED_ENV_RECEIPT": "independent-managed-pin"}
    environment = vaws_client.activated_client_environment(receipt, inherited)
    assert inherited["VIRTUAL_ENV"] != environment["VIRTUAL_ENV"]
    assert environment["VAWS_MANAGED_ENV_RECEIPT"] == "independent-managed-pin"
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import json,os,sys,importlib,importlib.metadata as m\n"
        "packages={}\n"
        "for module,name in [('remote_dev','vaws-remote-dev'),('vaws_coordinator','vaws-coordinator'),('vaws_knowledge','vaws-knowledge')]:\n"
        " d=m.distribution(name); direct=json.loads(d.read_text('direct_url.json') or '{}')\n"
        " packages[name]={'module':importlib.import_module(module).__file__,'version':d.version,'commit':direct.get('vcs_info',{}).get('commit_id')}\n"
        "print(json.dumps({'python':sys.executable,'prefix':sys.prefix,'active':os.environ.get('VIRTUAL_ENV'),'packages':packages}))\n",
        encoding="utf-8")
    reference = subprocess.run([sys.executable, str(probe)], capture_output=True, text=True, check=True)
    expected = json.loads(reference.stdout)
    shell = ([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", "python probe.py"]
             if os.name == "nt" else ["/bin/sh", "-c", "python probe.py"])
    commands = [shell]
    if shutil.which("uv"):
        commands.append([shutil.which("uv"), "run", "--no-project", "--offline", "python", "probe.py"])
    for command in commands:
        result = subprocess.run(command, cwd=tmp_path, env=environment,
                                capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        observed = json.loads(result.stdout)
        assert Path(observed["python"]).resolve() == Path(expected["python"]).resolve()
        assert Path(observed["prefix"]).resolve() == Path(receipt["root"]).resolve()
        assert observed["active"] == receipt["root"]
        assert observed["packages"] == expected["packages"]
