from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ONLY_FILES = (
    "tools/lib/host_access.py",
    "tools/lib/runtime_container.py",
    "tools/lib/runtime_transport.py",
    "tools/lib/runtime_bootstrap.py",
    "tools/lib/reset_cleanup.py",
    "tools/atomic/machine_probe_host_ssh.py",
    "tools/atomic/machine_bootstrap_host_ssh.py",
    "tools/atomic/machine_sync_workspace_mirror.py",
    "tools/atomic/runtime_probe_container_transport.py",
    "tools/atomic/runtime_reconcile_container.py",
    "tools/atomic/runtime_bootstrap_container_transport.py",
    "tools/atomic/runtime_cleanup_server.py",
    "tools/lib/code_parity.py",
    "tools/lib/runtime_env.py",
)
FORBIDDEN_SNIPPETS = (
    "tools.lib.remote._",
    "from .remote import _",
    "from tools.lib.remote import _",
    "from .remote import resolve_server_context",
    "from tools.lib.remote import resolve_server_context",
    "from .runtime import",
    "from tools.lib.runtime import",
)


def test_runtime_consumers_do_not_import_private_remote_symbols():
    for relative_path in PUBLIC_ONLY_FILES:
        text = (ROOT / relative_path).read_text(encoding="utf-8")
        for forbidden in FORBIDDEN_SNIPPETS:
            assert forbidden not in text, f"{relative_path} still imports private remote symbol: {forbidden}"


def test_legacy_runtime_workflow_modules_are_deleted():
    assert not (ROOT / "tools/lib/reset.py").exists()
    assert not (ROOT / "tools/lib/init_flow.py").exists()
