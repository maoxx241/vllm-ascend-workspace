from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
DISCOVERY_BACKED_SKILL_RECIPES = {
    "workspace-init": {
        "family_paths": (
            ".agents/discovery/families/workspace-foundation.yaml",
            ".agents/discovery/families/workspace-diagnostics.yaml",
            ".agents/discovery/families/machine-inventory.yaml",
            ".agents/discovery/families/machine-runtime.yaml",
        ),
        "tool_ids": (
            "workspace.probe_config_validity",
            "workspace.probe_git_auth",
            "workspace.probe_repo_topology",
            "workspace.probe_submodules",
            "workspace.describe_repo_targets",
            "workspace.diagnose_workspace",
            "machine.register_server",
            "machine.describe_server",
            "machine.list_servers",
            "machine.probe_host_ssh",
            "runtime.probe_container_transport",
            "machine.bootstrap_host_ssh",
            "machine.sync_workspace_mirror",
            "runtime.reconcile_container",
            "runtime.bootstrap_container_transport",
        ),
    },
    "machine-management": {
        "family_paths": (
            ".agents/discovery/families/machine-inventory.yaml",
            ".agents/discovery/families/machine-runtime.yaml",
        ),
        "tool_ids": (
            "machine.describe_server",
            "machine.list_servers",
            "machine.probe_host_ssh",
            "runtime.probe_container_transport",
            "machine.bootstrap_host_ssh",
            "machine.sync_workspace_mirror",
            "runtime.reconcile_container",
            "runtime.bootstrap_container_transport",
        ),
    },
    "serving": {
        "family_paths": (
            ".agents/discovery/families/serving-lifecycle.yaml",
        ),
        "tool_ids": (
            "serving.launch_service",
            "serving.probe_readiness",
            "serving.describe_session",
            "serving.list_sessions",
            "serving.stop_service",
        ),
    },
    "benchmark": {
        "family_paths": (
            ".agents/discovery/families/benchmark-execution.yaml",
            ".agents/discovery/families/serving-lifecycle.yaml",
        ),
        "tool_ids": (
            "benchmark.describe_preset",
            "benchmark.describe_run",
            "benchmark.run_probe",
            "serving.describe_session",
            "serving.list_sessions",
        ),
    },
    "workspace-reset": {
        "family_paths": (
            ".agents/discovery/families/reset-cleanup.yaml",
        ),
        "tool_ids": (
            "reset.prepare_request",
            "reset.cleanup_remote_runtime",
            "reset.cleanup_overlay",
            "reset.cleanup_known_hosts",
            "reset.restore_public_remotes",
        ),
    },
}


def _skill_text(skill_name: str) -> str:
    return (ROOT / ".agents" / "skills" / skill_name / "SKILL.md").read_text(
        encoding="utf-8"
    ).lower()


def _manifest(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def test_discovery_backed_skill_recipe_manifests_cover_expected_tool_ids():
    for skill_name, recipe in DISCOVERY_BACKED_SKILL_RECIPES.items():
        discovered_tool_ids = set()
        for family_path in recipe["family_paths"]:
            manifest = _manifest(family_path)
            discovered_tool_ids.update(tool["tool_id"] for tool in manifest["tools"])

        for tool_id in recipe["tool_ids"]:
            assert tool_id in discovered_tool_ids, (
                f"{skill_name} recipe references missing discovery tool {tool_id}"
            )


def test_public_skill_bodies_reference_discovery_families_and_tool_ids():
    for skill_name, recipe in DISCOVERY_BACKED_SKILL_RECIPES.items():
        text = _skill_text(skill_name)
        for family_path in recipe["family_paths"]:
            assert family_path.lower() in text, (
                f"{skill_name} missing discovery family path {family_path}"
            )
        for tool_id in recipe["tool_ids"]:
            assert tool_id.lower() in text, (
                f"{skill_name} missing discovery-backed recipe tool {tool_id}"
            )
