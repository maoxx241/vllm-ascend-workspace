#!/usr/bin/env python3

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
LIB_DIR = ROOT / ".agents" / "lib"
SCRIPTS = ROOT / ".agents" / "skills" / "machine-management" / "scripts"
for value in (str(LIB_DIR), str(SCRIPTS)):
    if value not in sys.path:
        sys.path.insert(0, value)

import _workflow_common as workflow  # noqa: E402
import manage_machine as machine_ops  # noqa: E402


def discover(images: list[dict[str, object]], machine_type: str | None) -> dict[str, object]:
    namespace: dict[str, object] = {}
    exec(machine_ops.LOCAL_IMAGE_DISCOVERY_PY, namespace)  # noqa: S102
    selector = namespace["select_latest_local_vllm_ascend_image"]
    assert callable(selector)
    return selector(images, machine_type)


class LocalLatestImageTests(unittest.TestCase):
    def test_selector_defers_resolution_to_remote_docker_host(self) -> None:
        resolution = machine_ops.resolve_image_request("local-latest", machine_type="A3")

        self.assertEqual(resolution.selector, machine_ops.IMAGE_SELECTOR_LOCAL_LATEST)
        self.assertEqual(resolution.policy, machine_ops.IMAGE_POLICY_LOCAL_LATEST)
        self.assertEqual(resolution.candidates, ())
        self.assertEqual(resolution.mirror_order, ())
        self.assertEqual(resolution.machine_type, "A3")

    def test_discovery_filters_repository_and_machine_type_then_uses_created_time(self) -> None:
        images = [
            {
                "Id": "sha256:unrelated",
                "Created": "2026-08-27T13:00:00Z",
                "RepoTags": ["example.invalid/other:main-a3"],
                "RepoDigests": [],
            },
            {
                "Id": "sha256:newer-a2",
                "Created": "2026-08-27T12:00:00Z",
                "RepoTags": ["quay.io/ascend/vllm-ascend:v0.12.0"],
                "RepoDigests": [],
            },
            {
                "Id": "sha256:older-a3",
                "Created": "2026-08-26T12:00:00Z",
                "RepoTags": ["quay.io/ascend/vllm-ascend:v0.11.0-a3"],
                "RepoDigests": [],
            },
            {
                "Id": "sha256:newest-a3",
                "Created": "2026-08-27T11:00:00Z",
                "RepoTags": [
                    "quay.io/ascend/vllm-ascend:latest",
                    "quay.io/ascend/vllm-ascend:v0.12.0-a3",
                ],
                "RepoDigests": ["quay.io/ascend/vllm-ascend@sha256:newest-a3"],
            },
            {
                "Id": "sha256:prefixed-a3",
                "Created": "2026-08-27T12:30:00Z",
                "RepoTags": [
                    "registry.local/team/company-vllm-ascend:v0.13.0-a3",
                ],
                "RepoDigests": [],
            },
            {
                "Id": "sha256:suffixed-a3",
                "Created": "2026-08-25T12:00:00Z",
                "RepoTags": [
                    "registry.local/team/vllm-ascend-dev:v0.10.0-a3",
                ],
                "RepoDigests": [],
            },
        ]

        result = discover(images, "A3")

        self.assertEqual(result["matched_count"], 5)
        self.assertEqual(result["eligible_count"], 4)
        self.assertEqual(result["selected_image_id"], "sha256:prefixed-a3")
        self.assertEqual(
            result["selected_reference"],
            "registry.local/team/company-vllm-ascend:v0.13.0-a3",
        )

    def test_image_choice_payload_exposes_local_latest_option(self) -> None:
        with (
            mock.patch.object(machine_ops, "fetch_latest_prerelease_tag", return_value="v0.12.0rc1"),
            mock.patch.object(machine_ops, "fetch_latest_release_tag", return_value="v0.11.0"),
        ):
            payload = workflow.image_selection_needs_input_payload(reason="choose")

        choices = payload["missing"]["choices"]
        local_choice = next(
            item for item in choices if item["value"] == machine_ops.IMAGE_SELECTOR_LOCAL_LATEST
        )
        self.assertIn("Docker daemon", local_choice["resolution"])

    def test_probe_without_compatible_local_image_blocks_before_bootstrap(self) -> None:
        probe = {
            "image": {
                "policy": machine_ops.IMAGE_POLICY_LOCAL_LATEST,
                "local_discovery": {
                    "matched_count": 2,
                    "eligible_count": 0,
                    "selected_reference": None,
                },
            }
        }

        blocker = workflow.local_image_discovery_blocker(probe)

        assert blocker is not None
        self.assertEqual(blocker["status"], "blocked")
        self.assertEqual(blocker["action"], "image-discovery")

    def test_probe_with_selected_local_image_continues(self) -> None:
        probe = {
            "image": {
                "policy": machine_ops.IMAGE_POLICY_LOCAL_LATEST,
                "local_discovery": {
                    "selected_reference": "quay.io/ascend/vllm-ascend:v0.12.0-a3",
                },
            }
        }

        self.assertIsNone(workflow.local_image_discovery_blocker(probe))

    def test_generated_remote_scripts_are_valid_bash_without_placeholders(self) -> None:
        for script in (
            machine_ops.render_host_probe_script(),
            machine_ops.render_bootstrap_host_script(),
        ):
            self.assertNotIn("__LOCAL_IMAGE_DISCOVERY", script)
            checked = subprocess.run(
                ["bash", "-n"],
                input=script,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(checked.returncode, 0, checked.stderr)

        bootstrap = machine_ops.render_bootstrap_host_script()
        self.assertIn("com.vaws.base_image_id", bootstrap)
        self.assertIn("selected-latest-local-vllm-ascend-image", bootstrap)


if __name__ == "__main__":
    unittest.main()
