from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.lib.service_manifest import parse_service_manifest_lines, remote_service_manifest_path


def test_remote_service_manifest_path_uses_runtime_artifacts_dir():
    assert remote_service_manifest_path("/vllm-workspace", "svc-123") == "/vllm-workspace/artifacts/services/svc-123.json"


def test_parse_service_manifest_lines_preserves_manifest_payload():
    payloads = parse_service_manifest_lines(
        "\n".join(
            [
                '{"service_id":"svc-123","server_name":"lab-a","manifest_path":"/vllm-workspace/artifacts/services/svc-123.json"}',
                '{"service_id":"svc-456","server_name":"lab-b","manifest_path":"/vllm-workspace/artifacts/services/svc-456.json"}',
            ]
        )
    )

    assert [payload["service_id"] for payload in payloads] == ["svc-123", "svc-456"]
    assert payloads[0]["manifest_path"] == "/vllm-workspace/artifacts/services/svc-123.json"
