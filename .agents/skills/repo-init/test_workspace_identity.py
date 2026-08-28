#!/usr/bin/env python3

from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
LIB_DIR = ROOT / ".agents" / "lib"
REPO_INIT_SCRIPTS = ROOT / ".agents" / "skills" / "repo-init" / "scripts"
for value in (str(LIB_DIR), str(REPO_INIT_SCRIPTS)):
    if value not in sys.path:
        sys.path.insert(0, value)

from _profile_choice_common import fixed_workspace_alias_question  # noqa: E402
from vaws_local_state import (  # noqa: E402
    WorkspaceStateError,
    effective_workspace_alias,
    ensure_workspace_identity,
    set_workspace_alias,
    workspace_identity_summary,
)


class WorkspaceIdentityTests(unittest.TestCase):
    def identity_path(self, temp_dir: str) -> Path:
        return Path(temp_dir) / ".vaws-local" / "workspace-identity.json"

    def test_ensure_silently_creates_one_persistent_uuid4(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.identity_path(temp_dir)
            first, first_action = ensure_workspace_identity(path=path)
            second, second_action = ensure_workspace_identity(path=path)
            self.assertEqual(first_action, "created")
            self.assertEqual(second_action, "existing")
            self.assertEqual(first["agent_id"], second["agent_id"])
            self.assertEqual(uuid.UUID(first["agent_id"]).version, 4)
            self.assertEqual(first["alias_decision"], "pending")
            self.assertTrue(workspace_identity_summary(path)["alias_choice_required"])

    def test_concurrent_ensure_returns_one_shared_uuid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.identity_path(temp_dir)
            results: list[str] = []
            errors: list[BaseException] = []

            def create() -> None:
                try:
                    identity, _ = ensure_workspace_identity(path=path)
                    results.append(identity["agent_id"])
                except BaseException as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [threading.Thread(target=create) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(errors, [])
            self.assertEqual(len(set(results)), 1)

    def test_alias_set_and_decline_are_persisted_decisions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.identity_path(temp_dir)
            identity, _ = set_workspace_alias("Team42", path=path)
            self.assertEqual(identity["alias"], "team42")
            self.assertEqual(effective_workspace_alias(path), "team42")
            self.assertFalse(workspace_identity_summary(path)["alias_choice_required"])

            identity, _ = set_workspace_alias(None, path=path, declined=True)
            self.assertIsNone(identity["alias"])
            self.assertEqual(identity["alias_decision"], "declined")
            self.assertIsNone(effective_workspace_alias(path))
            self.assertFalse(workspace_identity_summary(path)["alias_choice_required"])

    def test_alias_rejects_resource_unsafe_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(WorkspaceStateError):
                set_workspace_alias("team-name", path=self.identity_path(temp_dir))

    def test_init_question_has_machine_custom_and_none_choices(self) -> None:
        question = fixed_workspace_alias_question("agent12345")
        self.assertEqual(
            [option["id"] for option in question["options"]],
            ["machine-username", "custom", "none"],
        )


if __name__ == "__main__":
    unittest.main()
