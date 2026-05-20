"""Regression: ssh_stream's wall-clock timeout must fire even when the
remote command produces no output.

We don't actually ssh anywhere — we replace ``_ssh_base_cmd`` with an
empty list so the command runs through the local shell. The behaviour
under test is the local timer + select-loop logic, which is the same
code path used over real ssh.
"""
from __future__ import annotations

import time

import conftest  # noqa: F401

import _common as common


class _MonkeyPatch:
    """Minimal monkey-patch helper so tests can run without pytest."""

    def __init__(self) -> None:
        self._undo = []

    def setattr(self, target, name, value):
        original = getattr(target, name)
        self._undo.append((target, name, original))
        setattr(target, name, value)

    def restore(self) -> None:
        for target, name, original in reversed(self._undo):
            setattr(target, name, original)


def _local_endpoint() -> common.SshEndpoint:
    return common.SshEndpoint(host="local", port=22, user="local")


def test_silent_hang_does_not_exceed_wall_budget() -> None:
    """With a 2 s budget, sleep 30 must NOT block more than 10 s wall time.

    Depending on the host, either the in-process select-loop deadline
    fires (TimeoutError) or the inner ``timeout`` binary kills the
    remote sleep first (rc != 0). Both are acceptable; what's not
    acceptable is blocking past the budget.
    """
    import threading

    monkey = _MonkeyPatch()
    try:
        monkey.setattr(common, "_ssh_base_cmd", lambda _ep: [])
        endpoint = _local_endpoint()
        done = threading.Event()
        outcome: dict[str, object] = {}

        def runner() -> None:
            try:
                outcome["rc"] = common.ssh_stream(
                    endpoint, "sleep 30", timeout=2, forward_prefix="[t] "
                )
            except TimeoutError as exc:
                outcome["err"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        finished = done.wait(timeout=10.0)
        assert finished, "ssh_stream blocked past the wall-clock budget"
        thread.join(timeout=2.0)

        # Either a TimeoutError or a non-zero rc is fine; a successful (rc=0)
        # full sleep means neither mechanism fired.
        if "rc" in outcome:
            rc = outcome["rc"]
            assert rc != 0, f"sleep 30 should not have returned rc=0 under timeout=2 (got rc={rc})"
    finally:
        monkey.restore()


def test_wall_budget_respected_when_remote_exits_immediately() -> None:
    """A trivially-quick command must not block past the budget either.

    We feed ``true`` (which exits in milliseconds) so the test passes
    even on hosts where shlex-quoted commands wouldn't normally work
    without ssh's argv-flattening behaviour.
    """
    import threading

    monkey = _MonkeyPatch()
    try:
        # Override _ssh_base_cmd to point at a local "true" wrapper so
        # the production command-construction path (shlex.quote of the
        # payload) doesn't matter — we only test the timer logic here.
        import shutil
        true_bin = shutil.which("true") or "/usr/bin/true"
        monkey.setattr(common, "_ssh_base_cmd", lambda _ep: [true_bin, "--"])
        endpoint = _local_endpoint()
        done = threading.Event()

        def runner() -> None:
            try:
                common.ssh_stream(endpoint, "noop", timeout=None, forward_prefix="[t] ")
            finally:
                done.set()

        thread = threading.Thread(target=runner, daemon=True)
        thread.start()
        finished = done.wait(timeout=5.0)
        assert finished, "ssh_stream blocked on an immediately-exiting child"
        thread.join(timeout=2.0)
    finally:
        monkey.restore()


if __name__ == "__main__":
    test_silent_hang_does_not_exceed_wall_budget()
    test_wall_budget_respected_when_remote_exits_immediately()
    print("ok")
