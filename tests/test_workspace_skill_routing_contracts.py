from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIRST_CLASS_SKILLS = (
    "workspace-init",
    "machine-management",
    "serving",
    "benchmark",
    "workspace-reset",
)
REQUIRED_ROUTING_HEADERS = (
    "## Sanctioned Adapter Surface",
    "## Internal Delegation",
    "## Action Routing",
    "## Internal State Touched",
    "## Related Tests",
)


def _routing_text(skill_name: str) -> str:
    return (
        ROOT / ".agents" / "skills" / skill_name / "references" / "internal-routing.md"
    ).read_text(encoding="utf-8")


def _routing_section(skill_name: str, header: str) -> str:
    text = _routing_text(skill_name)
    marker = f"\n{header}\n"
    start = text.find(marker)
    assert start != -1, f"{skill_name} missing section {header}"
    start += len(marker)
    tail = text[start:]
    next_header = tail.find("\n## ")
    if next_header == -1:
        return tail.strip()
    return tail[:next_header].strip()


def test_internal_routing_files_declare_secondary_maintainer_role():
    for skill_name in FIRST_CLASS_SKILLS:
        text = _routing_text(skill_name)
        for header in REQUIRED_ROUTING_HEADERS:
            assert header in text, f"{skill_name} missing routing section {header}"

        lowered = text.lower()
        assert "maintainer backstop" in lowered
        assert "normal execution surface" in lowered
        assert "agents should not need this file" in lowered


def test_internal_routing_files_are_detailed_backstops_not_first_hops():
    for skill_name in FIRST_CLASS_SKILLS:
        text = _routing_text(skill_name).lower()
        assert "maintainer backstop" in text
        assert "agents should not need this file" in text
        assert "ambiguous routing" in text or "fallback" in text


def test_action_routing_stays_on_truthful_compatibility_and_discovery_surfaces():
    for skill_name in FIRST_CLASS_SKILLS:
        body = _routing_section(skill_name, "## Action Routing").lower()
        assert "sanctioned adapter" in body
        assert "tools/lib/" not in body
        assert "backend entrypoints" not in body


def test_internal_routing_files_use_canonical_cli_vocabulary_and_no_retired_modules():
    forbidden_terms = (
        "fleet add",
        "fleet verify",
        "fleet list",
        "reset --prepare",
        "reset --execute",
        "git-profile",
        "tools/lib/foundation.py",
        "tools/lib/git_profile.py",
        "tools/lib/fleet.py",
        "tests/test_vaws_fleet.py",
        "tests/test_vaws_init_bootstrap.py",
    )
    for skill_name in FIRST_CLASS_SKILLS:
        text = _routing_text(skill_name).lower()
        for forbidden in forbidden_terms:
            assert forbidden not in text, (
                f"{skill_name} leaked retired routing term: {forbidden}"
            )


def test_routing_files_reference_canonical_adapters():
    expectations = {
        "workspace-init": ("tools/vaws.py doctor",),
        "machine-management": (
            "tools/vaws.py machine add",
            "tools/vaws.py machine verify",
        ),
        "serving": ("tools/vaws.py serving start", "tools/vaws.py serving stop"),
        "benchmark": (
            "tools/vaws.py benchmark run",
            "tools/vaws.py serving list",
            "tools/vaws.py serving status",
        ),
        "workspace-reset": (
            "tools/vaws.py reset prepare",
            "tools/vaws.py doctor",
        ),
    }
    for skill_name, markers in expectations.items():
        text = _routing_text(skill_name).lower()
        for marker in markers:
            assert marker in text, f"{skill_name} missing routing marker: {marker}"


def test_workspace_init_routing_points_to_foundation_diagnostics_and_optional_machine_families():
    text = _routing_text("workspace-init").lower()
    assert ".agents/discovery/families/workspace-foundation.yaml" in text
    assert ".agents/discovery/families/workspace-diagnostics.yaml" in text
    assert ".agents/discovery/families/machine-inventory.yaml" in text
    assert ".agents/discovery/families/machine-runtime.yaml" in text
    assert "workspace.probe_config_validity" in text
    assert "workspace.probe_git_auth" in text
    assert "workspace.probe_repo_topology" in text
    assert "workspace.probe_submodules" in text
    assert "workspace.describe_repo_targets" in text
    assert "workspace.diagnose_workspace" in text
    assert "machine.register_server" in text
    assert "machine.probe_host_ssh" in text
    assert "runtime.probe_container_transport" in text


def test_machine_management_routing_points_to_discovery_family_and_split_ladders():
    text = _routing_text("machine-management").lower()
    assert ".agents/discovery/families/machine-inventory.yaml" in text
    assert ".agents/discovery/families/machine-runtime.yaml" in text
    assert "machine.describe_server" in text
    assert "machine.list_servers" in text
    assert "machine.probe_host_ssh" in text
    assert "runtime.probe_container_transport" in text
    assert "machine.bootstrap_host_ssh" in text
    assert "machine.sync_workspace_mirror" in text
    assert "runtime.reconcile_container" in text
    assert "runtime.bootstrap_container_transport" in text
    assert "runtime.cleanup_server" in text


def test_serving_routing_points_to_discovery_family_and_atomic_split():
    text = _routing_text("serving").lower()
    assert ".agents/discovery/families/serving-lifecycle.yaml" in text
    assert "serving.launch_service" in text
    assert "serving.probe_readiness" in text
    assert "serving.describe_session" in text
    assert "serving.list_sessions" in text
    assert "serving.stop_service" in text


def test_benchmark_routing_points_to_discovery_family_and_explicit_service_path():
    text = _routing_text("benchmark").lower()
    assert ".agents/discovery/families/benchmark-execution.yaml" in text
    assert ".agents/discovery/families/serving-lifecycle.yaml" in text
    assert "benchmark.describe_preset" in text
    assert "benchmark.describe_run" in text
    assert "benchmark.run_probe" in text
    assert "serving.list_sessions" in text
    assert "serving.describe_session" in text
    assert "temporary-service" not in text


def test_workspace_reset_routing_points_to_reset_cleanup_family():
    text = _routing_text("workspace-reset").lower()
    assert ".agents/discovery/families/reset-cleanup.yaml" in text
    assert "reset.prepare_request" in text
    assert "reset.cleanup_remote_runtime" in text
    assert "reset.cleanup_overlay" in text
    assert "reset.cleanup_known_hosts" in text
    assert "reset.restore_public_remotes" in text
    assert "tools/vaws.py reset execute" not in text
