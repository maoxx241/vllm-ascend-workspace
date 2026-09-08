#!/usr/bin/env python3
"""Check entry points against the Result Envelope v1 contract.

Three modes:

* ``scan``  — static survey of the tree's entry points and the current
  conformance rate. Heuristic by construction: it reads source, it does not
  run anything, and every heuristic is named in the output so a reader can
  discount it.
* ``check`` — validate an envelope payload from a file or ``stdin``.
* ``run``   — execute a command and verify the runtime contract: exactly one
  valid envelope on ``stdout``, progress confined to ``stderr``, and an exit
  code that agrees with the envelope.

This script follows the convention it enforces: progress on ``stderr``, one
envelope on ``stdout``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

ROOT = Path(__file__).resolve().parents[2]
LIB_DIR = ROOT / ".agents" / "lib"
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

from vaws_venv import ensure_workspace_interpreter  # noqa: E402

ensure_workspace_interpreter(repo_root=ROOT)


from vaws_result_envelope import (  # noqa: E402
    EnvelopeError,
    emit,
    make_attempt,
    make_command,
    make_environment,
    make_evidence,
    make_failure,
    make_next_step,
    make_operation,
    new_envelope,
    progress,
    utc_now,
    validate_envelope,
)

#: Directories that hold shared libraries and data rather than agent-facing
#: entry points. Excluding them keeps the conformance denominator honest.
EXCLUDED_DIR_PARTS = frozenset(
    {"tests", "lib", "knowledge", "schemas", "references", "presets", "agents"}
)
ENTRY_POINT_GUARD_RE = re.compile(r"^if\s+__name__\s*==\s*[\"']__main__[\"']", re.M)

#: Static checks. ``required`` members define conformance; the others are
#: reported so a migration can be sequenced by what is already close.
CHECKS: tuple[tuple[str, bool, str], ...] = (
    ("envelope_library", True, "imports .agents/lib/vaws_result_envelope.py"),
    ("stdout_json", True, "emits a JSON payload on stdout"),
    ("stderr_progress", True, "writes progress to stderr"),
    ("layer_attribution", True, "populates a failure layer on failure"),
    ("reproduce_command", False, "records the exact command for re-running"),
    ("environment_identity", False, "records soc/cann/driver/torch versions"),
    ("evidence_refs", False, "points at logs and artifacts by reference"),
    ("next_step", False, "states the next diagnostic step"),
    ("run_correlation", False, "carries a Run Manifest correlation id"),
)
REQUIRED_CHECKS = tuple(name for name, required, _ in CHECKS if required)
ALL_CHECKS = tuple(name for name, _, _ in CHECKS)

_ENVELOPE_IMPORT_RE = re.compile(r"\bvaws_result_envelope\b")
_LAYER_RE = re.compile(
    r"make_failure|unknown_failure|escalate_child_layer|[\"']layer[\"']\s*:"
)
_PROGRESS_RE = re.compile(
    r"sys\.stderr\.write|file\s*=\s*sys\.stderr|emit_progress\s*\(|progress\s*\("
)
_STDOUT_JSON_RE = re.compile(
    r"print_json\s*\(|print\s*\(\s*json\.dumps|print\s*\(\s*json_dump|"
    r"sys\.stdout\.write\s*\(\s*json|\bemit\s*\("
)
_REPRODUCE_RE = re.compile(r"make_attempt|[\"']reproduce[\"']|make_command")
_ENV_IDENTITY_RE = re.compile(
    r"make_environment|[\"']torch_npu[\"']|[\"']vllm_ascend[\"']"
)
_EVIDENCE_RE = re.compile(
    r"make_evidence|evidence_ref|[\"']artifacts[\"']\s*:|[\"']logs[\"']\s*:"
)
_NEXT_STEP_RE = re.compile(
    r"make_next_step|[\"']next_step[\"']|[\"']next_actions[\"']|[\"']do_not[\"']"
)
_RUN_ID_RE = re.compile(r"vaws_run_manifest|[\"']run_id[\"']|[\"']parent_run_id[\"']")


def repo_relative(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return path.name


def discover_entry_points(root: Path) -> list[Path]:
    """Python files with a ``__main__`` guard, excluding test modules.

    Test modules are excluded because they are invoked by the test runner and
    are not part of the agent-facing surface.
    """
    found: list[Path] = []
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).parts
        if set(relative[:-1]) & EXCLUDED_DIR_PARTS:
            continue
        if path.name.startswith("test_"):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if ENTRY_POINT_GUARD_RE.search(source):
            found.append(path)
    return found


def _plain_print_lines(source: str) -> list[int]:
    """Line numbers of ``print()`` calls that clearly do not emit JSON.

    A bare ``print("phase: starting")`` is the single most common way a script
    corrupts ``stdout`` for a JSON consumer, so it is worth naming precisely
    rather than folding into a text heuristic.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "print"):
            continue
        if any(
            isinstance(keyword.value, ast.Attribute)
            and keyword.arg == "file"
            and keyword.value.attr == "stderr"
            for keyword in node.keywords
        ):
            continue
        if not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Call):
            continue
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            lines.append(node.lineno)
        elif isinstance(first, ast.JoinedStr):
            lines.append(node.lineno)
    return lines


