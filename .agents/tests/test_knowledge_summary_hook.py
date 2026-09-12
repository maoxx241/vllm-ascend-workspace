"""Automatic capture reuses final text and never prevents normal client work."""
import contextlib
import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("workspace_knowledge_summary", ROOT / ".agents/hooks/knowledge_summary.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


def invoke(payload, *, client="codex", project=ROOT):
    output, errors = io.StringIO(), io.StringIO()
    with mock.patch.object(sys, "argv", ["knowledge_summary.py", "--client", client, "--project", str(project)]), \
         mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
         contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
        code = hook.main()
    assert code == 0
    assert output.getvalue() == "{}\n"
    assert errors.getvalue() == ""


def test_capture_receives_existing_summary_without_an_extra_format():
    payload = {"hook_event_name": "Stop", "cwd": str(ROOT),
               "last_assistant_message": "The run completed. A retry fixed the transient timeout."}
    config = object()
    with mock.patch.object(hook, "ensure_workspace_interpreter"), \
         mock.patch("vaws_knowledge_service.service_config", return_value=config), \
         mock.patch("vaws_knowledge.summary_hook.capture_summary") as capture:
        invoke(payload)
    capture.assert_called_once_with(payload, config=config, client="codex")


def test_capture_error_does_not_interrupt_client():
    with mock.patch.object(hook, "ensure_workspace_interpreter"), \
         mock.patch("vaws_knowledge_service.service_config", side_effect=OSError("config unavailable")):
        invoke({"hook_event_name": "Stop", "cwd": str(ROOT), "last_assistant_message": "Existing final response."})


def test_missing_environment_does_not_interrupt_client():
    def missing(**kwargs):
        print("workspace environment unavailable", file=sys.stderr)
        raise SystemExit(2)

    with mock.patch.object(hook, "ensure_workspace_interpreter", side_effect=missing):
        invoke({"hook_event_name": "Stop"})


def test_foreign_or_unknown_project_does_not_capture(tmp_path):
    for payload in ({"cwd": str(tmp_path)}, {}, {"cwd": "."}):
        with mock.patch.object(hook, "ensure_workspace_interpreter"), \
             mock.patch("vaws_knowledge_service.service_config") as config, \
             mock.patch("vaws_knowledge.summary_hook.capture_summary") as capture:
            invoke(payload)
        config.assert_not_called()
        capture.assert_not_called()


def test_project_subdirectory_is_in_scope(tmp_path):
    child = tmp_path / "child"
    child.mkdir()
    assert hook.in_project({"cwd": str(child)}, client="grok", project=tmp_path)
    assert not hook.in_project({"cwd": str(tmp_path.parent)}, client="grok", project=tmp_path)


def test_cursor_workspace_roots_must_stay_in_scope(tmp_path):
    assert hook.in_project({"workspace_roots": [str(tmp_path)]}, client="cursor", project=tmp_path)
    assert not hook.in_project({"workspace_roots": [str(tmp_path), str(tmp_path.parent)]}, client="cursor", project=tmp_path)
    assert not hook.in_project({"workspace_roots": [str(tmp_path)]}, client="codex", project=tmp_path)
    assert not hook.in_project({"cwd": str(tmp_path.parent), "workspace_roots": [str(tmp_path)]},
                               client="cursor", project=tmp_path)


@pytest.mark.skipif(os.name != "nt", reason="native Windows path interpretation")
def test_windows_summary_owner_accepts_only_its_mounted_wsl_project():
    project = Path(r"D:\work")
    assert hook.in_project({"cwd": "/mnt/d/work/subdir"}, client="grok", project=project)
    assert not hook.in_project({"cwd": "/mnt/d/other"}, client="grok", project=project)
    assert not hook.in_project({"cwd": "/mnt/d/work/../other"}, client="grok", project=project)
    assert not hook.in_project({"workspace_roots": ["/mnt/d/work", "/mnt/d/other"]}, client="cursor", project=project)
