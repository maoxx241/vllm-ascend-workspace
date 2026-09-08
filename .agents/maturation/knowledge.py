"""Feed reproducible harness failures back as knowledge candidates.

``knowledge_capture.py`` is treated as a CLI contract: this module builds a
redacted candidate payload, writes it to a file, and invokes the script. It
never imports or edits the capture implementation. If the script rejects the
payload the rejection is reported verbatim so an interface drift is visible.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from .redact import Redactor, contains_address

REPO_ROOT = Path(__file__).resolve().parents[2]
CAPTURE_SCRIPT = REPO_ROOT / ".agents" / "scripts" / "knowledge_capture.py"
OWNER_SKILL = "remote-toolbox"
MIN_REPRODUCTIONS = 2
STALL_GROUP = "endpoint.transport_stall"


def group_failures(trials: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str, str], list[Mapping[str, Any]]]:
    """Group failed trials by (operation, layer, fingerprint)."""
    groups: dict[tuple[str, str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for trial in trials:
        if trial.get("passed"):
            continue
        attribution = trial.get("attribution") or {}
        layer = str(attribution.get("layer") or "unknown")
        # An endpoint-wide stall is one fault regardless of which operations
        # were scheduled while the channel was dead; group it as such.
        op_key = STALL_GROUP if layer == "transport-stall" else str(trial.get("operation_id"))
        key = (op_key, layer, str(attribution.get("fingerprint") or ""))
        groups[key].append(trial)
    return groups


def build_candidates(
    trials: list[Mapping[str, Any]],
    *,
    run_id: str,
    evidence_relative_dir: str,
    redactor: Redactor,
    min_reproductions: int = MIN_REPRODUCTIONS,
) -> list[dict[str, Any]]:
    """One candidate per reproducible (operation, layer, fingerprint) group."""
    totals: dict[str, int] = defaultdict(int)
    per_endpoint_totals: dict[tuple[str, str], int] = defaultdict(int)
    for trial in trials:
        totals[str(trial.get("operation_id"))] += 1
        per_endpoint_totals[(str(trial.get("operation_id")), str(trial.get("endpoint_label")))] += 1
    candidates: list[dict[str, Any]] = []
    for (op_id, layer, fingerprint), failures in sorted(group_failures(trials).items()):
        if len(failures) < min_reproductions:
            continue
        sample = failures[0]
        is_stall = op_id == STALL_GROUP
        affected_ops = sorted({str(t.get("operation_id")) for t in failures})
        n = sum(totals[op] for op in affected_ops) if is_stall else totals[op_id]
        labels = sorted({str(t.get("endpoint_label")) for t in failures})
        kinds = sorted({str(t.get("endpoint_kind")) for t in failures})
        pythons = sorted({str((t.get("environment") or {}).get("remote_python") or "unknown") for t in failures})
        statuses = sorted({str(t.get("status")) for t in failures})
        reason = str((sample.get("attribution") or {}).get("reason") or "")
        step = str((sample.get("attribution") or {}).get("step") or "")
        durations = [int(t.get("duration_ms") or 0) for t in failures]
        all_fail = len(failures) == n
        # Deterministic *per environment*: every repetition failed on each
        # endpoint where any failed. The aggregate rate then hides a hard
        # failure behind a "flaky" label, so say so explicitly.
        per_endpoint_failures: dict[str, int] = defaultdict(int)
        for t in failures:
            per_endpoint_failures[str(t.get("endpoint_label"))] += 1
        env_deterministic = (
            not is_stall
            and not all_fail
            and all(per_endpoint_failures[label] == per_endpoint_totals[(op_id, label)] for label in labels)
        )
        if is_stall:
            episodes = sorted({str((t.get("attribution") or {}).get("stall_episode") or "") for t in failures})
            summary_text = "shared SSH channel stalls endpoint-wide under the maturation harness"
            symptom = (
                f"{len(episodes)} stall episode(s) on {len(labels)} endpoint(s) {', '.join(labels)}: "
                f"{len(failures)} consecutive trials across {len(affected_ops)} unrelated operations "
                f"({', '.join(affected_ops)}) all timed out at their full budget "
                f"({min(durations)}-{max(durations)} ms), then the endpoint recovered without intervention. "
                f"Attribution: {reason}"
            )
            root_cause = (
                "Consistent with the recorded ControlMaster signature: the multiplexed master for the endpoint "
                "is dead or wedged while its ControlPath socket still exists, so every new client attaches to "
                "the stale socket and waits out its full timeout. The per-command error text "
                "('remote python timed out') attributes the fault to the remote host, which is wrong. "
                "Confirm by correlating the episode start with the preceding CLI-spawned or SIGKILLed ssh process."
            )
            resolution = (
                "Pending. Candidate fixes to validate: (1) have the transport detect N consecutive full-budget "
                "timeouts on one endpoint and remove the stale ControlPath socket, (2) have wrappers that kill "
                "ssh processes never kill the master (or reap the socket afterwards), "
                "(3) report the stall as a transport fault rather than a remote timeout."
            )
        else:
            summary_text = f"{op_id} fails at the {layer} layer under the maturation harness"
            symptom = (
                f"{len(failures)} of {n} repetitions of {op_id} ({sample.get('shape')}) failed on "
                f"{len(labels)} endpoint(s) {', '.join(labels)}; statuses {', '.join(statuses)}; "
                f"failing step {step or 'call'}; failure durations {min(durations)}-{max(durations)} ms. "
                f"Attribution: {reason}"
            )
            if all_fail:
                determinism = "Every repetition failed, so this is a deterministic failure, not a flake. "
            elif env_deterministic:
                determinism = (
                    f"Deterministic per environment: every repetition failed on each of the {len(labels)} affected "
                    f"endpoint(s) and none failed elsewhere (remote python {', '.join(pythons)}). The aggregate pass "
                    "rate is an environment mix, not flakiness. "
                )
            else:
                determinism = "Intermittent (flake): some repetitions passed on the same endpoint. "
            root_cause = (
                f"Not yet attributed below the {layer} layer. "
                + determinism
                + "Inspect the raw trial output in the evidence directory before promoting."
            )
            resolution = (
                f"Pending. Reproduce with `python3 .agents/maturation/run.py --operation {op_id}` against the same "
                "endpoint kind, then attach the confirmed cause and fix before promotion."
            )
        payload = {
            "kind": "known-failure-signatures",
            "summary": summary_text,
            "owner_skill": OWNER_SKILL,
            "scope": {
                "component": "ssh-transport" if is_stall else str(sample.get("tool") or op_id),
                "environment": f"maturation-harness/{'+'.join(kinds)}",
                "subsystem": layer,
                "shape": "endpoint-stall" if is_stall else str(sample.get("shape") or ""),
            },
            "fingerprints": [item for item in (fingerprint, f"maturation {op_id} {layer}") if item],
            "symptom": symptom,
            "root_cause": root_cause,
            "resolution": resolution,
            "avoidance": "Treat the pass rate, not a single green run, as the maturity signal for this operation.",
            "applicable_versions": (
                f"maturation run {run_id}; endpoint kinds {', '.join(kinds)}; remote python {', '.join(pythons)}"
            ),
            "verification": {
                "status": "inconclusive",
                "checks": [f"Reproduced {len(failures)} of {n} repetitions in maturation run {run_id}."],
            },
            "evidence": [
                {"kind": "maturation-trials", "uri": f"{evidence_relative_dir}/trials.jsonl", "stable": False},
                {"kind": "maturation-report", "uri": f"{evidence_relative_dir}/run.json", "stable": False},
            ],
            "confidence": "medium" if (all_fail or env_deterministic or is_stall) else "low",
            "source": {"run_ids": [run_id]},
        }
        payload = redactor.value(payload)
        if contains_address(payload):
            raise ValueError(f"candidate for {op_id} still contains an endpoint identity after redaction")
        candidates.append(payload)
    return candidates


def capture_candidate(
    candidate: Mapping[str, Any],
    *,
    capture_script: Path = CAPTURE_SCRIPT,
    extra_args: list[str] | None = None,
    python: str | None = None,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Invoke ``knowledge_capture.py --input <file>`` and return its JSON."""
    if contains_address(candidate):
        return {"status": "refused", "error": "candidate contains an endpoint identity; not captured"}
    if not capture_script.exists():
        return {"status": "failed", "error": f"capture script missing: {capture_script.name}"}
    with tempfile.NamedTemporaryFile("w", suffix=".json", prefix="maturation-candidate-", delete=False, encoding="utf-8") as fh:
        json.dump(candidate, fh, ensure_ascii=False)
        temp_path = Path(fh.name)
    argv = [python or sys.executable, str(capture_script), "--input", str(temp_path), *(extra_args or [])]
    try:
        proc = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "error": "knowledge_capture.py timed out"}
    finally:
        temp_path.unlink(missing_ok=True)
    stdout = proc.stdout.strip()
    try:
        parsed = json.loads(stdout) if stdout else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict) or "status" not in parsed:
        return {
            "status": "contract-drift",
            "error": "knowledge_capture.py did not return a JSON object with a status field",
            "returncode": proc.returncode,
            "stdout_tail": stdout[-1000:],
            "stderr_tail": proc.stderr[-1000:],
        }
    parsed["returncode"] = proc.returncode
    if proc.returncode != 0:
        parsed.setdefault("stderr_tail", proc.stderr[-1000:])
    return parsed
