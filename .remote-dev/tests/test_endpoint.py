from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.endpoint import Endpoint, resolve_endpoint  # noqa: E402


class EndpointTests(unittest.TestCase):
    def test_endpoint_id_is_stable_and_redacts_from_state_path(self) -> None:
        endpoint = Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace")
        self.assertEqual(endpoint.endpoint_id, Endpoint(host="1.2.3.4", port=46000, root="/vllm-workspace").endpoint_id)
        self.assertEqual(len(endpoint.endpoint_id), 16)
        self.assertNotIn("1.2.3.4", endpoint.endpoint_id)

    def test_direct_endpoint_defaults(self) -> None:
        endpoint = resolve_endpoint({"host": "1.2.3.4", "port": 46000})
        self.assertEqual(endpoint.user, "root")
        self.assertEqual(endpoint.root, "/")
        self.assertEqual(endpoint.effective_cwd, "/vllm-workspace")


if __name__ == "__main__":
    unittest.main()
