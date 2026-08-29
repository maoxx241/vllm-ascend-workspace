from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from npu_fleet_monitor.db import Database
from npu_fleet_monitor.scheduler import AdaptiveScheduler
from npu_fleet_monitor.settings import Settings


class SchedulerTests(unittest.TestCase):
    def test_fastest_visible_viewer_controls_interval(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            settings = Settings(Path(state), Path(state), "127.0.0.1", 8789, 120, 30, 60, 90, 4, 12, 8192)
            db = Database(Path(state) / "test.sqlite3")
            db.initialize()
            scheduler = AdaptiveScheduler(settings, db, object())  # type: ignore[arg-type]
            self.assertEqual(scheduler.effective_interval(), 120)
            scheduler.heartbeat("viewer_00000001", 10, True)
            scheduler.heartbeat("viewer_00000002", 1, True)
            self.assertEqual(scheduler.effective_interval(), 1)
            scheduler.remove_lease("viewer_00000002")
            self.assertEqual(scheduler.effective_interval(), 10)
            scheduler.heartbeat("viewer_00000001", 10, False)
            self.assertEqual(scheduler.effective_interval(), 120)

    def test_live_query_waits_for_a_new_snapshot_and_requests_infrastructure(self) -> None:
        with tempfile.TemporaryDirectory() as state:
            settings = Settings(Path(state), Path(state), "127.0.0.1", 8789, 120, 30, 60, 90, 4, 12, 8192)
            db = Database(Path(state) / "test.sqlite3")
            db.initialize()
            scheduler = AdaptiveScheduler(settings, db, object())  # type: ignore[arg-type]
            scheduler._snapshots["server-1"] = {"collected_at": 1}  # noqa: SLF001
            result = {}

            def wait_for_snapshot() -> None:
                result.update(scheduler.collect_and_wait("server-1", force_infrastructure=True, timeout=2))

            thread = threading.Thread(target=wait_for_snapshot)
            thread.start()
            time.sleep(0.02)
            with scheduler._condition:  # noqa: SLF001
                self.assertIn("server-1", scheduler._manual)  # noqa: SLF001
                self.assertIn("server-1", scheduler._force_infrastructure)  # noqa: SLF001
                scheduler._snapshots["server-1"] = {"collected_at": 2, "status": "online"}  # noqa: SLF001
                scheduler._condition.notify_all()  # noqa: SLF001
            thread.join(timeout=1)
            self.assertEqual(result["collected_at"], 2)


if __name__ == "__main__":
    unittest.main()
