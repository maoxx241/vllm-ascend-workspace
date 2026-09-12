from pathlib import Path
import sys
import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))

from vaws_session_state import SessionStateError, load_serving_state, require_task_id, save_serving_state


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


def test_task_report_ids_are_exact_and_legacy_single_service_file_is_ignored(tmp_path):
    task = "vaws-task-a"
    old = tmp_path / ".vaws-local/tasks" / task / "serving.json"
    old.parent.mkdir(parents=True)
    old.write_text('{"service":"vllm","model":"old-model"}', encoding="utf-8")
    assert load_serving_state(task, repo_root=tmp_path) is None
    save_serving_state(task, {"service": "vllm", "model": "current-model"}, repo_root=tmp_path)
    for other in ("VAWS-TASK-A", "vaws/task-a", "vaws task-a", " vaws-task-a"):
        with pytest.raises(SessionStateError):
            load_serving_state(other, repo_root=tmp_path)
    assert load_serving_state(task, repo_root=tmp_path)["model"] == "current-model"


def test_windows_filename_normalization_cannot_alias_an_existing_task(tmp_path):
    save_serving_state("task", {"service": "vllm", "model": "current-model"}, repo_root=tmp_path)
    for identifier in ("task.", "task..", "con", "prn", "aux.json", "nul", "com1", "com9.txt", "lpt1", "lpt9.log"):
        with pytest.raises(SessionStateError):
            load_serving_state(identifier, repo_root=tmp_path)
    assert require_task_id("task.name") == "task.name"
    assert load_serving_state("task", repo_root=tmp_path)["model"] == "current-model"
