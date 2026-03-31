import subprocess

import yaml

from conftest import run_vaws


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
    assert auth["host_auth"]["mode"] == "ssh-key"
    assert auth["git_auth"]["mode"] == "ssh-agent"

    targets = yaml.safe_load(
        (vaws_repo / ".workspace.local" / "targets.yaml").read_text()
    )
    assert targets["hosts"]["host-a"]["host"] == "173.125.1.2"
    assert targets["targets"]["single-default"]["runtime"]["workspace_root"] == "/vllm-workspace"

    assert _remote_url(vaws_repo, "vllm", "origin") == "git@github.com:alice/vllm.git"
    assert _remote_url(vaws_repo, "vllm", "upstream") == "https://github.com/vllm-project/vllm.git"
    assert _remote_url(vaws_repo, "vllm-ascend", "origin") == "git@github.com:alice/vllm-ascend.git"
    assert _remote_url(vaws_repo, "vllm-ascend", "upstream") == "https://github.com/vllm-project/vllm-ascend.git"


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
