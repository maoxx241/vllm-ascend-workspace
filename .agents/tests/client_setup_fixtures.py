"""Selected-runtime boundaries for configuration planning tests.

These tests exercise generated configuration, not environment construction.
Real construction, pinning and process execution have dedicated tests.
"""
import sys

import vaws_knowledge_service


def selected_runtime(monkeypatch, setup, tmp_path):
    receipt = {"key": "d" * 64, "platform": sys.platform,
               "python": sys.executable, "root": sys.prefix,
               "receipt": str(tmp_path / "selected-ready.json")}
    monkeypatch.setattr(setup, "native_ready", lambda root: receipt)
    monkeypatch.setattr(setup, "windows_ready", lambda root: receipt)
    monkeypatch.setattr(setup, "managed_receipt", lambda root: receipt)
    monkeypatch.setattr(setup, "managed_python", lambda: sys.executable)
    monkeypatch.setattr(setup, "windows_mounted_workspace", lambda root: False)
    monkeypatch.setattr(vaws_knowledge_service, "managed_receipt", lambda root: receipt)
    monkeypatch.setattr(vaws_knowledge_service, "managed_python", lambda root: sys.executable)
    monkeypatch.setattr(vaws_knowledge_service, "windows_mounted_workspace", lambda root: False)
    return receipt
