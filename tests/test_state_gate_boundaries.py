from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _text(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_runtime_state_gate_consumers_do_not_import_runtime_wrapper():
    for relative_path in (
        "tools/lib/runtime_env.py",
        "tools/lib/code_parity.py",
        "tools/lib/reset_cleanup.py",
    ):
        text = _text(relative_path)
        assert "from .runtime import" not in text
        assert "from tools.lib.runtime import" not in text


def test_runtime_state_gate_consumers_do_not_import_remote_context_resolution():
    for relative_path in (
        "tools/lib/runtime_env.py",
        "tools/lib/code_parity.py",
        "tools/lib/serving_lifecycle.py",
        "tools/lib/benchmark_execution.py",
        "tools/atomic/machine_bootstrap_host_ssh.py",
        "tools/atomic/machine_probe_host_ssh.py",
        "tools/atomic/machine_sync_workspace_mirror.py",
        "tools/atomic/runtime_bootstrap_container_transport.py",
        "tools/atomic/runtime_probe_container_transport.py",
        "tools/atomic/runtime_reconcile_container.py",
        "tools/atomic/runtime_cleanup_server.py",
    ):
        text = _text(relative_path)
        assert "from .remote import resolve_server_context" not in text
        assert "from tools.lib.remote import resolve_server_context" not in text


def test_legacy_state_gate_modules_are_deleted():
    assert not (ROOT / "tools/lib/init_flow.py").exists()
    assert not (ROOT / "tools/lib/reset.py").exists()
