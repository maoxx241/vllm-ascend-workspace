from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_IMPORT_SNIPPETS = (
    "from tools.lib.serving import",
    "import tools.lib.serving",
    "from tools.lib.runtime import",
    "import tools.lib.runtime",
    "from tools.lib.capability_state import",
    "import tools.lib.capability_state",
    "from .capability_state import",
    "from .runtime import",
    "tools.lib.remote._",
    "from tools.lib.remote import resolve_server_context",
    "from .remote import resolve_server_context",
)


def _assert_no_forbidden_imports(relative_paths: tuple[str, ...]) -> None:
    for relative_path in relative_paths:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_IMPORT_SNIPPETS:
            assert forbidden not in text, f"{relative_path} imported forbidden dependency: {forbidden}"


def test_serving_helpers_and_atomic_tools_do_not_import_wrappers_or_cached_runtime_state():
    _assert_no_forbidden_imports(
        (
            "tools/lib/serving_session.py",
            "tools/lib/serving_lifecycle.py",
            "tools/atomic/serving_launch_service.py",
            "tools/atomic/serving_probe_readiness.py",
            "tools/atomic/serving_describe_session.py",
            "tools/atomic/serving_list_sessions.py",
            "tools/atomic/serving_stop_service.py",
        )
    )


def test_benchmark_helpers_and_atomic_tools_do_not_import_wrappers_or_cached_runtime_state():
    _assert_no_forbidden_imports(
        (
            "tools/lib/benchmark_execution.py",
            "tools/atomic/benchmark_describe_preset.py",
            "tools/atomic/benchmark_describe_run.py",
            "tools/atomic/benchmark_run_probe.py",
        )
    )
