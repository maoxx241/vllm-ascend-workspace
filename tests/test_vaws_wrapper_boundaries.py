import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _module_name(relative_path: str) -> str:
    return ".".join(Path(relative_path).with_suffix("").parts)


def _resolve_from_import(module_name: str, node: ast.ImportFrom) -> str:
    if node.level == 0:
        return node.module or ""
    base_parts = module_name.split(".")[:-node.level]
    if node.module:
        base_parts.extend(node.module.split("."))
    return ".".join(base_parts)


def _import_refs(relative_path: str) -> set[str]:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relative_path)
    module_name = _module_name(relative_path)
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            refs.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = _resolve_from_import(module_name, node)
            for alias in node.names:
                refs.add(f"{base}.{alias.name}" if base else alias.name)
    return refs


def test_vaws_parser_imports_only_repo_paths_and_compat_adapters():
    refs = {ref for ref in _import_refs("tools/vaws.py") if ref.startswith("tools.lib.")}
    assert refs == {
        "tools.lib.config.RepoPaths",
        "tools.lib.vaws_doctor",
        "tools.lib.vaws_reset",
        "tools.lib.vaws_machine",
        "tools.lib.vaws_serving",
        "tools.lib.vaws_benchmark",
    }


def test_legacy_adapters_have_explicit_boundaries():
    for relative_path, allowed_prefixes in (
        (
            "tools/lib/vaws_doctor.py",
            (
                "tools.lib.config.",
                "tools.lib.doctor",
                "tools.lib.vaws_compat",
            ),
        ),
        (
            "tools/lib/vaws_reset.py",
            (
                "tools.lib.config.",
                "tools.lib.reset_cleanup",
                "tools.lib.vaws_compat",
            ),
        ),
    ):
        assert (ROOT / relative_path).exists()
        refs = {ref for ref in _import_refs(relative_path) if ref.startswith("tools.lib.")}
        assert refs
        unexpected = {
            ref for ref in refs if not any(ref.startswith(prefix) for prefix in allowed_prefixes)
        }
        assert unexpected == set()


def test_machine_adapter_imports_only_public_machine_runtime_surfaces():
    relative_path = "tools/lib/vaws_machine.py"
    assert (ROOT / relative_path).exists()
    refs = {ref for ref in _import_refs(relative_path) if ref.startswith("tools.lib.")}
    allowed_prefixes = (
        "tools.lib.config.",
        "tools.lib.machine_registry",
        "tools.lib.runtime_bootstrap",
        "tools.lib.runtime_cleanup",
        "tools.lib.vaws_compat",
    )
    unexpected = {ref for ref in refs if not any(ref.startswith(prefix) for prefix in allowed_prefixes)}
    assert unexpected == set()
    assert not any(ref.startswith("tools.lib.remote._") for ref in refs)
    assert not any(ref == "tools.lib.machine" or ref.startswith("tools.lib.machine.") for ref in refs)
    assert not any(ref == "tools.lib.remote.resolve_server_context" for ref in refs)


def test_serving_and_benchmark_adapters_import_helpers_not_cli_wrappers():
    for relative_path, allowed_prefixes, forbidden_prefixes in (
        (
            "tools/lib/vaws_serving.py",
            (
                "tools.lib.config.",
                "tools.lib.serving_lifecycle",
                "tools.lib.serving_session",
                "tools.lib.vaws_compat",
            ),
            (
                "tools.atomic.",
                "tools.lib.serving.",
                "tools.lib.benchmark.",
            ),
        ),
        (
            "tools/lib/vaws_benchmark.py",
            (
                "tools.lib.benchmark_execution",
                "tools.lib.config.",
                "tools.lib.serving_session",
                "tools.lib.vaws_compat",
            ),
            (
                "tools.atomic.",
                "tools.lib.benchmark.",
                "tools.lib.serving_lifecycle",
            ),
        ),
    ):
        assert (ROOT / relative_path).exists()
        refs = {ref for ref in _import_refs(relative_path) if ref.startswith("tools.")}
        unexpected = {ref for ref in refs if ref.startswith("tools.lib.") and not any(ref.startswith(prefix) for prefix in allowed_prefixes)}
        assert unexpected == set()
        assert not any(ref.startswith(prefix) for ref in refs for prefix in forbidden_prefixes)


def test_benchmark_adapter_no_longer_mentions_weights_path():
    source = (ROOT / "tools/lib/vaws_benchmark.py").read_text(encoding="utf-8")
    assert "weights_path" not in source