def scan_file(path: Path) -> dict[str, Any]:
    source = path.read_text(encoding="utf-8", errors="replace")
    checks = {
        "envelope_library": bool(_ENVELOPE_IMPORT_RE.search(source)),
        "stdout_json": bool(_STDOUT_JSON_RE.search(source)),
        "stderr_progress": bool(_PROGRESS_RE.search(source)),
        "layer_attribution": bool(_LAYER_RE.search(source)),
        "reproduce_command": bool(_REPRODUCE_RE.search(source)),
        "environment_identity": bool(_ENV_IDENTITY_RE.search(source)),
        "evidence_refs": bool(_EVIDENCE_RE.search(source)),
        "next_step": bool(_NEXT_STEP_RE.search(source)),
        "run_correlation": bool(_RUN_ID_RE.search(source)),
    }
    plain_prints = _plain_print_lines(source)
    return {
        "entry_point": repo_relative(path),
        "checks": checks,
        "required_passed": sum(1 for name in REQUIRED_CHECKS if checks[name]),
        "conformant": all(checks[name] for name in REQUIRED_CHECKS),
        "stdout_purity_risk_lines": plain_prints[:20],
        "stdout_purity_risk": bool(plain_prints),
    }


def scan_tree(root: Path) -> dict[str, Any]:
    entry_points = discover_entry_points(root)
    progress("scan", f"scanning {len(entry_points)} entry points")
    results = [scan_file(path) for path in entry_points]
    total = len(results) or 1
    per_check = {
        name: sum(1 for item in results if item["checks"][name])
        for name in ALL_CHECKS
    }
    conformant = [item["entry_point"] for item in results if item["conformant"]]
    near_miss = sorted(
        (
            item
            for item in results
            if not item["conformant"] and item["required_passed"] >= 2
        ),
        key=lambda item: (-item["required_passed"], item["entry_point"]),
    )
    return {
        "root": repo_relative(root),
        "entry_points": len(results),
        "conformant": len(conformant),
        "conformance_rate": round(len(conformant) / total, 4),
        "conformant_entry_points": conformant,
        "required_checks": list(REQUIRED_CHECKS),
        "per_check_passed": per_check,
        "per_check_rate": {
            name: round(count / total, 4) for name, count in per_check.items()
        },
        "stdout_purity_risk": sum(
            1 for item in results if item["stdout_purity_risk"]
        ),
        "near_miss": [
            {
                "entry_point": item["entry_point"],
                "required_passed": item["required_passed"],
                "missing": [
                    name for name in REQUIRED_CHECKS if not item["checks"][name]
                ],
            }
            for item in near_miss[:20]
        ],
        "results": results,
    }


