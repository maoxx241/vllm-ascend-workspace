import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from conftest import run_vaws

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import vaws

PLACEHOLDER_WORKSPACE_URL = "git@github.com:your-org/vllm-ascend-workspace.git"


def _set_remote(repo, remote_name: str, url: str) -> None:
    subprocess.run(
        ["git", "remote", "set-url", remote_name, url],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def _seed_canonical_overlay(vaws_repo) -> None:
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir(exist_ok=True)
    (overlay / "servers.yaml").write_text("version: 1\nservers: {}\n", encoding="utf-8")
    (overlay / "auth.yaml").write_text(
        "version: 1\nssh_auth: {refs: {}}\ngit_auth: {refs: {}}\n",
        encoding="utf-8",
    )
    (overlay / "repos.yaml").write_text(
        "version: 1\nworkspace:\n  protected_branches: [main]\nsubmodules: {}\n",
        encoding="utf-8",
    )
    _set_remote(vaws_repo, "origin", "https://github.com/alice/vllm-ascend-workspace.git")
    _set_remote(vaws_repo, "upstream", "https://github.com/vllm-project/vllm-ascend-workspace.git")


def test_doctor_reports_missing_overlay(vaws_repo):
    result = run_vaws(vaws_repo, "doctor")
    assert result.returncode == 1
    assert ".workspace.local" in result.stdout


def test_doctor_cli_dispatches_to_compat_adapter(monkeypatch, vaws_repo):
    called = {}
    monkeypatch.chdir(vaws_repo)
    monkeypatch.setattr(
        vaws,
        "doctor",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("legacy doctor path called")),
        raising=False,
    )

    def fake_run(paths):
        called["root"] = str(paths.root)
        return 0

    monkeypatch.setattr(vaws, "vaws_doctor", SimpleNamespace(run=fake_run), raising=False)

    assert vaws.main(["doctor"]) == 0
    assert called == {"root": str(vaws_repo)}


def test_doctor_reports_invalid_servers_inventory(vaws_repo):
    _seed_canonical_overlay(vaws_repo)
    (vaws_repo / ".workspace.local" / "servers.yaml").write_text(
        "not: [valid\n",
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "invalid" in output
    assert "servers.yaml" in output


def test_doctor_reports_placeholder_workspace_remote(vaws_repo):
    _seed_canonical_overlay(vaws_repo)
    _set_remote(vaws_repo, "upstream", PLACEHOLDER_WORKSPACE_URL)

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "placeholder" in output.lower()
    assert "remote" in output.lower()


def test_doctor_fails_when_recursive_submodule_is_missing(vaws_repo):
    _seed_canonical_overlay(vaws_repo)
    catlass_root = vaws_repo / "vllm-ascend" / "csrc" / "third_party" / "catlass"
    git_marker = catlass_root / ".git"
    shutil.rmtree(git_marker)

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "submodule" in output
    assert "catlass" in output


def test_doctor_no_longer_requires_state_json(vaws_repo):
    _seed_canonical_overlay(vaws_repo)
    assert not (vaws_repo / ".workspace.local" / "state.json").exists()

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 0
    assert "doctor: ok" in result.stdout.lower()


def test_doctor_succeeds_for_canonical_overlay_and_recursive_submodules(vaws_repo):
    _seed_canonical_overlay(vaws_repo)

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 0
    assert "doctor: ok" in result.stdout.lower()
