"""Native entries select by immutable environment identity, without import probes."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import vaws_environment as environments
import vaws_venv

ROOT = Path(__file__).resolve().parents[2]


def test_runtime_bootstrap_accepts_current_receipt_without_import_probes(monkeypatch):
    monkeypatch.delenv(vaws_venv.SKIP_ENV, raising=False)
    monkeypatch.setenv(environments.PIN_ENV, "before")
    monkeypatch.setenv(vaws_venv.REEXEC_ENV, "current")
    receipt = {"key": "current", "root": sys.prefix, "python": sys.executable, "receipt": "pinned"}
    monkeypatch.setattr(vaws_venv, "native_ready", lambda root: receipt)
    if os.name == "nt" and not sys.flags.utf8_mode:
        pytest.skip("this fixture needs the test runner's UTF-8 mode")
    vaws_venv.ensure_workspace_interpreter(repo_root=ROOT, packages=("not_installed_optional_package",))
    assert os.environ[environments.PIN_ENV] == "pinned"
    assert vaws_venv.REEXEC_ENV not in os.environ


def test_explicit_entry_requires_ready_identity_even_with_reexec_boolean(monkeypatch, tmp_path):
    monkeypatch.delenv(vaws_venv.SKIP_ENV, raising=False)
    monkeypatch.delenv(environments.PIN_ENV, raising=False)
    monkeypatch.setenv(vaws_venv.REEXEC_ENV, "1")
    with pytest.raises(SystemExit) as failure:
        vaws_venv.ensure_workspace_interpreter(repo_root=tmp_path, packages=("json",))
    assert failure.value.code == 2


def test_bootstrap_does_not_need_installed_packages(monkeypatch):
    spec = importlib.util.spec_from_file_location("bootstrap_under_test", ROOT / ".agents/scripts/vaws_deps.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    calls = []
    receipt = {"root": str(ROOT), "key": "f" * 64, "receipt": "ready-receipt"}
    monkeypatch.setattr(module, "ensure_workspace_interpreter", lambda **kwargs: pytest.fail("bootstrap tried to re-exec"))
    monkeypatch.setattr(module, "prepare_environment", lambda root, **kwargs: calls.append((root, kwargs)) or receipt)
    monkeypatch.setattr(module, "prepare_knowledge", lambda root: {"status": "pending", "ready": False})
    import vaws_environment_link
    monkeypatch.setattr(vaws_environment_link, "link_environment", lambda *args, **kwargs: ROOT)
    assert module.main(["sync", "--locked", "--group", "dev"]) == 0
    assert calls == [(ROOT, {"install_options": ["--locked", "--group", "dev"]})]


def test_shared_checkout_text_identity_does_not_depend_on_git_autocrlf(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    shutil.copyfile(ROOT / ".gitattributes", tmp_path / ".gitattributes")
    source = tmp_path / "entry.py"
    identities = set()
    for line_ending in (b"\n", b"\r\n"):
        source.write_bytes(b"print('platform independent')" + line_ending)
        for autocrlf in ("true", "false", "input"):
            result = subprocess.run(["git", "-c", f"core.autocrlf={autocrlf}", "hash-object", "--path=entry.py", "entry.py"],
                                    cwd=tmp_path, capture_output=True, text=True, check=True)
            identities.add(result.stdout.strip())
    assert len(identities) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows nested interpreter UTF-8 handoff")
def test_nested_child_can_enable_utf8_with_inherited_environment_marker():
    code = (
        "import os,sys; from pathlib import Path; "
        f"sys.path.insert(0,{str(ROOT / '.agents/lib')!r}); import vaws_venv; "
        "receipt={'key':'nested','root':sys.prefix,'python':sys.executable,'receipt':'fixture'}; "
        "vaws_venv.native_ready=lambda root:receipt; "
        "os.environ[vaws_venv.REEXEC_ENV]='nested'; "
        "vaws_venv.ensure_workspace_interpreter(repo_root=Path.cwd()); "
        "print('nested-utf8',sys.flags.utf8_mode)"
    )
    environment = dict(os.environ, PYTHONUTF8="0")
    environment.pop(vaws_venv.SKIP_ENV, None)
    result = subprocess.run([sys.executable, "-c", code], env=environment,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "nested-utf8 1"
