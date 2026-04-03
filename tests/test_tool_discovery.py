import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.tool_discovery import load_discovery_index, validate_discovery_tree


def test_discovery_tree_has_no_broken_paths():
    assert validate_discovery_tree(ROOT) == []


def test_discovery_index_records_vaws_as_thin_compatibility_wrapper():
    index = load_discovery_index(ROOT)
    decision = index["namespace_decisions"]["vaws"]
    assert decision["status"] == "thin-compatibility-wrapper"
    assert decision["rule"] == "do not add new workflow ownership to vaws"


def test_vaws_thin_wrapper_rule_is_backed_by_code_shape_tests():
    text = (ROOT / "tests/test_vaws_wrapper_boundaries.py").read_text(encoding="utf-8")
    for needle in (
        "tools/vaws.py",
        "tools/lib/vaws_doctor.py",
        "tools/lib/vaws_reset.py",
        "tools/lib/vaws_machine.py",
        "tools/lib/vaws_serving.py",
        "tools/lib/vaws_benchmark.py",
    ):
        assert needle in text


def test_machine_runtime_family_manifest_lists_mutation_tools():
    manifest = (ROOT / ".agents/discovery/families/machine-runtime.yaml").read_text(encoding="utf-8")
    for tool_id in (
        "machine.bootstrap_host_ssh",
        "machine.sync_workspace_mirror",
        "runtime.reconcile_container",
        "runtime.bootstrap_container_transport",
        "runtime.cleanup_server",
    ):
        assert tool_id in manifest


def test_discovery_index_registers_workspace_foundation_family_and_skill_route():
    index = load_discovery_index(ROOT)

    assert any(family["family_id"] == "workspace-foundation" for family in index["families"])
    assert any(
        route["skill_name"] == "workspace-init"
        and "workspace-foundation" in route["family_ids"]
        and "workspace-diagnostics" in route["family_ids"]
        and "machine-inventory" in route["family_ids"]
        and "machine-runtime" in route["family_ids"]
        for route in index["skill_routes"]
    )


def test_discovery_index_registers_machine_inventory_family_and_skill_route():
    index = load_discovery_index(ROOT)

    assert any(family["family_id"] == "machine-inventory" for family in index["families"])
    assert any(
        route["skill_name"] == "machine-management"
        and "machine-inventory" in route["family_ids"]
        and "machine-runtime" in route["family_ids"]
        for route in index["skill_routes"]
    )


def test_discovery_index_registers_workspace_diagnostics_family_and_skill_route():
    index = load_discovery_index(ROOT)

    assert any(family["family_id"] == "workspace-diagnostics" for family in index["families"])
    assert any(
        route["skill_name"] == "workspace-init"
        and "workspace-diagnostics" in route["family_ids"]
        and "workspace-foundation" in route["family_ids"]
        for route in index["skill_routes"]
    )


def test_discovery_index_registers_reset_cleanup_family_and_skill_route():
    index = load_discovery_index(ROOT)

    assert any(family["family_id"] == "reset-cleanup" for family in index["families"])
    assert any(
        route["skill_name"] == "workspace-reset" and "reset-cleanup" in route["family_ids"]
        for route in index["skill_routes"]
    )


def test_discovery_index_registers_serving_family_and_skill_route():
    index = load_discovery_index(ROOT)

    assert any(family["family_id"] == "serving-lifecycle" for family in index["families"])
    assert any(
        route["skill_name"] == "serving" and "serving-lifecycle" in route["family_ids"]
        for route in index["skill_routes"]
    )


def test_discovery_index_registers_benchmark_family_and_skill_route():
    index = load_discovery_index(ROOT)

    assert any(family["family_id"] == "benchmark-execution" for family in index["families"])
    assert any(
        route["skill_name"] == "benchmark"
        and "benchmark-execution" in route["family_ids"]
        and "serving-lifecycle" in route["family_ids"]
        for route in index["skill_routes"]
    )


def test_benchmark_family_recipe_requires_explicit_service_and_no_temporary_service():
    manifest = (ROOT / ".agents/discovery/families/benchmark-execution.yaml").read_text(encoding="utf-8").lower()
    assert "benchmark-explicit-service" in manifest
    assert "temporary-service" not in manifest
    assert "weights_path" not in manifest


def test_first_contact_surface_points_to_discovery_as_fallback_only():
    for relative_path in ("AGENTS.md", "CLAUDE.md", ".cursorrules", "README.md", ".agents/README.md"):
        text = (ROOT / relative_path).read_text(encoding="utf-8").lower()
        assert ".agents/discovery/readme.md" in text
        assert "only when" in text
        assert "start at .agents/discovery/readme.md" not in text
