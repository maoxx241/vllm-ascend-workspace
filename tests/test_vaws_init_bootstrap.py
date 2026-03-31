import sys
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from conftest import run_vaws
from tools.lib import preflight


def _remote_url(repo, relative_path, remote_name):
    result = subprocess.run(
        ["git", "remote", "get-url", remote_name],
        cwd=repo / relative_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_init_bootstrap_writes_overlay_and_configures_repo_remotes(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
        "--vllm-origin-url",
        "git@github.com:alice/vllm.git",
        "--git-auth-mode",
        "ssh-agent",
    )

    assert result.returncode == 0

    repos = yaml.safe_load((vaws_repo / ".workspace.local" / "repos.yaml").read_text())
    assert repos["submodules"]["vllm"]["upstream_url"] == "https://github.com/vllm-project/vllm.git"
    assert repos["submodules"]["vllm"]["origin_url"] == "git@github.com:alice/vllm.git"
    assert repos["submodules"]["vllm-ascend"]["upstream_url"] == "https://github.com/vllm-project/vllm-ascend.git"
    assert repos["submodules"]["vllm-ascend"]["origin_url"] == "git@github.com:alice/vllm-ascend.git"

    auth = yaml.safe_load((vaws_repo / ".workspace.local" / "auth.yaml").read_text())
    assert auth["version"] == 1
    assert auth["ssh_auth"]["refs"]["default"]["kind"] == "ssh-key"
    assert auth["ssh_auth"]["refs"]["default"]["username"] == "root"
    assert auth["git_auth"]["refs"]["default"]["kind"] == "ssh-agent"

    targets = yaml.safe_load(
        (vaws_repo / ".workspace.local" / "targets.yaml").read_text()
    )
    assert targets["hosts"]["host-a"]["host"] == "173.125.1.2"
    assert targets["targets"]["single-default"]["runtime"]["workspace_root"] == "/vllm-workspace"

    assert _remote_url(vaws_repo, "vllm", "origin") == "git@github.com:alice/vllm.git"
    assert _remote_url(vaws_repo, "vllm", "upstream") == "https://github.com/vllm-project/vllm.git"
    assert _remote_url(vaws_repo, "vllm-ascend", "origin") == "git@github.com:alice/vllm-ascend.git"
    assert _remote_url(vaws_repo, "vllm-ascend", "upstream") == "https://github.com/vllm-project/vllm-ascend.git"

    target_result = run_vaws(vaws_repo, "target", "ensure", "single-default")
    assert target_result.returncode == 0
    state = yaml.safe_load((vaws_repo / ".workspace.local" / "state.json").read_text())
    assert state["schema_version"] == 1
    assert state["current_target"] == "single-default"


def test_preflight_reports_missing_and_optional_tools(monkeypatch):
    def fake_which(command):
        if command in {"ssh", "gh"}:
            return None
        return f"/usr/bin/{command}"

    monkeypatch.setattr(preflight.shutil, "which", fake_which)

    report = preflight.check_local_control_plane_deps()

    assert report.missing_required == ("ssh",)
    assert report.missing_recommended == ("gh",)


def test_init_bootstrap_creates_overlay_compatible_with_doctor(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
        "--vllm-origin-url",
        "git@github.com:alice/vllm.git",
        "--git-auth-mode",
        "ssh-agent",
    )

    assert result.returncode == 0

    doctor_result = run_vaws(vaws_repo, "doctor")

    assert doctor_result.returncode == 0
    assert "doctor: ok" in doctor_result.stdout.lower()


def test_init_bootstrap_fails_cleanly_for_malformed_state_json(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "state.json").write_text("not valid json", encoding="utf-8")

    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "state.json" in output
    assert "invalid runtime state" in output or "invalid" in output
    assert "traceback" not in output


def test_init_bootstrap_fails_cleanly_for_unsupported_state_schema_version(vaws_repo):
    overlay = vaws_repo / ".workspace.local"
    overlay.mkdir()
    (overlay / "servers.yaml").write_text("version: 1\nservers: {}\n", encoding="utf-8")
    (overlay / "auth.yaml").write_text(
        "version: 1\nssh_auth: {refs: {}}\ngit_auth: {refs: {}}\n",
        encoding="utf-8",
    )
    (overlay / "repos.yaml").write_text(
        "version: 1\nworkspace: {}\nsubmodules: {}\n",
        encoding="utf-8",
    )
    (overlay / "state.json").write_text('{"schema_version": 2}\n', encoding="utf-8")

    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "schema_version" in output
    assert "unsupported" in output or "invalid" in output
    assert "traceback" not in output


def test_init_bootstrap_allows_missing_vllm_origin_url(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 0

    repos = yaml.safe_load((vaws_repo / ".workspace.local" / "repos.yaml").read_text())
    assert "origin_url" not in repos["submodules"]["vllm"]
    assert repos["submodules"]["vllm-ascend"]["origin_url"] == "git@github.com:alice/vllm-ascend.git"
    assert _remote_url(vaws_repo, "vllm", "origin") == "https://github.com/vllm-project/vllm.git"
    assert _remote_url(vaws_repo, "vllm", "upstream") == "https://github.com/vllm-project/vllm.git"


def test_init_bootstrap_requires_vllm_ascend_origin_url(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-host",
        "173.125.1.2",
        "--server-user",
        "root",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "vllm-ascend" in output
    assert "origin" in output


def test_init_bootstrap_requires_server_host(vaws_repo):
    result = run_vaws(
        vaws_repo,
        "init",
        "--bootstrap",
        "--server-user",
        "root",
        "--vllm-ascend-origin-url",
        "git@github.com:alice/vllm-ascend.git",
    )

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "server" in output
    assert "host" in output
