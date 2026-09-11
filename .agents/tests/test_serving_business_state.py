from pathlib import Path
import sys

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

from vaws_session_state import load_serving_state, save_serving_state


def test_service_settings_do_not_cross_names_or_escape_task_root(tmp_path):
    task = "vaws-business-test"
    first = {"service": "a/b", "model": "model-one", "tp": 1}
    second = {"service": "a-b", "model": "model-two", "tp": 2}
    paths = [save_serving_state(task, row, repo_root=tmp_path) for row in (first, second)]
    assert paths[0] != paths[1]
    assert all(path.is_relative_to(tmp_path / ".vaws-local" / "tasks" / task) for path in paths)
    assert load_serving_state(task, service="a/b", repo_root=tmp_path) == first
    assert load_serving_state(task, service="a-b", repo_root=tmp_path) == second
    assert load_serving_state(task, service="missing", repo_root=tmp_path) is None