def check_payload(text: str) -> dict[str, Any]:
    """Validate one envelope payload; never raise on bad input."""
    findings: list[str] = []
    payload: Any = None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        return {
            "valid": False,
            "findings": [f"stdout is not a single JSON document: {exc}"],
            "envelope_id": None,
            "outcome": None,
            "layer": None,
        }
    if not isinstance(payload, dict):
        findings.append("envelope root is not a JSON object")
    else:
        try:
            validate_envelope(payload)
        except EnvelopeError as exc:
            findings.extend(str(exc).split("; "))
    failure = payload.get("failure") if isinstance(payload, dict) else None
    return {
        "valid": not findings,
        "findings": findings,
        "envelope_id": payload.get("envelope_id") if isinstance(payload, dict) else None,
        "outcome": payload.get("outcome") if isinstance(payload, dict) else None,
        "layer": (failure or {}).get("layer") if isinstance(failure, dict) else None,
    }


def run_and_check(argv: Sequence[str], *, timeout: float | None) -> dict[str, Any]:
    progress("run", f"executing {argv[0]}")
    started = time.monotonic()
    try:
        completed = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        return {
            "valid": False,
            "findings": [f"command did not finish within {timeout}s"],
            "exit_code": None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout_bytes": 0,
            "stderr_bytes": 0,
        }
    except OSError as exc:
        return {
            "valid": False,
            "findings": [f"command could not be launched: {exc}"],
            "exit_code": None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "stdout_bytes": 0,
            "stderr_bytes": 0,
        }
    report = check_payload(completed.stdout)
    findings = list(report["findings"])
    if completed.stdout.count("\n") and completed.stdout.strip().count("}\n{"):
        findings.append("stdout carries more than one JSON document")
    if "__VAWS_" in completed.stdout:
        findings.append("progress sentinel found on stdout; progress belongs on stderr")
    envelope_exit = None
    try:
        envelope_exit = json.loads(completed.stdout).get("exit_code")
    except (json.JSONDecodeError, AttributeError):
        pass
    if isinstance(envelope_exit, int) and envelope_exit != completed.returncode:
        findings.append(
            f"process exit code {completed.returncode} disagrees with "
            f"envelope exit_code {envelope_exit}"
        )
    return {
        "valid": not findings,
        "findings": findings,
        "exit_code": completed.returncode,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "stdout_bytes": len(completed.stdout.encode("utf-8", errors="replace")),
        "stderr_bytes": len(completed.stderr.encode("utf-8", errors="replace")),
        "envelope_id": report["envelope_id"],
        "outcome": report["outcome"],
        "layer": report["layer"],
    }


