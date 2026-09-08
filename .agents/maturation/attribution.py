"""Attribute one failed trial to a layer.

A boolean hides the difference between "the SSH channel died", "the remote
command exited 1", "the tool service returned an instant timeout", and
"the result did not match the declared expectation". The knowledge base
already records that an instant timeout and a real timeout are different
faults; the attribution keeps that distinction explicit.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

LAYERS = (
    "transport",  # ssh/mux level: channel died, connection refused, exit 255
    "tool-service",  # the tool returned instantly/without a parsable result
    "tool-helper",  # the tool's own remote helper or wrapper code failed
    "timeout",  # a genuine deadline expiry
    "remote-command",  # the remote command ran and exited non-zero
    "path-policy",  # blocked by root/cwd/symlink policy
    "integrity",  # hash mismatch, partial or leftover state
    "remote-state",  # job registry/log state inconsistent
    "environment",  # endpoint environment fact violated (hostname mapping, python)
    "contract",  # tool reported success but result violates the expectation
    "harness",  # the harness itself raised
)

TRANSPORT_PATTERNS = (
    re.compile(r"ssh: connect to host", re.IGNORECASE),
    re.compile(r"connection (?:closed|reset|refused|timed out)", re.IGNORECASE),
    re.compile(r"kex_exchange_identification", re.IGNORECASE),
    re.compile(r"mux_client|control socket|ControlMaster", re.IGNORECASE),
    re.compile(r"broken pipe", re.IGNORECASE),
    re.compile(r"client_loop: send disconnect", re.IGNORECASE),
    re.compile(r"Permission denied \(publickey", re.IGNORECASE),
    re.compile(r"No route to host|Network is unreachable|Name or service not known", re.IGNORECASE),
)
ENVIRONMENT_STATUSES = frozenset({"hostname_unmapped", "python_missing"})
INSTANT_TIMEOUT_RATIO = 0.25
# A leaked subprocess.TimeoutExpired that still names ssh / ControlMaster is
# the mux hanging, not the harness raising. Do not put the raw exception
# (it carries destinations and local paths) into the reason string.
_SSH_MUX_HINT = re.compile(
    r"\b(?:ssh|ControlMaster|ControlPath|ControlPersist|mux_client|control socket)\b",
    re.IGNORECASE,
)


def _stderr_text(result: Mapping[str, Any]) -> str:
    preview = result.get("preview") or {}
    stderr = preview.get("stderr") if isinstance(preview, Mapping) else None
    if isinstance(stderr, Mapping):
        stderr = " ".join(str(stderr.get(key, "")) for key in ("text", "head", "tail"))
    parts = [str(stderr or ""), str(result.get("error") or "")]
    return "\n".join(parts)


def attribute(
    result: Mapping[str, Any] | None,
    *,
    expectation_error: str | None,
    duration_ms: int | None,
    timeout_ms: int | None,
    exception: str | None = None,
    op_class: str = "",
) -> dict[str, Any]:
    """Return ``{"layer", "reason", "fingerprint"}`` for a failed trial."""
    if exception:
        leaked = _leaked_timeout(exception)
        if leaked is not None:
            return leaked
        return {"layer": "harness", "reason": exception[:500], "fingerprint": _fingerprint(exception)}
    if not isinstance(result, Mapping):
        return {
            "layer": "tool-service",
            "reason": "tool returned no parsable result payload",
            "fingerprint": "tool returned no parsable result",
        }
    outcome = str(result.get("outcome") or "")
    status = str(result.get("status") or "")
    exit_code = result.get("exit_code")
    stderr = _stderr_text(result)
    if exit_code == 255 or any(pattern.search(stderr) for pattern in TRANSPORT_PATTERNS):
        return {
            "layer": "transport",
            "reason": f"ssh transport fault (exit={exit_code}, status={status})",
            "fingerprint": _fingerprint(f"transport {status} {stderr[:200]}"),
        }
    if outcome == "timeout" or status == "timeout":
        if duration_ms is not None and timeout_ms and duration_ms < timeout_ms * INSTANT_TIMEOUT_RATIO:
            return {
                "layer": "tool-service",
                "reason": (
                    f"instant timeout after {duration_ms} ms against a {timeout_ms} ms budget; "
                    "matches the recorded tool-service fault signature, not a remote fault"
                ),
                "fingerprint": "instant timeout any command",
            }
        return {
            "layer": "timeout",
            "reason": f"deadline expired after {duration_ms} ms (budget {timeout_ms} ms)",
            "fingerprint": _fingerprint(f"timeout {status}"),
        }
    if status in ENVIRONMENT_STATUSES or op_class == "environment":
        return {
            "layer": "environment",
            "reason": f"environment expectation violated (status={status}, outcome={outcome})",
            "fingerprint": _fingerprint(f"environment {status} {stderr[:200]}"),
        }
    if status in {"hash_mismatch", "partial_state", "leftover_temp_files"}:
        return {
            "layer": "integrity",
            "reason": f"integrity check failed (status={status})",
            "fingerprint": _fingerprint(f"integrity {status}"),
        }
    if status in {"log_not_found", "job_start_failed", "job_id_exists", "unknown"} or str(result.get("tool", "")).startswith("remote.job"):
        if outcome != "success" or expectation_error:
            return {
                "layer": "remote-state",
                "reason": f"job registry state inconsistent (status={status}, outcome={outcome})",
                "fingerprint": _fingerprint(f"job {status} {outcome}"),
            }
    error_text = str(result.get("error") or "")
    if error_text.startswith("remote python") or status == "exception":
        # The tool's own helper (remote Python snippet or local wrapper code)
        # failed — not the caller's command, not the transport.
        return {
            "layer": "tool-helper",
            "reason": f"tool helper failed (status={status}): {error_text[:200] or 'exception in tool code'}",
            "fingerprint": _fingerprint(f"tool-helper {status} {error_text[:200]}"),
        }
    if outcome == "blocked":
        return {
            "layer": "path-policy",
            "reason": f"blocked by path policy (status={status})",
            "fingerprint": _fingerprint(f"blocked {status}"),
        }
    if outcome in {"failed", "needs_input"} and status in {"nonzero_exit", "cwd_not_found", "exception", "failed"}:
        return {
            "layer": "remote-command",
            "reason": f"remote command failed (status={status}, exit={exit_code})",
            "fingerprint": _fingerprint(f"remote-command {status} {stderr[:200]}"),
        }
    if outcome == "success" and expectation_error:
        return {
            "layer": "contract",
            "reason": f"tool reported success but expectation failed: {expectation_error}",
            "fingerprint": _fingerprint(f"contract {expectation_error}"),
        }
    return {
        "layer": "contract",
        "reason": f"unexpected result (outcome={outcome}, status={status}): {expectation_error or ''}".strip(),
        "fingerprint": _fingerprint(f"unexpected {outcome} {status}"),
    }


def _exception_name(exception: str) -> str:
    head = exception.split("\n", 1)[0]
    return head.split(":", 1)[0].strip().rsplit(".", 1)[-1]


def _leaked_timeout(exception: str) -> dict[str, Any] | None:
    """Map a leaked ``TimeoutExpired`` to timeout/transport, never harness.

    ``remote.artifact_pull`` (and friends) sometimes raise
    ``subprocess.TimeoutExpired`` instead of returning a result payload.
    When the timed-out argv is ssh / ControlMaster, the shared mux is
    wedged; that is a transport fault. The raw exception text is discarded
    because it carries destinations and local paths.
    """
    if _exception_name(exception) != "TimeoutExpired":
        return None
    if _SSH_MUX_HINT.search(exception):
        return {
            "layer": "transport",
            "reason": (
                "TimeoutExpired leaked from the tool during an ssh/mux call; "
                "the shared ControlMaster is wedged"
            ),
            "fingerprint": "timeout leaked ssh mux",
        }
    return {
        "layer": "timeout",
        "reason": "TimeoutExpired leaked from the tool instead of a result payload",
        "fingerprint": "timeout leaked from tool",
    }


_HEX_RE = re.compile(r"\b[0-9a-f]{12,}\b", re.IGNORECASE)
_NUM_RE = re.compile(r"\b\d{3,}\b")
_WS_RE = re.compile(r"\s+")


def _fingerprint(text: str) -> str:
    text = _HEX_RE.sub("<hex>", text)
    text = _NUM_RE.sub("<n>", text)
    return _WS_RE.sub(" ", text).strip().lower()[:200]
