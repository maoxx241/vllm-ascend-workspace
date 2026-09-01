"""Skill contract: wrapper CLIs must accept exactly the arguments
documented in SKILL.md / acceptance.md.

A change to the CLI surface breaks the agent's call site, so we lock it
down with a simple parser-introspection test rather than running the
full pipeline.
"""
from __future__ import annotations

import subprocess
from types import SimpleNamespace
from unittest import mock

import conftest  # noqa: F401

import _common as common
import profile_analyze
import profile_sweep


def _option_set(parser) -> set[str]:
    flags: set[str] = set()
    for action in parser._actions:  # type: ignore[attr-defined]
        for s in action.option_strings:
            flags.add(s)
    return flags


def test_analyze_wrapper_has_required_args() -> None:
    parser = profile_analyze._build_parser()
    opts = _option_set(parser)
    for required in (
        "--session-id",
        "--session-file",
        "--manifest",
        "--remote-profile-root",
        "--tag",
        "--remote-work-dir",
        "--remote-output-dir",
        "--local-output-dir",
        "--overwrite",
        "--keep-remote-output",
        "--remote-timeout",
        "--skip-html",
        "--report-mode",
        "--from-stage",
        "--to-stage",
        "--only-stage",
        "--verbose",
    ):
        assert required in opts, f"profile_analyze missing flag: {required}"


def test_analyze_wrapper_input_is_mutually_exclusive() -> None:
    """--manifest / --remote-profile-root must be mutually exclusive."""
    parser = profile_analyze._build_parser()
    groups = [g for g in parser._mutually_exclusive_groups]  # type: ignore[attr-defined]
    assert groups, "profile_analyze should have a mutually exclusive input group"
    flags = {s for action in groups[0]._group_actions for s in action.option_strings}
    assert "--manifest" in flags and "--remote-profile-root" in flags


def test_sweep_wrapper_has_required_args() -> None:
    parser = profile_sweep._build_parser()
    opts = _option_set(parser)
    for required in (
        "--session-id",
        "--session-file",
        "--search-root",
        "--tag",
        "--limit",
        "--remote-work-dir",
        "--remote-timeout",
        "--keep-remote-output",
        "--jobs",
        "--reuse-existing",
        "--pull-html",
        "--render-html",
        "--report-mode",
        "--local-output-dir",
        "--overwrite",
        "--verbose",
    ):
        assert required in opts, f"profile_sweep missing flag: {required}"


def _report_mode_choices(parser) -> tuple[str, ...]:
    for action in parser._actions:  # type: ignore[attr-defined]
        if "--report-mode" in action.option_strings:
            return tuple(action.choices or ())
    return ()


def test_report_mode_choices_only_summary_and_full_raw() -> None:
    """Regression: the deprecated 'interactive' mode must stay gone.

    'interactive' rendered HTML without raw kernel rows, which gutted the
    operator cards. We collapsed report-mode to just ``summary`` (first-
    stage debug) and ``full-raw`` (canonical analysis output).
    """
    analyze_choices = _report_mode_choices(profile_analyze._build_parser())
    sweep_choices = _report_mode_choices(profile_sweep._build_parser())

    assert set(analyze_choices) == {"summary", "full-raw"}, (
        f"profile_analyze --report-mode choices drifted: {analyze_choices}"
    )
    assert set(sweep_choices) == {"summary", "full-raw"}, (
        f"profile_sweep --report-mode choices drifted: {sweep_choices}"
    )
    assert "interactive" not in analyze_choices
    assert "interactive" not in sweep_choices


def test_required_remote_python_dependency_fails_closed() -> None:
    with mock.patch.object(
        common,
        "ssh_exec",
        return_value=SimpleNamespace(returncode=0, stdout="", stderr=""),
    ):
        try:
            common.remote_python_with_module(object(), "yaml", required=True)
        except RuntimeError as exc:
            assert "required module 'yaml'" in str(exc)
        else:
            raise AssertionError("required remote dependency probe must fail closed")


def test_required_remote_python_dependency_selects_importable_interpreter() -> None:
    results = iter(
        [
            SimpleNamespace(returncode=0, stdout="", stderr=""),
            SimpleNamespace(returncode=0, stdout="OK\n", stderr=""),
        ]
    )
    with mock.patch.object(common, "ssh_exec", side_effect=lambda *a, **k: next(results)):
        py = common.remote_python_with_module(object(), "yaml", required=True)
    assert py == "/usr/local/python3.10/bin/python3"


def test_remote_python_probe_timeout_required_fails_closed() -> None:
    with mock.patch.object(
        common,
        "ssh_exec",
        side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30),
    ):
        try:
            common.remote_python_with_module(object(), "yaml", required=True)
        except RuntimeError as exc:
            assert "timed out" in str(exc)
            assert "'yaml'" in str(exc)
        else:
            raise AssertionError("required probe timeout must raise RuntimeError")


def test_remote_python_probe_timeout_optional_falls_back_to_python3() -> None:
    with mock.patch.object(
        common,
        "ssh_exec",
        side_effect=subprocess.TimeoutExpired(cmd="ssh", timeout=30),
    ):
        py = common.remote_python_with_module(object(), "yaml", required=False)
    assert py == "python3"


def test_ssh_base_cmd_sets_default_connect_timeout() -> None:
    endpoint = common.SshEndpoint(host="example.internal", port=22, user="root")
    cmd = " ".join(common._ssh_base_cmd(endpoint))
    assert f"ConnectTimeout={common.SSH_CONNECT_TIMEOUT_SECONDS}" in cmd


def test_all_stage_parsers_disable_abbrev() -> None:
    """acceptance.md: every argparse parser must set allow_abbrev=False."""
    from ascend_profile import (
        analyze,
        classify,
        cross_rank,
        diagnostics,
        normalize,
        report,
        segment,
        summarize,
        sweep,
    )

    stage_modules = (
        analyze,
        classify,
        cross_rank,
        diagnostics,
        normalize,
        report,
        segment,
        summarize,
        sweep,
    )
    for module in stage_modules:
        parser = module.build_parser()
        assert parser.allow_abbrev is False, f"{module.__name__} parser must set allow_abbrev=False"
    for wrapper in (profile_analyze, profile_sweep):
        assert wrapper._build_parser().allow_abbrev is False


if __name__ == "__main__":
    test_analyze_wrapper_has_required_args()
    test_analyze_wrapper_input_is_mutually_exclusive()
    test_sweep_wrapper_has_required_args()
    test_report_mode_choices_only_summary_and_full_raw()
    test_required_remote_python_dependency_fails_closed()
    test_required_remote_python_dependency_selects_importable_interpreter()
    test_remote_python_probe_timeout_required_fails_closed()
    test_remote_python_probe_timeout_optional_falls_back_to_python3()
    test_ssh_base_cmd_sets_default_connect_timeout()
    test_all_stage_parsers_disable_abbrev()
    print("ok")
