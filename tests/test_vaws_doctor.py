import json
import shutil
import subprocess

from conftest import run_vaws

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
    (overlay / "state.json").write_text(
        json.dumps({"schema_version": 2, "servers": {}}, indent=2) + "\n",
        encoding="utf-8",
    )
    _set_remote(vaws_repo, "origin", "https://github.com/alice/vllm-ascend-workspace.git")
    _set_remote(vaws_repo, "upstream", "https://github.com/vllm-project/vllm-ascend-workspace.git")


def test_doctor_reports_missing_overlay(vaws_repo):
    result = run_vaws(vaws_repo, "doctor")
    assert result.returncode == 1
    assert ".workspace.local" in result.stdout


def test_doctor_reports_targets_yaml_as_legacy_residue(vaws_repo):
    _seed_canonical_overlay(vaws_repo)
    (vaws_repo / ".workspace.local" / "targets.yaml").write_text(
        "current_target: legacy-box\n",
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "targets.yaml" in output
    assert "legacy" in output.lower() or "retired" in output.lower()


def test_doctor_reports_retired_state_keys(vaws_repo):
    _seed_canonical_overlay(vaws_repo)
    (vaws_repo / ".workspace.local" / "state.json").write_text(
        '{"schema_version": 1, "lifecycle": {"foundation": {"status": "ready"}}}\n',
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "schema_version" in output
    assert "retired key" in output
    assert "lifecycle" in output


def test_doctor_reports_legacy_server_inventory_residue(vaws_repo):
    _seed_canonical_overlay(vaws_repo)
    (vaws_repo / ".workspace.local" / "servers.yaml").write_text(
        "\n".join(
            [
                "version: 1",
                "bootstrap:",
                "  completed: true",
                "  mode: remote-first",
                "servers:",
                "  lab-a:",
                "    host: 10.0.0.12",
                "    port: 22",
                "    login_user: root",
                "    ssh_auth_ref: shared-lab-a",
                "    runtime:",
                "      image_ref: quay.nju.edu.cn/ascend/vllm-ascend:latest",
                "      container_name: vaws-workspace",
                "      ssh_port: 41001",
                "      workspace_root: /vllm-workspace",
                "      bootstrap_mode: host-then-container",
                "    verification:",
                "      status: ready",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = (result.stdout + result.stderr).lower()
    assert "bootstrap" in output
    assert "verification" in output


def test_doctor_reports_placeholder_workspace_remote(vaws_repo):
    _seed_canonical_overlay(vaws_repo)
    _set_remote(vaws_repo, "upstream", PLACEHOLDER_WORKSPACE_URL)

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 1
    output = result.stdout + result.stderr
    assert "placeholder_workspace_remote" in output


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


def test_doctor_succeeds_for_canonical_overlay_and_recursive_submodules(vaws_repo):
    _seed_canonical_overlay(vaws_repo)

    result = run_vaws(vaws_repo, "doctor")

    assert result.returncode == 0
    assert "doctor: ok" in result.stdout.lower()
