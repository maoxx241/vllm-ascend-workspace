"""A native module entry keeps arguments and selects the platform installation."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import vaws_venv

ROOT = Path(__file__).resolve().parents[2]


def test_native_module_entry_preserves_unicode_arguments_and_python_flags():
    base = Path(sys.base_prefix) / ("python.exe" if os.name == "nt" else "bin/python3")
    assert base.is_file()
    with tempfile.TemporaryDirectory(prefix="agent module ") as temporary:
        directory = Path(temporary)
        module = directory / "agent_entry.py"
        module.write_text(
            "from pathlib import Path\nimport json,sys\n"
            "from vaws_venv import ensure_workspace_interpreter\n"
            f"ensure_workspace_interpreter(repo_root=Path({str(ROOT)!r}))\n"
            "import vaws_coordinator\n"
            "print(json.dumps({'args':sys.argv[1:], 'utf8':sys.flags.utf8_mode, 'python':sys.executable},ensure_ascii=False))\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        for key in ("VAWS_SKIP_VENV_REEXEC", "VAWS_VENV_REEXEC", "VIRTUAL_ENV"):
            environment.pop(key, None)
        environment["PYTHONPATH"] = os.pathsep.join((str(directory), str(ROOT / ".agents/lib")))
        environment["PYTHONNOUSERSITE"] = "1"
        reply = subprocess.run([str(base), "-X", "utf8", "-m", "agent_entry", "中文 path", "--business-option"],
            env=environment, cwd=directory, capture_output=True, encoding="utf-8", timeout=30)
        assert reply.returncode == 0, reply.stderr
        data = json.loads(reply.stdout)
        assert data["args"] == ["中文 path", "--business-option"]
        assert data["utf8"] == 1
        assert Path(data["python"]).absolute() == vaws_venv.workspace_venv_python(ROOT).absolute()


def test_bootstrap_does_not_need_installed_packages(monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location("bootstrap_under_test", ROOT / ".agents/scripts/vaws_deps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    def execute(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(module, "ensure_workspace_interpreter", lambda **kwargs: (_ for _ in ()).throw(AssertionError("bootstrap tried to re-exec")))
    monkeypatch.setattr(module.subprocess, "run", execute)
    assert module.main(["sync", "--locked", "--group", "dev"]) == 0
    command, kwargs = calls[0]
    assert command == ["uv", "sync", "--locked", "--group", "dev"]
    assert Path(kwargs["env"]["UV_PROJECT_ENVIRONMENT"]) == ROOT / ".vaws-local/venvs" / sys.platform


def test_shared_checkout_text_identity_does_not_depend_on_git_autocrlf(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    shutil.copyfile(ROOT / ".gitattributes", tmp_path / ".gitattributes")
    source = tmp_path / "entry.py"
    identities = set()
    for line_ending in (b"\n", b"\r\n"):
        source.write_bytes(b"print('platform independent')" + line_ending)
        for autocrlf in ("true", "false", "input"):
            result = subprocess.run(
                ["git", "-c", f"core.autocrlf={autocrlf}", "hash-object", "--path=entry.py", "entry.py"],
                cwd=tmp_path, capture_output=True, text=True, check=True,
            )
            identities.add(result.stdout.strip())
    assert len(identities) == 1