def _self_envelope(
    *,
    action: str,
    argv: Sequence[str],
    report: dict[str, Any],
    ok: bool,
    summary: str,
    findings: Iterable[str],
    started_at: str,
    duration_ms: int,
    next_actions: Sequence[str],
) -> dict[str, Any]:
    command = make_command(argv=argv, cwd=".")
    failure = None
    outcome = "success"
    if not ok:
        finding_list = list(findings)
        outcome = "failure"
        reason_code = "envelope_nonconformant"
        if any("not a single JSON document" in item for item in finding_list):
            reason_code = "non_json_output"
        elif action == "scan":
            reason_code = "conformance_below_gate"
        failure = make_failure(
            layer="tool",
            reason_code=reason_code,
            message=summary,
            attribution_basis=[
                "the linted target is a local wrapper and its output contract "
                "is what failed",
                *finding_list[:8],
            ],
            confidence="high",
            ruled_out=["transport", "remote_env", "remote_workload", "device"],
        )
    return new_envelope(
        operation=make_operation(
            entry_point=".agents/scripts/envelope_lint.py",
            action=action,
            skill=None,
            target_kind="local",
            target_id=report.get("root") or (list(argv)[-1] if argv else None),
        ),
        outcome=outcome,
        summary=summary,
        attempt=make_attempt(
            command=command,
            reproduce=command["display"],
            started_at=started_at,
            duration_ms=duration_ms,
        ),
        environment=make_environment(source="unknown"),
        evidence=make_evidence(),
        next_step=make_next_step(actions=list(next_actions) or ["no action needed"]),
        failure=failure,
        extensions={"report": report},
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    scan = sub.add_parser("scan", help="static conformance survey of a tree")
    scan.add_argument("--root", default=str(ROOT / ".agents"))
    scan.add_argument(
        "--details",
        action="store_true",
        help="include the per-entry-point table in extensions.report.results",
    )
    scan.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="exit non-zero when the conformance rate is below this fraction",
    )

    check = sub.add_parser("check", help="validate an envelope payload")
    check.add_argument(
        "--payload-file",
        default="-",
        help="path to a JSON envelope, or '-' for stdin",
    )

    run = sub.add_parser("run", help="run a command and check its envelope")
    run.add_argument("--timeout", type=float, default=300.0)
    run.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def child_command(raw: Sequence[str] | None) -> list[str]:
    """Build the child argv, consuming only one optional leading ``--``.

    ``envelope_lint.py run -- cmd ...`` uses a single leading separator to
    end lint options. That token is not part of the child. Every remaining
    token is left unchanged, in order, including a later ``--`` that the
    child itself uses as an option delimiter. The returned list is the
    exact argv used for both execution and the report.
    """
    command = list(raw or ())
    if command and command[0] == "--":
        return command[1:]
    return command


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    args = build_parser().parse_args(raw_argv)
    self_argv = ["python3", ".agents/scripts/envelope_lint.py", *raw_argv]
    started_at = utc_now()
    started = time.monotonic()

    if args.mode == "scan":
        report = scan_tree(Path(args.root).resolve())
        results = report.pop("results")
        if args.details:
            report["results"] = results
        rate = report["conformance_rate"]
        ok = args.fail_under is None or rate >= args.fail_under
        summary = (
            f"{report['conformant']}/{report['entry_points']} entry points "
            f"conform to the envelope contract ({rate:.1%})"
        )
        progress("scan", summary)
        return emit(
            _self_envelope(
                action="scan",
                argv=self_argv,
                report=report,
                ok=ok,
                summary=summary,
                findings=[f"conformance rate {rate} below gate {args.fail_under}"]
                if not ok
                else [],
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                next_actions=[
                    "migrate the near_miss entry points first: they already "
                    "keep progress on stderr and JSON on stdout",
                    "read docs/agent-feedback-contract.md for the migration "
                    "sequence",
                ],
            )
        )

    if args.mode == "check":
        if args.payload_file == "-":
            text = sys.stdin.read()
            source = "stdin"
        else:
            path = Path(args.payload_file)
            try:
                text = path.read_text(encoding="utf-8")
            except OSError as exc:
                text = ""
                progress("check", f"cannot read payload: {exc}")
            source = repo_relative(path)
        report = check_payload(text)
        report["source"] = source
        summary = (
            f"payload from {source} conforms to Result Envelope v1"
            if report["valid"]
            else f"payload from {source} has {len(report['findings'])} violations"
        )
        progress("check", summary)
        return emit(
            _self_envelope(
                action="check",
                argv=self_argv,
                report=report,
                ok=bool(report["valid"]),
                summary=summary,
                findings=report["findings"],
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                next_actions=[
                    "fix the reported fields in the producing entry point",
                ]
                if not report["valid"]
                else [],
            )
        )

    command = child_command(args.command)
    if not command:
        report = {"findings": ["no command given"], "valid": False}
        progress("run", "no command given")
        return emit(
            _self_envelope(
                action="run",
                argv=self_argv,
                report=report,
                ok=False,
                summary="no command given to run",
                findings=report["findings"],
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
                next_actions=["pass the command after 'run --'"],
            )
        )
    report = run_and_check(command, timeout=args.timeout)
    report["command"] = command
    summary = (
        "command satisfies the runtime envelope contract"
        if report["valid"]
        else f"command violates the envelope contract ({len(report['findings'])} findings)"
    )
    progress("run", summary)
    return emit(
        _self_envelope(
            action="run",
            argv=self_argv,
            report=report,
            ok=bool(report["valid"]),
            summary=summary,
            findings=report["findings"],
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1000),
            next_actions=[
                "adopt .agents/lib/vaws_result_envelope.py in the target script",
            ]
            if not report["valid"]
            else [],
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
