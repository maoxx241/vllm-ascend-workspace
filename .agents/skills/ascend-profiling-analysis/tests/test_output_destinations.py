"""Contract + unit tests for --no-pull / --archive-output output destinations.

Same parser-introspection style as test_skill_contract.py: the CLI surface is
locked down without running the pipeline, and the pure / ssh-mocked helpers
behind the two flags are unit-tested locally.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest import mock

import conftest  # noqa: F401

import _common as common
import profile_analyze


def _option_set(parser) -> set[str]:
    flags: set[str] = set()
    for action in parser._actions:  # type: ignore[attr-defined]
        for s in action.option_strings:
            flags.add(s)
    return flags


# ---------------------------------------------------------------------------
# Parser contract
# ---------------------------------------------------------------------------

def test_no_pull_and_archive_output_flags_exist() -> None:
    opts = _option_set(profile_analyze._build_parser())
    assert "--no-pull" in opts, "profile_analyze missing flag: --no-pull"
    assert "--archive-output" in opts, "profile_analyze missing flag: --archive-output"


def test_no_pull_and_archive_output_are_mutually_exclusive() -> None:
    parser = profile_analyze._build_parser()
    try:
        parser.parse_args(
            ["--remote-profile-root", "/x", "--no-pull", "--archive-output", "/y"]
        )
    except SystemExit:
        pass
    else:
        raise AssertionError("--no-pull + --archive-output must be rejected")


def test_no_pull_and_archive_output_parse_individually() -> None:
    parser = profile_analyze._build_parser()
    args = parser.parse_args(["--remote-profile-root", "/x", "--no-pull"])
    assert args.no_pull is True and args.archive_output is None
    args = parser.parse_args(["--remote-profile-root", "/x", "--archive-output", "/y"])
    assert args.no_pull is False and args.archive_output == "/y"
    args = parser.parse_args(["--remote-profile-root", "/x"])
    assert args.no_pull is False and args.archive_output is None


# ---------------------------------------------------------------------------
# _build_output_archive_script (pure)
# ---------------------------------------------------------------------------

def test_build_output_archive_script_shape() -> None:
    script = profile_analyze._build_output_archive_script(
        "/tmp/ascend_profile_framework/runs/20260907_demo",
        "/mnt/weight/m00663269/profiling/analysis",
        "20260907_demo",
    )
    assert script.startswith("set -e; mkdir -p ")
    # Contents-of-src copy form keeps layout flat under <root>/<run-dir-name>
    # (shlex.quote leaves clean paths unquoted).
    assert "cp -r /tmp/ascend_profile_framework/runs/20260907_demo/. " in script
    assert "/mnt/weight/m00663269/profiling/analysis/20260907_demo" in script


def test_build_output_archive_script_strips_trailing_slashes() -> None:
    script = profile_analyze._build_output_archive_script(
        "/out/", "/archive/", "run1",
    )
    assert "/archive/run1" in script
    assert "/archive//run1" not in script


# ---------------------------------------------------------------------------
# _archive_remote_output (ssh mocked)
# ---------------------------------------------------------------------------

def _endpoint() -> common.SshEndpoint:
    return common.SshEndpoint(host="example.internal", port=22, user="root")


def test_archive_remote_output_returns_archived_path_on_success() -> None:
    ok = SimpleNamespace(returncode=0, stdout="", stderr="")
    with mock.patch.object(common, "ssh_exec", return_value=ok):
        dst = profile_analyze._archive_remote_output(
            _endpoint(), "/out/run1", "/archive", "run1",
        )
    assert dst == "/archive/run1"


def test_archive_remote_output_failure_returns_none_and_never_raises() -> None:
    with mock.patch.object(
        common, "ssh_exec", side_effect=RuntimeError("boom")
    ), mock.patch("time.sleep"):
        dst = profile_analyze._archive_remote_output(
            _endpoint(), "/out/run1", "/archive", "run1",
        )
    assert dst is None


# ---------------------------------------------------------------------------
# _read_remote_json / _read_remote_analysis_summary (ssh mocked)
# ---------------------------------------------------------------------------

def test_read_remote_json_parses_dict() -> None:
    ok = SimpleNamespace(returncode=0, stdout=json.dumps({"a": 1}), stderr="")
    with mock.patch.object(common, "ssh_exec", return_value=ok) as mocked:
        data = profile_analyze._read_remote_json(_endpoint(), "/out/report/x.json")
    assert data == {"a": 1}
    command = mocked.call_args[0][1]
    assert command == "cat /out/report/x.json"


def test_read_remote_json_bad_json_returns_none() -> None:
    bad = SimpleNamespace(returncode=0, stdout="not json", stderr="")
    with mock.patch.object(common, "ssh_exec", return_value=bad):
        assert profile_analyze._read_remote_json(_endpoint(), "/out/x.json") is None


def test_read_remote_json_non_dict_returns_none() -> None:
    arr = SimpleNamespace(returncode=0, stdout="[1, 2]", stderr="")
    with mock.patch.object(common, "ssh_exec", return_value=arr):
        assert profile_analyze._read_remote_json(_endpoint(), "/out/x.json") is None


def test_read_remote_json_ssh_failure_returns_none_after_retries() -> None:
    with mock.patch.object(
        common, "ssh_exec", side_effect=RuntimeError("transport down")
    ) as mocked, mock.patch("time.sleep"):
        assert profile_analyze._read_remote_json(_endpoint(), "/out/x.json") is None
    # _ssh_exec_with_retry policy: 3 attempts before giving up.
    assert mocked.call_count == 3


def test_read_remote_analysis_summary_targets_report_subpath() -> None:
    ok = SimpleNamespace(returncode=0, stdout=json.dumps({"schema_version": 1}), stderr="")
    with mock.patch.object(common, "ssh_exec", return_value=ok) as mocked:
        data = profile_analyze._read_remote_analysis_summary(_endpoint(), "/out/run1/")
    assert data == {"schema_version": 1}
    command = mocked.call_args[0][1]
    assert command == "cat /out/run1/report/analysis_summary.json"


# ---------------------------------------------------------------------------
# _diagnosis_counts_from_data (pure)
# ---------------------------------------------------------------------------

def test_diagnosis_counts_from_data_canonical_key() -> None:
    data = {
        "diagnosis_findings": [
            {"confidence": "high"},
            {"confidence": "low"},
            {"confidence": "high"},
        ]
    }
    assert profile_analyze._diagnosis_counts_from_data(data) == {"high": 2, "low": 1}


def test_diagnosis_counts_from_data_legacy_key_fallbacks() -> None:
    assert profile_analyze._diagnosis_counts_from_data(
        {"findings": [{"confidence": "medium"}]}
    ) == {"medium": 1}
    assert profile_analyze._diagnosis_counts_from_data(
        {"claims": [{"confidence": "low"}]}
    ) == {"low": 1}


def test_diagnosis_counts_from_data_empty_and_missing_confidence() -> None:
    assert profile_analyze._diagnosis_counts_from_data({}) == {}
    assert profile_analyze._diagnosis_counts_from_data(
        {"diagnosis_findings": [{}]}
    ) == {"unknown": 1}


if __name__ == "__main__":
    test_no_pull_and_archive_output_flags_exist()
    test_no_pull_and_archive_output_are_mutually_exclusive()
    test_no_pull_and_archive_output_parse_individually()
    test_build_output_archive_script_shape()
    test_build_output_archive_script_strips_trailing_slashes()
    test_archive_remote_output_returns_archived_path_on_success()
    test_archive_remote_output_failure_returns_none_and_never_raises()
    test_read_remote_json_parses_dict()
    test_read_remote_json_bad_json_returns_none()
    test_read_remote_json_non_dict_returns_none()
    test_read_remote_json_ssh_failure_returns_none_after_retries()
    test_read_remote_analysis_summary_targets_report_subpath()
    test_diagnosis_counts_from_data_canonical_key()
    test_diagnosis_counts_from_data_legacy_key_fallbacks()
    test_diagnosis_counts_from_data_empty_and_missing_confidence()
    print("ok")
