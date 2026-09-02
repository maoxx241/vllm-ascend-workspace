from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
for _path in (str(ROOT), str(REPO_ROOT / ".agents" / "lib")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import core.vaws_ops as vaws_ops  # noqa: E402
import vaws_task_client  # noqa: E402


class FakeTaskClient:
    state = "finishing"

    def __init__(self, *_args, **_kwargs):
        self.context = {"session": {"id": "sess-test"}}

    def finish(self, _force=False):
        return {"state": self.state, "executions": [], "worktrees_preserved": True}


class VawsOpsTests(unittest.TestCase):
    def test_finish_non_terminal_state_is_blocked_not_success(self) -> None:
        # A finish that leaves the session in "finishing" (executions still
        # stopping) must not report success (D7).
        with mock.patch.object(vaws_task_client, "TaskClient", FakeTaskClient):
            payload = vaws_ops.vaws_call("vaws.finish", {})
        self.assertEqual(payload["result"]["status"], "finishing")
        self.assertEqual(payload["result"]["outcome"], "blocked")

    def test_finish_terminal_state_stays_success(self) -> None:
        class DoneClient(FakeTaskClient):
            state = "finished"

        with mock.patch.object(vaws_task_client, "TaskClient", DoneClient):
            payload = vaws_ops.vaws_call("vaws.finish", {})
        self.assertEqual(payload["result"]["status"], "finished")
        self.assertEqual(payload["result"]["outcome"], "success")


if __name__ == "__main__":
    unittest.main()
